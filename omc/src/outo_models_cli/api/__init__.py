"""Typed client for the public outo-models REST API.

Each public method in this package is a thin wrapper around one HTTP
call. The shared logic — error mapping, transport-layer wrapping, auth
header insertion — lives in `http.build_client`. Command modules never
touch `httpx` directly so a wire-format change (renamed field, new
envelope) only touches one file.

The methods are *synchronous* because the CLI commands that use them
(single-shot ops like `repo create`, `repo list`, `auth whoami`, `ls`)
are short-lived and benefit from the simpler control flow. The
streaming download command uses `http.build_async_client` directly
because parallelism is the whole point of that command.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from outo_models_cli.errors import (
    BadResponseError,
    map_transport_error,
)
from outo_models_cli.http import build_client

# ---------------------------------------------------------------------------
# Dataclasses — response shapes, pinned for the wire contract
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WhoAmI:
    """Result of `GET /api/auth/me`."""

    username: str
    role: str
    server: str


@dataclass(frozen=True, slots=True)
class RepoSummary:
    """Subset of `GET /api/repos` / `POST /api/repos` payloads.

    Fields not needed by the CLI (e.g. `id`, timestamps) are dropped on
    the server's JSON payload so the response stays minimal. Commands
    that need the full record should call `get_repo()`.
    """

    name: str
    kind: str
    visibility: str
    description: str | None
    size_bytes: int
    owner: str
    clone_url: str


@dataclass(frozen=True, slots=True)
class RepoDetail(RepoSummary):
    """`GET /api/repos/{owner}/{name}` payload."""

    downloads_count: int = 0


@dataclass(frozen=True, slots=True)
class FileEntry:
    """One row in `GET /api/repos/{owner}/{name}/files`."""

    name: str
    path: str
    kind: str  # "file" | "dir"
    size_bytes: int | None


@dataclass(frozen=True, slots=True)
class UploadResult:
    """Server's response to a successful `POST .../upload`."""

    commit_sha: str
    files: list[str]
    message: str | None = None


# ---------------------------------------------------------------------------
# Shared JSON unwrap + error funnel
# ---------------------------------------------------------------------------


def unwrap(response: httpx.Response) -> dict[str, Any]:
    """Parse a JSON object body or raise a clean error."""
    if response.status_code == 204 or not response.content:
        return {}
    try:
        payload: Any = response.json()
    except ValueError as exc:
        raise BadResponseError("The server returned a non-JSON response.") from exc
    if not isinstance(payload, dict):
        raise BadResponseError("The server returned an unexpected response shape.")
    return payload


def send(client: httpx.Client, method: str, path: str, **kwargs: Any) -> httpx.Response:
    """Issue `method path` and translate transport failures."""
    try:
        return client.request(method, path, **kwargs)
    except httpx.HTTPError as exc:
        raise map_transport_error(exc) from exc


# ---------------------------------------------------------------------------
# Repos helpers (shared by submodules)
# ---------------------------------------------------------------------------


def summary_from(payload: dict[str, Any]) -> RepoSummary:
    """Parse a `/api/repos` row into a `RepoSummary`."""
    owner = payload.get("owner", "")
    description = payload.get("description")
    return RepoSummary(
        name=str(payload.get("name", "")),
        kind=str(payload.get("kind", "")),
        visibility=str(payload.get("visibility", "")),
        description=None if description is None else str(description),
        size_bytes=int(payload.get("size_bytes", 0)),
        owner=str(owner),
        clone_url=str(payload.get("clone_url", "")),
    )


# ---------------------------------------------------------------------------
# Facade — issue calls against a fresh `httpx.Client`
# ---------------------------------------------------------------------------


def with_client(
    base_url: str,
    token: str,
    *,
    transport: httpx.BaseTransport | None = None,
) -> httpx.Client:
    """Construct a short-lived client for the caller to use as a context.

    `transport=` is forwarded so tests can inject an `httpx.MockTransport`
    (or a `respx` router) without monkeypatching — same convention as
    the server's admin client.
    """
    kwargs: dict[str, Any] = {}
    if transport is not None:
        kwargs["transport"] = transport
    return build_client(base_url, token, **kwargs)


# ---------------------------------------------------------------------------
# Display-name helper used by `upload`
# ---------------------------------------------------------------------------


def display_filename(path_str: str, *, file_root: Path | None, idx: int) -> str:
    """Compute the filename sent in the multipart part.

    For file uploads (no `file_root`), the basename is sent verbatim.
    For folder uploads, the relative path under `file_root` is sent so
    the server stores `dir/file.txt` rather than `file.txt`.
    """
    name = Path(path_str).name
    if file_root is None:
        return name
    try:
        rel = Path(path_str).resolve().relative_to(file_root.resolve())
    except ValueError:
        # Fallback to basename if the file isn't actually under `file_root`.
        # The server will still store it under `path_in_repo`, so the
        # upload cannot silently land in the wrong place — it just
        # degrades to a flat listing.
        return name
    rel_str = rel.as_posix()
    return rel_str or name or f"file-{idx}"


# ---------------------------------------------------------------------------
# Submodule re-exports
# ---------------------------------------------------------------------------
# Imported last so the dataclasses defined above are visible to the
# submodules' `from outo_models_cli.api import ...` statements. Call
# sites can write `api.me(...)` instead of `api.auth.me(...)`.

from outo_models_cli.api.auth import me  # noqa: E402
from outo_models_cli.api.repos import (  # noqa: E402
    create_repo,
    delete_repo,
    get_repo,
    list_files,
    list_repos,
    resolve_url,
    walk_repo,
)
from outo_models_cli.api.upload import upload  # noqa: E402

__all__ = [
    "FileEntry",
    "RepoDetail",
    "RepoSummary",
    "UploadResult",
    "WhoAmI",
    "create_repo",
    "delete_repo",
    "display_filename",
    "get_repo",
    "list_files",
    "list_repos",
    "me",
    "resolve_url",
    "send",
    "summary_from",
    "unwrap",
    "upload",
    "walk_repo",
    "with_client",
]
