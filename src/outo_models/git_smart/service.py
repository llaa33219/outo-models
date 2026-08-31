"""Git smart-HTTP service.

`GitSmartService` exposes the `git clone/push/pull http(s)://host/<owner>/<name>.git`
surface on top of `dulwich.web.HTTPGitApplication`. The service is mounted
by WP-13 at root via `app.mount("/", git_service.asgi_app())` so the
clone URLs stay HF-style (`/{owner}/{name}.git`); the ASGI app sees
`PATH_INFO` shaped like `/<owner>/<name>.git/info/refs` (or `<name>`
without the `.git` suffix — we normalize).

Responsibilities, in order:

    1. Reject LFS requests with a 501 stub before they reach dulwich.
    2. Resolve the `(owner, name)` → `Repo` row from the DB; 404 if missing.
    3. Decide whether this is a PULL (upload-pack) or PUSH (receive-pack)
       request, including the GET `info/refs?service=…` advertisement for
       receive-pack (gated with PUSH semantics).
    4. Resolve `Authorization: Basic …` into a `User`; reject missing/invalid
       creds with `401` and the WWW-Authenticate challenge.
    5. Apply the authorize() decision matrix (public/private x pull/push).
    6. For PUSH, run `quota.check_push_allowed` against `Content-Length`.
       `QuotaExceededError` → `413`.
    7. Hand the WSGI-shaped request to `dulwich.web.HTTPGitApplication` via
       a minimal streaming WSGI↔ASGI adapter (request body buffered up to
       a configurable limit; larger bodies → `413`).
    8. After a successful PUSH (2xx), record `Revision` rows for every newly
       advanced branch, update `Repo.size_bytes` and `UserUsage`, and append
       a `repo.push` audit entry — all inside the per-repo
       `RepoLockRegistry` write lock.

The service owns its DB transaction on the post-push path so a partial
failure mid-bookkeeping does not leave the repo and the DB out of sync.

# allow: SIZE_OK — the WP-13 ownership list locks this file to
# `src/outo_models/git_smart/service.py`, so the WSGI↔ASGI bridge, the
# dulwich backend adapter, and the post-push bookkeeping helpers cannot
# live in separate modules of their own. Splitting would force changes
# outside the owned-file list, which the task contract forbids.
"""

from __future__ import annotations

import json
import os
from collections.abc import Awaitable, Callable, Iterable
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import parse_qs

from dulwich.errors import NotGitRepository
from dulwich.repo import Repo as _DulwichRepo
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from outo_models.config import Settings
from outo_models.db import (
    AuditLog,
    Repo,
    Revision,
    User,
    get_session_factory,
)
from outo_models.exceptions import QuotaExceededError
from outo_models.git_smart.auth import GitAction, authorize, resolve_git_identity
from outo_models.repos.quota import add_usage, check_push_allowed
from outo_models.repos.storage import REPO_LOCKS, disk_usage, repo_fs_path
from outo_models.utils.paths import repos_dir

if TYPE_CHECKING:
    from types import TracebackType


#: Default cap on the request body the adapter will buffer before it
#: 413s. Git pack files can grow large; 512 MiB is large enough for
#: realistic pushes and small enough that a runaway client cannot OOM
#: the process.
DEFAULT_MAX_PUSH_BYTES = 512 * 1024 * 1024


# ---------------------------------------------------------------------------
# Path / URL helpers
# ---------------------------------------------------------------------------


def _parse_path(path_info: str) -> tuple[str, str, list[str]] | None:
    """Split `PATH_INFO` into `(owner, name, remaining_segments)`.

    Returns `None` when the path does not match `<owner>/<name>` so the
    caller can answer `404` directly. `name` is the bare repo name with the
    optional `.git` suffix stripped.
    """
    segments = [seg for seg in path_info.split("/") if seg]
    if len(segments) < 2:
        return None
    owner, name = segments[0], segments[1]
    if not owner or not name:
        return None
    if name.endswith(".git"):
        name = name[: -len(".git")]
    return owner, name, segments[2:]


