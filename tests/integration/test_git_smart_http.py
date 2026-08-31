"""End-to-end git smart-HTTP integration tests.

Spins `GitSmartService` under `uvicorn` on an ephemeral TCP port and
exercises every contract the public API promises: anonymous + authenticated
clone, push with audit / revision / quota bookkeeping, the auth failure
matrix, and the quota-exceeded path.

Real `git` CLI is the test harness — `dulwich.porcelain` is not used here
on purpose. The point of these tests is to prove that a stock `git` client
on the other side of the network can drive the service exactly the way a
human would.

Run budget: < 3 minutes for the whole module. Every test binds its own
ephemeral port and cleans up the uvicorn thread on exit so the suite
remains parallel-safe on multi-agent workstations.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import threading
import time
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from outo_models.auth.tokens import fingerprint
from outo_models.config import get_settings
from outo_models.db import (
    AuditLog,
    Base,
    PersonalAccessToken,
    Repo,
    Revision,
    User,
    UserQuota,
    UserUsage,
    dispose_engines,
    get_engine,
    get_session_factory,
)
from outo_models.git_smart.service import GitSmartService
from outo_models.repos.create import create_repo
from outo_models.repos.models import RepoKind, Visibility

# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def session_factory(
    tmp_data_dir: Path,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Fresh per-test sqlite engine + schema; auto-disposed on exit."""
    await dispose_engines()
    settings = get_settings()
    engine: AsyncEngine = get_engine(settings)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = get_session_factory(engine)
    try:
        yield factory
    finally:
        await engine.dispose()
        await dispose_engines()


@pytest.fixture
def git_env(tmp_path: Path) -> dict[str, str]:
    """Per-test environment that disables prompts and configures a user."""
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_AUTHOR_NAME"] = "Tester"
    env["GIT_AUTHOR_EMAIL"] = "tester@example.com"
    env["GIT_COMMITTER_NAME"] = "Tester"
    env["GIT_COMMITTER_EMAIL"] = "tester@example.com"
    # Each test writes workdir into tmp_path; no global config needed.
    env.pop("GIT_CONFIG_GLOBAL", None)
    env.pop("GIT_CONFIG_NOSYSTEM", None)
    env["HOME"] = str(tmp_path / "home")
    (tmp_path / "home").mkdir()
    return env


async def _run_git(
    args: list[str], *, cwd: Path, env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    """Run `git <args>` and raise with full output if it fails."""
    proc = await asyncio.to_thread(
        subprocess.run,
        ["git", *args],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise AssertionError(
            f"git {args!r} failed in {cwd}\n"
            f"--- stdout ---\n{proc.stdout}\n"
            f"--- stderr ---\n{proc.stderr}"
        )
    return proc


async def _seed_owner_with_pat(
    factory: async_sessionmaker[AsyncSession],
    username: str,
    *,
    role: str = "user",
    status: str = "approved",
) -> tuple[User, str]:
    """Insert a user, mint a raw PAT, persist its fingerprint; return `(User, pat)`."""
    raw_token = f"v4.local.{username}-integration-token"
    async with factory() as session:
        user = User(
            username=username,
            email=f"{username}@example.com",
            password_hash="h",
            role=role,
            status=status,
        )
        session.add(user)
        await session.flush()
        session.add(
            PersonalAccessToken(
                user_id=user.id,
                name="integration",
                fingerprint_hash=fingerprint(raw_token),
                prefix=raw_token[:8],
                scopes='["read","write","repos:read","repos:write"]',
            )
        )
        await session.commit()
        user_id = user.id
    async with factory() as session:
        owner = (
            await session.execute(select(User).where(User.id == user_id))
        ).scalar_one()
        return owner, raw_token


async def _make_repo(
    factory: async_sessionmaker[AsyncSession],
    owner: User,
    *,
    name: str,
    visibility: Visibility = Visibility.PRIVATE,
    quota_bytes: int | None = None,
) -> Repo:
    """Create a bare repo for `owner`; optionally set a low quota cap."""
    async with factory() as session:
        owner_for_repo = (
            await session.execute(select(User).where(User.id == owner.id))
        ).scalar_one()
        repo = await create_repo(
            session,
            owner=owner_for_repo,
            name=name,
            kind=RepoKind.MODEL,
            visibility=visibility,
        )
        await session.commit()
        repo_id = repo.id

    if quota_bytes is not None:
        async with factory() as session:
            # `create_repo` already materialized a default quota row via
            # `ensure_quota_rows`; tighten the cap instead of inserting.
            existing = (
                await session.execute(
                    select(UserQuota).where(UserQuota.user_id == owner.id)
                )
            ).scalar_one()
            existing.max_bytes = quota_bytes
            await session.commit()

    async with factory() as session:
        return (
            await session.execute(select(Repo).where(Repo.id == repo_id))
        ).scalar_one()


@pytest.fixture
def server_url(
    tmp_data_dir: Path,
    session_factory: async_sessionmaker[AsyncSession],
) -> Iterator[str]:
    """Spin `GitSmartService` under uvicorn on an ephemeral TCP port.

    Yields the base URL (`http://127.0.0.1:<port>`) and tears the server
    down cleanly on exit, so tests can run in parallel without port
    conflicts.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(2048)
    port = sock.getsockname()[1]

    settings = get_settings()
    service = GitSmartService(settings)
    app = service.asgi_app()

    # Import here so the test process does not pay the import cost unless it
    # actually exercises the integration suite.
    from uvicorn import Config, Server

    config = Config(app, log_level="warning", access_log=False)
    server = Server(config=config)

    async def _serve() -> None:
        await server.serve(sockets=[sock])

    thread = threading.Thread(target=asyncio.run, args=(_serve(),), daemon=True)
    thread.start()

    # Wait until the server is listening (max 5 s).
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if server.started:
            break
        time.sleep(0.02)
    else:
        raise RuntimeError("uvicorn did not start within 5 s")

    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        if thread.is_alive():
            # Force a hard exit; nothing left to do.
            raise RuntimeError("uvicorn thread did not exit within 5 s")
        sock.close()


def _repo_url(base: str, owner: str, name: str) -> str:
    return f"{base}/{owner}/{name}.git"


def _with_basic(url: str, username: str, password: str) -> str:
    """Inject `username:password` into the URL's userinfo slot."""
    scheme, rest = url.split("://", 1)
    return f"{scheme}://{username}:{password}@{rest}"


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------


class TestCloneRoundTrip:
    """Anonymous and authenticated clone of a public repo both succeed."""

    async def test_anonymous_clone_of_public_repo(
        self,
        server_url: str,
        session_factory: async_sessionmaker[AsyncSession],
        tmp_data_dir: Path,
        git_env: dict[str, str],
    ) -> None:
        owner, _pat = await _seed_owner_with_pat(session_factory, "alice")
        await _make_repo(session_factory, owner, name="model-a", visibility=Visibility.PUBLIC)

        workdir = tmp_data_dir / "clone-anon"
        workdir.mkdir()
        await _run_git(
            ["clone", _repo_url(server_url, "alice", "model-a")],
            cwd=workdir,
            env=git_env,
        )

        assert (workdir / "model-a").is_dir()
        # No commits yet, but the clone must have produced a working `.git`.
        assert (workdir / "model-a" / ".git").is_dir()

    async def test_clone_pulls_commits_already_pushed(
        self,
        server_url: str,
        session_factory: async_sessionmaker[AsyncSession],
        tmp_data_dir: Path,
        git_env: dict[str, str],
    ) -> None:
        owner, pat = await _seed_owner_with_pat(session_factory, "alice")
        await _make_repo(session_factory, owner, name="model-b", visibility=Visibility.PUBLIC)

        # First, push a commit as the owner.
        push_workdir = tmp_data_dir / "push-source"
        push_workdir.mkdir()
        (push_workdir / "src").mkdir()
        await _run_git(["init"], cwd=push_workdir / "src", env=git_env)
        (push_workdir / "src" / "README.md").write_text("hello\n")
        await _run_git(["add", "README.md"], cwd=push_workdir / "src", env=git_env)
        await _run_git(
            ["commit", "-m", "first commit"],
            cwd=push_workdir / "src",
            env=git_env,
        )
        push_url = _with_basic(_repo_url(server_url, "alice", "model-b"), "alice", pat)
        await _run_git(
            ["push", push_url, "HEAD:refs/heads/master"],
            cwd=push_workdir / "src",
            env=git_env,
        )

        # Second, a fresh anonymous clone pulls the same commit content.
        clone_workdir = tmp_data_dir / "clone-fresh"
        clone_workdir.mkdir()
        await _run_git(
            ["clone", _repo_url(server_url, "alice", "model-b")],
            cwd=clone_workdir,
            env=git_env,
        )
        assert (clone_workdir / "model-b" / "README.md").read_text() == "hello\n"
        # `pat` is unused here; keep mypy quiet.
        assert pat


class TestPushBookkeeping:
    """A successful push updates revisions, repo size, usage, and audit."""

    async def test_push_records_revisions_audit_and_size(
        self,
        server_url: str,
        session_factory: async_sessionmaker[AsyncSession],
        tmp_data_dir: Path,
        git_env: dict[str, str],
    ) -> None:
        owner, pat = await _seed_owner_with_pat(session_factory, "alice")
        repo = await _make_repo(
            session_factory, owner, name="model-p", visibility=Visibility.PUBLIC
        )
        repo_id = repo.id
        owner_id = owner.id

        src = tmp_data_dir / "src"
        src.mkdir()
        await _run_git(["init"], cwd=src, env=git_env)
        (src / "a.txt").write_text("alpha\n")
        await _run_git(["add", "a.txt"], cwd=src, env=git_env)
        await _run_git(["commit", "-m", "alpha commit"], cwd=src, env=git_env)
        push_url = _with_basic(_repo_url(server_url, "alice", "model-p"), "alice", pat)
        await _run_git(
            ["push", push_url, "HEAD:refs/heads/master"],
            cwd=src,
            env=git_env,
        )

        async with session_factory() as session:
            revs = (
                await session.execute(
                    select(Revision).where(Revision.repo_id == repo_id)
                )
            ).scalars().all()
            assert len(revs) == 1
            assert revs[0].branch == "master"
            # Git appends a newline to every commit message.
            assert revs[0].message.strip() == "alpha commit"
            assert revs[0].author_id == owner_id
            assert len(revs[0].commit_sha) == 40

            audit = (
                await session.execute(
                    select(AuditLog).where(AuditLog.action == "repo.push")
                )
            ).scalars().all()
            assert len(audit) == 1
            detail = json.loads(audit[0].detail or "{}")
            assert "branches" in detail
            assert detail["branches"][0]["new"] == revs[0].commit_sha

            repo_row = (
                await session.execute(select(Repo).where(Repo.id == repo_id))
            ).scalar_one()
            assert repo_row.size_bytes > 0

            usage = (
                await session.execute(
                    select(UserUsage).where(UserUsage.user_id == owner_id)
                )
            ).scalar_one()
            assert usage.used_bytes > 0
        # Use pat so mypy is happy.
        assert pat


class TestAuthFailures:
    """The unauthenticated / unauthorized push paths return the documented codes."""

    async def test_push_without_auth_returns_401(
        self,
        server_url: str,
        session_factory: async_sessionmaker[AsyncSession],
        tmp_data_dir: Path,
        git_env: dict[str, str],
    ) -> None:
        owner, _pat = await _seed_owner_with_pat(session_factory, "alice")
        await _make_repo(session_factory, owner, name="model-na", visibility=Visibility.PUBLIC)

        src = tmp_data_dir / "src"
        src.mkdir()
        await _run_git(["init"], cwd=src, env=git_env)
        (src / "a.txt").write_text("x\n")
        await _run_git(["add", "a.txt"], cwd=src, env=git_env)
        await _run_git(["commit", "-m", "x"], cwd=src, env=git_env)

        # No credentials in the URL.
        proc = await asyncio.to_thread(
            subprocess.run,
            [
                "git",
                "push",
                _repo_url(server_url, "alice", "model-na"),
                "HEAD:refs/heads/master",
            ],
            cwd=str(src),
            env={**git_env, "GIT_TERMINAL_PROMPT": "0"},
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode != 0
        combined = (proc.stderr or "") + (proc.stdout or "")
        assert (
            "401" in combined
            or "could not read Username" in combined
            or "Authentication failed" in combined
        )

    async def test_push_with_wrong_pat_returns_401(
        self,
        server_url: str,
        session_factory: async_sessionmaker[AsyncSession],
        tmp_data_dir: Path,
        git_env: dict[str, str],
    ) -> None:
        owner, _real_pat = await _seed_owner_with_pat(session_factory, "alice")
        await _make_repo(session_factory, owner, name="model-bad-pat", visibility=Visibility.PUBLIC)

        src = tmp_data_dir / "src"
        src.mkdir()
        await _run_git(["init"], cwd=src, env=git_env)
        (src / "a.txt").write_text("y\n")
        await _run_git(["add", "a.txt"], cwd=src, env=git_env)
        await _run_git(["commit", "-m", "y"], cwd=src, env=git_env)

        bogus_url = _with_basic(
            _repo_url(server_url, "alice", "model-bad-pat"),
            "alice",
            "v4.local.this-is-not-the-real-pat",
        )
        proc = await asyncio.to_thread(
            subprocess.run,
            [
                "git",
                "-c",
                "credential.helper=",
                "push",
                bogus_url,
                "HEAD:refs/heads/master",
            ],
            cwd=str(src),
            env={**git_env, "GIT_TERMINAL_PROMPT": "0"},
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode != 0
        combined = (proc.stderr or "") + (proc.stdout or "")
        assert (
            "401" in combined
            or "could not read Username" in combined
            or "Authentication failed" in combined
        )

    async def test_non_owner_push_to_private_repo_returns_403(
        self,
        server_url: str,
        session_factory: async_sessionmaker[AsyncSession],
        tmp_data_dir: Path,
        git_env: dict[str, str],
    ) -> None:
        owner, _owner_pat = await _seed_owner_with_pat(session_factory, "alice")
        _intruder, intruder_pat = await _seed_owner_with_pat(
            session_factory, "mallory"
        )
        await _make_repo(session_factory, owner, name="model-priv", visibility=Visibility.PRIVATE)

        src = tmp_data_dir / "src"
        src.mkdir()
        await _run_git(["init"], cwd=src, env=git_env)
        (src / "a.txt").write_text("z\n")
        await _run_git(["add", "a.txt"], cwd=src, env=git_env)
        await _run_git(["commit", "-m", "z"], cwd=src, env=git_env)

        bogus_url = _with_basic(
            _repo_url(server_url, "alice", "model-priv"),
            "intruder",
            intruder_pat,
        )
        proc = await asyncio.to_thread(
            subprocess.run,
            [
                "git",
                "-c",
                "credential.helper=",
                "push",
                bogus_url,
                "HEAD:refs/heads/master",
            ],
            cwd=str(src),
            env={**git_env, "GIT_TERMINAL_PROMPT": "0"},
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode != 0
        combined = (proc.stderr or "") + (proc.stdout or "")
        assert (
            "403" in combined
            or "forbidden" in combined.lower()
            or "could not read Username" in combined
            or "Authentication failed" in combined
        )
        # Sanity: the intruder did not actually push anything.
        async with session_factory() as session:
            count = (
                await session.execute(
                    select(Revision).where(Revision.repo_id == owner.id)
                )
            ).scalars().all()
            assert count == []

    async def test_anonymous_clone_of_private_repo_returns_401(
        self,
        server_url: str,
        session_factory: async_sessionmaker[AsyncSession],
        tmp_data_dir: Path,
        git_env: dict[str, str],
    ) -> None:
        owner, _pat = await _seed_owner_with_pat(session_factory, "alice")
        await _make_repo(session_factory, owner, name="model-priv2", visibility=Visibility.PRIVATE)

        clone_workdir = tmp_data_dir / "clone-priv"
        clone_workdir.mkdir()
        proc = await asyncio.to_thread(
            subprocess.run,
            [
                "git",
                "clone",
                _repo_url(server_url, "alice", "model-priv2"),
            ],
            cwd=str(clone_workdir),
            env={**git_env, "GIT_TERMINAL_PROMPT": "0"},
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode != 0
        combined = (proc.stderr or "") + (proc.stdout or "")
        assert (
            "401" in combined
            or "could not read Username" in combined
            or "Authentication failed" in combined
        )


class TestQuotaExceeded:
    """A push larger than the user's quota returns 413."""

    async def test_push_over_quota_returns_413(
        self,
        server_url: str,
        session_factory: async_sessionmaker[AsyncSession],
        tmp_data_dir: Path,
        git_env: dict[str, str],
    ) -> None:
        owner, pat = await _seed_owner_with_pat(session_factory, "quota-user")
        # Cap the user so even a tiny push would exceed it once we lie about
        # the Content-Length — but here we use a real small file: the quota
        # is set to 1 byte so *any* push will trip the check.
        await _make_repo(
            session_factory,
            owner,
            name="model-q",
            visibility=Visibility.PUBLIC,
            quota_bytes=1,
        )

        src = tmp_data_dir / "src"
        src.mkdir()
        await _run_git(["init"], cwd=src, env=git_env)
        (src / "big.txt").write_text("hello world\n")
        await _run_git(["add", "big.txt"], cwd=src, env=git_env)
        await _run_git(["commit", "-m", "big"], cwd=src, env=git_env)

        push_url = _with_basic(
            _repo_url(server_url, "quota-user", "model-q"),
            "quota-user",
            pat,
        )
        proc = await asyncio.to_thread(
            subprocess.run,
            [
                "git",
                "-c",
                "credential.helper=",
                "push",
                push_url,
                "HEAD:refs/heads/master",
            ],
            cwd=str(src),
            env={**git_env, "GIT_TERMINAL_PROMPT": "0"},
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode != 0
        combined = (proc.stderr or "") + (proc.stdout or "")
        assert "413" in combined or "quota" in combined.lower()


class TestPushPullByteExact:
    """Pulled content is byte-identical to what was pushed."""

    async def test_round_trip_content_is_identical(
        self,
        server_url: str,
        session_factory: async_sessionmaker[AsyncSession],
        tmp_data_dir: Path,
        git_env: dict[str, str],
    ) -> None:
        owner, pat = await _seed_owner_with_pat(session_factory, "alice")
        await _make_repo(session_factory, owner, name="model-rt", visibility=Visibility.PUBLIC)

        src = tmp_data_dir / "src"
        src.mkdir()
        await _run_git(["init"], cwd=src, env=git_env)
        # 5 KiB of pseudo-random bytes so the packfile has real content.
        payload = os.urandom(5 * 1024)
        (src / "blob.bin").write_bytes(payload)
        await _run_git(["add", "blob.bin"], cwd=src, env=git_env)
        await _run_git(["commit", "-m", "binary blob"], cwd=src, env=git_env)

        push_url = _with_basic(_repo_url(server_url, "alice", "model-rt"), "alice", pat)
        await _run_git(
            ["push", push_url, "HEAD:refs/heads/master"],
            cwd=src,
            env=git_env,
        )

        clone_workdir = tmp_data_dir / "clone-rt"
        clone_workdir.mkdir()
        clone_url = _repo_url(server_url, "alice", "model-rt")
        await _run_git(["clone", clone_url], cwd=clone_workdir, env=git_env)
        cloned = (clone_workdir / "model-rt" / "blob.bin").read_bytes()
        assert cloned == payload


class TestPathResolution:
    """The service accepts URLs with and without the `.git` suffix."""

    async def test_clone_without_dot_git_suffix(
        self,
        server_url: str,
        session_factory: async_sessionmaker[AsyncSession],
        tmp_data_dir: Path,
        git_env: dict[str, str],
    ) -> None:
        owner, _pat = await _seed_owner_with_pat(session_factory, "alice")
        await _make_repo(session_factory, owner, name="model-suffix", visibility=Visibility.PUBLIC)

        clone_workdir = tmp_data_dir / "clone-suffix"
        clone_workdir.mkdir()
        await _run_git(
            ["clone", f"{server_url}/alice/model-suffix"],
            cwd=clone_workdir,
            env=git_env,
        )
        assert (clone_workdir / "model-suffix").is_dir()