def _classify(
    *,
    method: str,
    rest_segments: list[str],
    query_string: str,
) -> tuple[GitAction, bool]:
    """Return `(action, is_info_refs_ad)` for the request.

    `is_info_refs_ad=True` marks the GET `info/refs?service=…` advertisement
    so the caller knows the body has no semantic effect (only PULL /
    receive-pack POSTs carry new objects).
    """
    method = method.upper()
    is_ad = False
    if method == "GET" and rest_segments[:1] == ["info"]:
        # `<repo>/info/refs?service=git-{upload,receive}-pack`
        if rest_segments[1:2] == ["refs"]:
            params = parse_qs(query_string)
            service = (params.get("service") or [""])[0]
            if service == "git-receive-pack":
                return GitAction.PUSH, True
            if service == "git-upload-pack":
                return GitAction.PULL, True
            # Unknown / missing service: dulwich will serve dumb HTTP
            # (read-only); treat as PULL so anonymous reads of public repos
            # still succeed.
            return GitAction.PULL, True
        return GitAction.PULL, is_ad
    if method == "POST":
        if rest_segments[:1] == ["git-upload-pack"]:
            return GitAction.PULL, is_ad
        if rest_segments[:1] == ["git-receive-pack"]:
            return GitAction.PUSH, is_ad
    return GitAction.PULL, is_ad  # default — dulwich will answer 404/405


def _is_lfs(rest_segments: list[str]) -> bool:
    """True iff the request targets `<repo>/info/lfs/*`."""
    return len(rest_segments) >= 2 and rest_segments[:2] == ["info", "lfs"]


# ---------------------------------------------------------------------------
# WSGI ↔ ASGI bridge
# ---------------------------------------------------------------------------


type ASGIReceive = Callable[[], Awaitable[dict[str, object]]]
type ASGISend = Callable[[dict[str, object]], Awaitable[None]]


class _WsgiToAsgi:
    """Minimal WSGI-to-ASGI bridge used to drive `dulwich.web.HTTPGitApplication`.

    The bridge buffers the request body into a `BytesIO` (v1 compromise;
    see module docstring) and assembles a fresh WSGI environ per request.
    Response streaming is collected into a single body chunk — git
    responses are small (refs advertisement or status report), so a
    single-buffered body is fine for v1.
    """

    def __init__(
        self,
        wsgi_app: Callable[..., Iterable[bytes]],
        *,
        max_body_bytes: int,
    ) -> None:
        self._wsgi_app = wsgi_app
        self._max_body_bytes = max_body_bytes

    async def _buffer_body(self, receive: ASGIReceive) -> bytes | None:
        """Drain the ASGI request body; `None` if the client disconnected."""
        chunks: list[bytes] = []
        total = 0
        more = True
        while more:
            event = await receive()
            if event.get("type") == "http.disconnect":
                return None
            chunk_obj = event.get("body")
            chunk = (
                b""
                if not isinstance(chunk_obj, (bytes, bytearray))
                else bytes(chunk_obj)
            )
            if chunk:
                total += len(chunk)
                if total > self._max_body_bytes:
                    return _OVER_LIMIT_SENTINEL
                chunks.append(chunk)
            more = bool(event.get("more_body", False))
        return b"".join(chunks)

    @staticmethod
    def _build_environ(scope: dict[str, object], body: bytes) -> dict[str, object]:
        """Translate the ASGI scope into a WSGI environ dict."""
        method = str(scope.get("method", "GET")).upper()
        raw_path = scope.get("path", "/")
        if isinstance(raw_path, bytes):
            raw_path = raw_path.decode("utf-8")
        raw_path = str(raw_path)

        raw_qs = scope.get("query_string", "")
        if isinstance(raw_qs, bytes):
            query_string = raw_qs.decode("latin-1")
        elif isinstance(raw_qs, str):
            query_string = raw_qs
        else:
            query_string = str(raw_qs or "")

        raw_headers_obj = scope.get("headers")
        if isinstance(raw_headers_obj, list):
            raw_headers: list[object] = raw_headers_obj
        else:
            raw_headers = []
        headers: list[tuple[str, str]] = []
        content_type = ""
        content_length = "0"
        for entry in raw_headers:
            if not isinstance(entry, (list, tuple)) or len(entry) != 2:
                continue
            raw_k, raw_v = entry
            k = raw_k.decode("latin-1") if isinstance(raw_k, bytes) else str(raw_k)
            v = raw_v.decode("latin-1") if isinstance(raw_v, bytes) else str(raw_v)
            key_lower = k.lower()
            headers.append((k, v))
            if key_lower == "content-type":
                content_type = v
            elif key_lower == "content-length":
                content_length = v

        environ: dict[str, object] = {
            "REQUEST_METHOD": method,
            "SCRIPT_NAME": "",
            "PATH_INFO": raw_path,
            "QUERY_STRING": query_string,
            "SERVER_NAME": "outo-models",
            "SERVER_PORT": "80",
            "SERVER_PROTOCOL": "HTTP/1.1",
            "wsgi.version": (1, 0),
            "wsgi.url_scheme": "http",
            "wsgi.input": BytesIO(body),
            "wsgi.errors": _BytesIOSink(),
            "wsgi.multithread": True,
            "wsgi.multiprocess": False,
            "wsgi.run_once": False,
            "CONTENT_TYPE": content_type,
            "CONTENT_LENGTH": content_length,
            "wsgi.bytes_errors": "strict",
        }
        # Re-add HTTP headers in WSGI's `HTTP_*` format.
        for k, v in headers:
            key = k.upper().replace("-", "_")
            if key not in ("CONTENT_TYPE", "CONTENT_LENGTH"):
                environ[f"HTTP_{key}"] = v
        # `QUERY_STRING` may be bytes in some ASGI servers; dulwich expects str.
        environ["QUERY_STRING"] = query_string
        return environ

    async def __call__(
        self,
        scope: dict[str, object],
        receive: ASGIReceive,
        send: ASGISend,
    ) -> tuple[int, bytes] | None:
        """Drive the WSGI app; return `(status_code, body)` or `None` on disconnect."""
        body = await self._buffer_body(receive)
        if body is None:
            return None
        if body is _OVER_LIMIT_SENTINEL:
            await _send_413(send)
            return 413, b"Request body too large"

        environ = self._build_environ(scope, body)

        captured: dict[str, object] = {}
        body_buffer: list[bytes] = []

        def write(data: bytes) -> None:
            body_buffer.append(bytes(data))

        def start_response(
            status: str,
            headers: list[tuple[str, str]],
            _exc_info: tuple[type[BaseException], BaseException, TracebackType] | None = None,
        ) -> Callable[[bytes], None]:
            captured["status"] = status
            captured["headers"] = headers
            return write

        result: Iterable[bytes] = self._wsgi_app(environ, start_response)
        # Drain the WSGI iterable — dulwich may write some bytes via
        # `result.__next__` as well as via the `write` callable.
        chunks: list[bytes] = []
        for chunk in result:
            if chunk:
                chunks.append(bytes(chunk))
        if hasattr(result, "close"):
            result.close()

        status = captured.get("status", "500 Internal Server Error")
        try:
            status_code = int(str(status).split(" ", 1)[0])
        except (ValueError, AttributeError):
            status_code = 500
        response_headers_raw_obj = captured.get("headers", [])
        response_headers_raw: list[object] = (
            response_headers_raw_obj if isinstance(response_headers_raw_obj, list) else []
        )
        response_headers: list[tuple[bytes, bytes]] = []
        for entry in response_headers_raw:
            if not isinstance(entry, (list, tuple)) or len(entry) != 2:
                continue
            k, v = entry
            kb = k.encode("latin-1") if isinstance(k, str) else bytes(k)
            vb = v.encode("latin-1") if isinstance(v, str) else bytes(v)
            response_headers.append((kb, vb))

        body_out = b"".join(chunks) + b"".join(body_buffer)
        await send(
            {
                "type": "http.response.start",
                "status": status_code,
                "headers": response_headers,
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": body_out,
                "more_body": False,
            }
        )
        return status_code, body_out


# Sentinel returned by `_WsgiToAsgi._buffer_body` to signal "too large".
_OVER_LIMIT_SENTINEL: bytes = b"\x00\x01oversize"


class _BytesIOSink:
    """Minimal WSGI `errors` sink that swallows writes.

    Dulwich may write tracebacks to `wsgi.errors` on protocol failures;
    we drop them on the floor in v1 to avoid an unconfigured stderr
    dependency. The error still surfaces via the response status.
    """

    def write(self, _data: str) -> None:
        return None

    def flush(self) -> None:
        return None


async def _send_413(send: ASGISend) -> None:
    """Emit a plain-text 413 directly via ASGI."""
    msg = b"Request body too large"
    await send(
        {
            "type": "http.response.start",
            "status": 413,
            "headers": [
                (b"content-type", b"text/plain; charset=utf-8"),
                (b"content-length", str(len(msg)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": msg, "more_body": False})


async def _send_404(send: ASGISend, message: str) -> None:
    body = message.encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": 404,
            "headers": [
                (b"content-type", b"text/plain; charset=utf-8"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body, "more_body": False})


async def _send_405(send: ASGISend, allow: str, message: str) -> None:
    """405 Method Not Allowed with `Allow` header; git uses this to detect
    that a bare-repo URL is *not* a WebDAV directory and should be served
    via the smart-HTTP endpoints.
    """
    body = message.encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": 405,
            "headers": [
                (b"content-type", b"text/plain; charset=utf-8"),
                (b"allow", allow.encode("ascii")),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body, "more_body": False})


async def _send_401(send: ASGISend, challenge: str) -> None:
    """401 with a `WWW-Authenticate: Basic …` challenge."""
    body = b"Authentication required"
    await send(
        {
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"text/plain; charset=utf-8"),
                (b"www-authenticate", challenge.encode("ascii")),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body, "more_body": False})


async def _send_403(send: ASGISend, message: str) -> None:
    body = message.encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": 403,
            "headers": [
                (b"content-type", b"text/plain; charset=utf-8"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body, "more_body": False})


# ---------------------------------------------------------------------------
# dulwich backend
# ---------------------------------------------------------------------------


class _RepoBackend:
    """`dulwich.server.Backend` that maps `/<owner>/<name>.git` to the on-disk repo.

    `dulwich.web.HTTPGitApplication` calls `open_repository(path)` with
    `path` formatted like `"/alice/myrepo.git"` (a leading slash because
    `dulwich.web.url_prefix` always returns one). Stock
    `FileSystemBackend` rejects absolute paths, so we resolve the path
    against `repos_dir()` ourselves and guard against `..` traversal.
    """

    def __init__(self, root: Path) -> None:
        self._root = str(root.resolve())

    def open_repository(self, path: str | bytes) -> _DulwichRepo:
        # Dulwich passes the path as `bytes` from the receive-pack handler
        # but as `str` from the info/refs advertisement handler.
        path_str = path.decode("utf-8") if isinstance(path, bytes) else path
        relative = path_str.lstrip("/")
        if not relative:
            raise NotGitRepository("empty repository path")
        parts = relative.split("/")
        if any(p in ("", ".", "..") for p in parts):
            raise NotGitRepository(f"Invalid repository path: {path_str!r}")
        full = (Path(self._root) / relative).resolve()
        root_prefix = self._root.rstrip(os.sep) + os.sep
        if not (str(full) + os.sep).startswith(root_prefix):
            raise NotGitRepository(f"Path {path_str!r} escapes repository root")
        if not full.is_dir():
            raise NotGitRepository(f"No repository at {path_str!r}")
        return _DulwichRepo(str(full))


# ---------------------------------------------------------------------------
# post-push bookkeeping
# ---------------------------------------------------------------------------


def _capture_branch_refs(repo_path: Path) -> dict[str, str]:
    """Return `{ref_name: sha}` for every `refs/heads/*` in the bare repo.

    Missing / branchless repos return an empty dict; callers should
    treat that as "no work to do".
    """
    if not repo_path.exists():
        return {}
    repo = _DulwichRepo(str(repo_path))
    refs: dict[str, str] = {}
    for raw in repo.refs:
        name = raw.decode("utf-8", errors="replace")
        if not name.startswith("refs/heads/"):
            continue
        sha_bytes = repo.refs.read_ref(raw)
        if sha_bytes is None:
            continue
        refs[name] = sha_bytes.decode("ascii")
    return refs


def _read_commit_message_author(
    repo_path: Path, commit_sha: str
) -> tuple[str, str] | None:
    """Read `(message, author)` for `commit_sha` from `repo_path`.

    Returns `None` if the object is missing or is not a commit — the
    caller should skip the row in that case rather than fail the whole
    bookkeeping transaction.
    """
    try:
        repo = _DulwichRepo(str(repo_path))
        obj = repo[commit_sha.encode("ascii")]
    except (KeyError, NotGitRepository):
        return None
    type_name = getattr(obj, "type_name", None)
    # `type_name` is bytes in modern dulwich (`b"commit"`); accept both.
    if type_name not in (b"commit", "commit"):
        return None
    # Only `Commit` objects carry `message` / `author`; `type_name` is
    # bytes on disk but `Commit` is the only class that produces it.
    from dulwich.objects import Commit
    if not isinstance(obj, Commit):
        return None
    message = obj.message.decode("utf-8", errors="replace")
    author = obj.author.decode("utf-8", errors="replace")
    return message, author


# ---------------------------------------------------------------------------
# the service itself
# ---------------------------------------------------------------------------


class GitSmartService:
    """ASGI service backing the `/git/...` mount.

    Constructed with `Settings`; the WSGI app is built lazily on first
    `asgi_app()` call so the service is cheap to instantiate.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        max_push_bytes: int = DEFAULT_MAX_PUSH_BYTES,
    ) -> None:
        self._settings = settings
        self._max_push_bytes = max_push_bytes
        self._wsgi_app_factory: Callable[..., Iterable[bytes]] | None = None
        self._adapter: _WsgiToAsgi | None = None
        self._lfs_path = Path(__file__).with_name("lfs.py")

    # ----- WSGI construction (lazy, idempotent) -----

    def _ensure_wsgi(self) -> _WsgiToAsgi:
        if self._adapter is not None:
            return self._adapter
        # Import dulwich here so the module can be imported on systems
        # without dulwich for type-checking only.
        from dulwich.web import HTTPGitApplication

        backend = _RepoBackend(repos_dir())
        # `dulwich.server.Backend` declares `open_repository(self, path: str)` but
        # our `_RepoBackend` widens the param to `str | bytes`; the call site is
        # still valid at runtime (every dulwich path is decodable).
        wsgi = HTTPGitApplication(backend=backend)  # type: ignore[arg-type]
        self._wsgi_app_factory = wsgi
        self._adapter = _WsgiToAsgi(wsgi, max_body_bytes=self._max_push_bytes)
        return self._adapter

    # ----- ASGI surface -----

    def asgi_app(
        self,
    ) -> Callable[[dict[str, object], ASGIReceive, ASGISend], Awaitable[None]]:
        """Return the ASGI app callable WP-13 mounts under `/git`."""
        adapter = self._ensure_wsgi()
        # Import the LFS handler lazily so an unused import doesn't slow
        # down module import elsewhere.
        from outo_models.git_smart.lfs import lfs_not_supported

        async def app(
            scope: dict[str, object],
            receive: ASGIReceive,
            send: ASGISend,
        ) -> None:
            if scope.get("type") != "http":
                return
            method = str(scope.get("method", "GET")).upper()
            path_info = str(scope.get("path", "/"))
            query_string = str(scope.get("query_string", ""))
            # `query_string` may arrive as bytes depending on the ASGI
            # server; normalise.
            if isinstance(query_string, bytes):
                query_string = query_string.decode("latin-1")

            parsed = _parse_path(path_info)
            if parsed is None:
                await _send_404(send, "not found")
                return
            owner_name, repo_name, rest = parsed

            # Reject WebDAV-style probes (PROPFIND, LOCK, MKACTIVITY, …) up
            # front so `git` learns the URL is NOT a regular directory and
            # falls back to the smart-HTTP endpoints.
            if method not in ("GET", "POST"):
                await _send_405(send, "GET, POST", "method not allowed")
                return

            # LFS short-circuit (must run before auth/quota).
            if _is_lfs(rest):
                await lfs_not_supported(scope, receive, send)
                return

            action, _is_ad = _classify(
                method=method, rest_segments=rest, query_string=query_string
            )

            # Locate the Repo + owner User.
            async with self._session() as session:
                repo_row = (
                    await session.execute(
                        select(Repo).where(
                            Repo.name == repo_name,
                            Repo.owner_id == select(User.id)
                            .where(User.username == owner_name)
                            .scalar_subquery(),
                        )
                    )
                ).scalar_one_or_none()
                if repo_row is None:
                    await _send_404(send, f"repository not found: {owner_name}/{repo_name}")
                    return
                # Load the owner User row for `authorize()`.
                owner = (
                    await session.execute(
                        select(User).where(User.id == repo_row.owner_id)
                    )
                ).scalar_one()

            # Resolve identity from Authorization header.
            from outo_models.git_smart.auth import _build_auth_challenge

            raw_headers_obj = scope.get("headers")
            auth_header_obj = _header_value(raw_headers_obj, "authorization")
            auth_header: str | None = (
                str(auth_header_obj) if auth_header_obj is not None else None
            )
            user = await resolve_git_identity(auth_header, settings=self._settings)

            raw_headers = raw_headers_obj

            # Authorization gate.
            try:
                await authorize(
                    user,
                    repo=repo_row,
                    owner=owner,
                    action=action,
                )
            except Exception as exc:  # typed below; broad on purpose
                status = getattr(exc, "status_code", 403)
                if status == 401:
                    await _send_401(send, _build_auth_challenge())
                else:
                    msg = str(exc) or "forbidden"
                    await _send_403(send, msg)
                return

            # Quota check for PUSH.
            if action is GitAction.PUSH:
                content_length = _header_value(raw_headers, "content-length")
                incoming = int(content_length) if content_length else 0
                async with self._session() as session:
                    owner_for_quota = (
                        await session.execute(
                            select(User).where(User.id == owner.id)
                        )
                    ).scalar_one()
                    try:
                        await check_push_allowed(
                            session, owner_for_quota, incoming
                        )
                        await session.commit()
                    except QuotaExceededError as exc:
                        # `QuotaExceededError.status_code == 413`.
                        await _send_413(send)
                        msg = str(exc)
                        await send(
                            {
                                "type": "http.response.start",
                                "status": 413,
                                "headers": [
                                    (b"content-type", b"text/plain; charset=utf-8"),
                                ],
                            }
                        )
                        await send(
                            {
                                "type": "http.response.body",
                                "body": msg.encode("utf-8"),
                                "more_body": False,
                            }
                        )
                        return

            # Dulwich looks up the repo by prefix (`/owner/name.git`) and
            # would resolve to a missing dir without the `.git` suffix.
            tail = "/".join(rest)
            normalised_path = f"/{owner_name}/{repo_name}.git"
            if tail:
                normalised_path = f"{normalised_path}/{tail}"
            normalised_scope = dict(scope)
            normalised_scope["path"] = normalised_path
            normalised_scope["raw_path"] = None
            raw_qs = normalised_scope.get("query_string")
            if isinstance(raw_qs, bytes):
                normalised_scope["query_string"] = raw_qs.decode("latin-1")

            # Capture pre-push refs (only for PUSH, where bookkeeping matters).
            pre_refs: dict[str, str] = {}
            if action is GitAction.PUSH:
                pre_refs = _capture_branch_refs(
                    repo_fs_path(owner_name, repo_name)
                )

            result = await adapter(normalised_scope, receive, send)
            if result is None:
                return
            status_code, _body = result

            # Post-PUSH bookkeeping.
            if (
                action is GitAction.PUSH
                and 200 <= status_code < 300
                and user is not None
            ):
                await self._record_push(
                    owner=owner,
                    repo=repo_row,
                    pre_refs=pre_refs,
                    actor=user,
                )

        return app

    # ----- helpers -----

    @staticmethod
    def _session() -> AsyncSession:
        """Return a fresh `AsyncSession` ready for use as an async context."""
        return get_session_factory()()

    async def _record_push(
        self,
        *,
        owner: User,
        repo: Repo,
        pre_refs: dict[str, str],
        actor: User,
    ) -> None:
        """Record revisions + audit + size delta after a successful push.

        All work happens under `REPO_LOCKS.acquire(owner, name)` so a
        concurrent write to the same repo serializes behind this
        bookkeeping. The session is owned here — we commit on success
        and roll back on failure.
        """
        owner_username = owner.username
        name = repo.name
        fs_path = repo_fs_path(owner_username, name)

        async with REPO_LOCKS.acquire(owner_username, name):
            post_refs = _capture_branch_refs(fs_path)
            advances: list[dict[str, str | None]] = []
            revisions: list[Revision] = []
            for ref_name, new_sha in post_refs.items():
                if pre_refs.get(ref_name) == new_sha:
                    continue
                advances.append(
                    {"ref": ref_name, "old": pre_refs.get(ref_name), "new": new_sha}
                )
                details = _read_commit_message_author(fs_path, new_sha)
                if details is None:
                    continue
                message, _author_line = details
                branch = ref_name.removeprefix("refs/heads/")
                revisions.append(
                    Revision(
                        repo_id=repo.id,
                        commit_sha=new_sha,
                        branch=branch,
                        author_id=actor.id,
                        message=message,
                        size_bytes=0,
                    )
                )
            if not advances:
                return

            async with self._session() as session:
                repo_row = (
                    await session.execute(
                        select(Repo).where(Repo.id == repo.id)
                    )
                ).scalar_one()
                owner_row = (
                    await session.execute(
                        select(User).where(User.id == owner.id)
                    )
                ).scalar_one()

                new_size = await disk_usage(fs_path)
                size_delta = new_size - repo_row.size_bytes

                session.add_all(revisions)
                repo_row.size_bytes = new_size
                session.add(
                    AuditLog(
                        actor_id=actor.id,
                        action="repo.push",
                        target_type="repo",
                        target_id=str(repo.id),
                        detail=json.dumps(
                            {
                                "repo": f"{owner_username}/{name}",
                                "branches": advances,
                            }
                        ),
                    )
                )
                await add_usage(session, owner_row, size_delta)
                await session.commit()


def _header_value(headers: object, name: str) -> str | None:
    """Return the first matching header value from ASGI headers (case-insensitive)."""
    if not isinstance(headers, list):
        return None
    needle = name.lower()
    for raw_k, raw_v in headers:
        k = raw_k.decode("latin-1").lower() if isinstance(raw_k, bytes) else str(raw_k).lower()
        if k == needle:
            return (
                raw_v.decode("latin-1") if isinstance(raw_v, bytes) else str(raw_v)
            )
    return None


__all__ = [
    "DEFAULT_MAX_PUSH_BYTES",
    "GitSmartService",
]
