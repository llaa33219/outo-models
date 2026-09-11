"""Integration tests for the upload endpoint.

Contract under test:

    POST /api/repos/{owner}/{name}/upload
        multipart/form-data:
            - message (optional)
            - path (optional, default "")
            - files[] (one or more file parts)

Coverage:

    * Happy path: the file lands in the repo; a fresh `git clone`
      through the git service shows the file (reuses the existing
      git_smart test harness from `test_git_smart_http.py`).
    * Auth matrix: anon 401, non-owner 403.
    * Traversal 422 (`..`, absolute).
    * Empty file list 422.
    * Quota 413 (synthetic low cap).
    * Empty repo first commit works.
    * Per-file size cap 413 with the git+LFS hint.
    * Revision + AuditLog rows are written.
    * Repo.size_bytes + UserUsage are updated.

The harness pattern mirrors `test_git_smart_http._seed_owner_with_pat`:
seed an approved user + raw PAT, create a bare repo via the API,
then drive the upload endpoint directly with the HTTPX test client.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

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
# fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture
async def session_factory(
    tmp_data_dir: Path,
) -> Iterator[async_sessionmaker[AsyncSession]]:
    """Fresh per-test sqlite engine + schema; auto-disposed on exit."""
    await dispose_engines()
    settings = get_settings()
    engine = get_engine(settings)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = get_session_factory(engine)
    try:
        yield factory
    finally:
        await engine.dispose()
        await dispose_engines()


async def _seed_user_with_pat(
    factory: async_sessionmaker[AsyncSession],
    *,
    username: str,
    role: str = "user",
    pat: str | None = None,
    pat_scopes: str = '["read","write","repos:read","repos:write"]',
    quota_bytes: int = 10 * 1024**3,
) -> tuple[User, str | None]:
    """Insert an approved user with optional PAT + quota rows.

    Returns `(user, pat_or_None)`.
    """
    async with factory() as session:
        user = User(
            username=username,
            email=f"{username}@example.com",
            password_hash="h",
            role=role,
            status="approved",
        )
        session.add(user)
        await session.flush()
        if pat:
            session.add(
                PersonalAccessToken(
                    user_id=user.id,
                    name="integration",
                    fingerprint_hash=fingerprint(pat),
                    prefix=pat[:8],
                    scopes=pat_scopes,
                )
            )
        session.add(UserQuota(user_id=user.id, max_bytes=quota_bytes))
        session.add(UserUsage(user_id=user.id, used_bytes=0))
        await session.commit()
        user_id = user.id
    async with factory() as session:
        return (
            (await session.execute(select(User).where(User.id == user_id))).scalar_one(),
            pat,
        )


async def _make_repo(
    factory: async_sessionmaker[AsyncSession],
    owner: User,
    *,
    name: str,
    visibility: Visibility = Visibility.PRIVATE,
) -> int:
    """Create a bare repo via the API; return the repo id."""
    async with factory() as session:
        owner_row = (await session.execute(select(User).where(User.id == owner.id))).scalar_one()
        repo = await create_repo(
            session,
            owner=owner_row,
            name=name,
            kind=RepoKind.MODEL,
            visibility=visibility,
        )
        await session.commit()
        return repo.id


@pytest.fixture
def git_env(tmp_path: Path) -> dict[str, str]:
    """Per-test environment for the real `git` CLI used in the round-trip test."""
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_AUTHOR_NAME"] = "Tester"
    env["GIT_AUTHOR_EMAIL"] = "tester@example.com"
    env["GIT_COMMITTER_NAME"] = "Tester"
    env["GIT_COMMITTER_EMAIL"] = "tester@example.com"
    env.pop("GIT_CONFIG_GLOBAL", None)
    env.pop("GIT_CONFIG_NOSYSTEM", None)
    env["HOME"] = str(tmp_path / "home")
    (tmp_path / "home").mkdir()
    return env


@pytest.fixture
def server_url(
    tmp_data_dir: Path,
    session_factory: async_sessionmaker[AsyncSession],
) -> Iterator[str]:
    """Spin `GitSmartService` under uvicorn on an ephemeral TCP port.

    Mirrors `test_git_smart_http.server_url`; reused to prove that an
    upload-then-clone round-trip produces the expected file in the
    git service.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(2048)
    port = sock.getsockname()[1]

    settings = get_settings()
    service = GitSmartService(settings)
    app = service.asgi_app()

    from uvicorn import Config, Server

    config = Config(app, log_level="warning", access_log=False)
    server = Server(config=config)

    async def _serve() -> None:
        await server.serve(sockets=[sock])

    thread = threading.Thread(target=asyncio.run, args=(_serve(),), daemon=True)
    thread.start()

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
            raise RuntimeError("uvicorn thread did not exit within 5 s")
        sock.close()


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------


class TestUploadHappyPath:
    """A single-file upload lands in the repo and the round-trip clones cleanly."""

    async def test_upload_writes_file_into_default_branch(
        self,
        app: tuple[TestClient, FastAPI, object],
        session_factory: async_sessionmaker[AsyncSession],
        tmp_data_dir: Path,
    ) -> None:
        client, _, _ = app
        owner, pat = await _seed_user_with_pat(
            session_factory, username="alice", pat="v4.local.alice"
        )
        await _make_repo(session_factory, owner, name="upload-happy")

        response = client.post(
            "/api/repos/alice/upload-happy/upload",
            headers={"Authorization": f"Bearer {pat}"},
            data={"message": "add weights"},
            files={"files": ("weights.bin", b"abcd" * 1024, "application/octet-stream")},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["files"] == ["weights.bin"]
        assert body["message"] == "add weights"
        assert len(body["commit_sha"]) == 40

        # Verify the file actually landed in the bare repo on disk.
        from dulwich.repo import Repo as _DulwichRepo

        from outo_models.repos.storage import repo_fs_path

        bare = _DulwichRepo(str(repo_fs_path("alice", "upload-happy")))
        tip = bare.refs[b"refs/heads/main"]
        commit = bare[tip]
        tree = bare[commit.tree]
        entries = {entry.path.decode(): entry.sha for entry in tree.items()}
        assert b"weights.bin" in entries or "weights.bin" in entries
        blob_sha = entries.get("weights.bin") or entries.get(b"weights.bin")
        blob = bare[blob_sha]
        assert blob.data == b"abcd" * 1024

    async def test_upload_then_git_clone_round_trip(
        self,
        app: tuple[TestClient, FastAPI, object],
        session_factory: async_sessionmaker[AsyncSession],
        tmp_data_dir: Path,
        server_url: str,
        git_env: dict[str, str],
    ) -> None:
        """Upload a file, then a fresh `git clone` retrieves it through git service."""
        client, _, _ = app
        owner, pat = await _seed_user_with_pat(
            session_factory, username="alice", pat="v4.local.alice"
        )
        await _make_repo(
            session_factory,
            owner,
            name="upload-roundtrip",
            visibility=Visibility.PUBLIC,
        )

        payload = os.urandom(5 * 1024)
        response = client.post(
            "/api/repos/alice/upload-roundtrip/upload",
            headers={"Authorization": f"Bearer {pat}"},
            data={"message": "init"},
            files={"files": ("payload.bin", payload, "application/octet-stream")},
        )
        assert response.status_code == 200, response.text

        # Fresh anonymous clone through the real git service must yield
        # the same bytes we uploaded.
        clone_workdir = tmp_data_dir / "clone-uploaded"
        clone_workdir.mkdir()
        url = f"{server_url}/alice/upload-roundtrip.git"
        proc = await asyncio.to_thread(
            subprocess.run,
            ["git", "clone", url],
            cwd=str(clone_workdir),
            env=git_env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0, proc.stderr
        cloned = (clone_workdir / "upload-roundtrip" / "payload.bin").read_bytes()
        assert cloned == payload

    async def test_upload_writes_revision_and_audit_rows(
        self,
        app: tuple[TestClient, FastAPI, object],
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        client, _, _ = app
        owner, pat = await _seed_user_with_pat(
            session_factory, username="alice", pat="v4.local.alice"
        )
        repo_id = await _make_repo(session_factory, owner, name="upload-bookkeep")

        response = client.post(
            "/api/repos/alice/upload-bookkeep/upload",
            headers={"Authorization": f"Bearer {pat}"},
            data={"message": "two files"},
            files=[
                ("files", ("a.txt", b"alpha", "text/plain")),
                ("files", ("b.txt", b"beta", "text/plain")),
            ],
        )
        assert response.status_code == 200, response.text

        async with session_factory() as session:
            revs = (
                (await session.execute(select(Revision).where(Revision.repo_id == repo_id)))
                .scalars()
                .all()
            )
            assert len(revs) == 1
            assert revs[0].message == "two files"
            assert revs[0].author_id == owner.id
            assert len(revs[0].commit_sha) == 40

            audits = (
                (await session.execute(select(AuditLog).where(AuditLog.action == "repo.upload")))
                .scalars()
                .all()
            )
            assert len(audits) == 1
            detail = json.loads(audits[0].detail or "{}")
            assert detail["repo"] == "alice/upload-bookkeep"
            assert sorted(detail["files"]) == ["a.txt", "b.txt"]
            assert detail["bytes"] == len(b"alpha") + len(b"beta")

            repo_row = (await session.execute(select(Repo).where(Repo.id == repo_id))).scalar_one()
            assert repo_row.size_bytes > 0

            usage = (
                await session.execute(select(UserUsage).where(UserUsage.user_id == owner.id))
            ).scalar_one()
            assert usage.used_bytes > 0


class TestUploadAuth:
    """Auth matrix: anon 401, non-owner 403, admin 200."""

    async def test_anonymous_upload_returns_401(
        self,
        app: tuple[TestClient, FastAPI, object],
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        client, _, _ = app
        owner, _ = await _seed_user_with_pat(session_factory, username="alice")
        await _make_repo(session_factory, owner, name="upload-anon")

        response = client.post(
            "/api/repos/alice/upload-anon/upload",
            data={"message": "anon"},
            files={"files": ("a.txt", b"x", "text/plain")},
        )
        assert response.status_code == 401

    async def test_non_owner_upload_returns_403(
        self,
        app: tuple[TestClient, FastAPI, object],
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        client, _, _ = app
        owner, _ = await _seed_user_with_pat(session_factory, username="alice")
        _mallory, mallory_pat = await _seed_user_with_pat(
            session_factory, username="mallory", pat="v4.local.mallory"
        )
        await _make_repo(session_factory, owner, name="upload-priv")

        response = client.post(
            "/api/repos/alice/upload-priv/upload",
            headers={"Authorization": f"Bearer {mallory_pat}"},
            data={"message": "intruder"},
            files={"files": ("a.txt", b"x", "text/plain")},
        )
        assert response.status_code == 403

    async def test_admin_can_upload_to_any_repo(
        self,
        app: tuple[TestClient, FastAPI, object],
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        client, _, _ = app
        owner, _ = await _seed_user_with_pat(session_factory, username="alice")
        _admin, admin_pat = await _seed_user_with_pat(
            session_factory,
            username="admin",
            role="admin",
            pat="v4.local.admin",
        )
        await _make_repo(session_factory, owner, name="upload-admin")

        response = client.post(
            "/api/repos/alice/upload-admin/upload",
            headers={"Authorization": f"Bearer {admin_pat}"},
            data={"message": "admin override"},
            files={"files": ("a.txt", b"x", "text/plain")},
        )
        assert response.status_code == 200, response.text

    async def test_readonly_pat_is_rejected(
        self,
        app: tuple[TestClient, FastAPI, object],
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        client, _, _ = app
        owner, pat = await _seed_user_with_pat(
            session_factory,
            username="alice",
            pat="v4.local.readonly",
            pat_scopes='["read"]',
        )
        await _make_repo(session_factory, owner, name="upload-readonly")

        response = client.post(
            "/api/repos/alice/upload-readonly/upload",
            headers={"Authorization": f"Bearer {pat}"},
            data={"message": "denied"},
            files={"files": ("a.txt", b"x", "text/plain")},
        )
        assert response.status_code == 401

    async def test_missing_repo_returns_404(
        self,
        app: tuple[TestClient, FastAPI, object],
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        client, _, _ = app
        _owner, pat = await _seed_user_with_pat(
            session_factory, username="alice", pat="v4.local.alice"
        )

        response = client.post(
            "/api/repos/alice/no-such-repo/upload",
            headers={"Authorization": f"Bearer {pat}"},
            data={"message": "missing"},
            files={"files": ("a.txt", b"x", "text/plain")},
        )
        assert response.status_code == 404


class TestUploadValidation:
    """422 for bad input shape, 413 for per-file / quota overflow."""

    async def test_empty_file_list_returns_422(
        self,
        app: tuple[TestClient, FastAPI, object],
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        client, _, _ = app
        owner, pat = await _seed_user_with_pat(
            session_factory, username="alice", pat="v4.local.alice"
        )
        await _make_repo(session_factory, owner, name="upload-empty")

        response = client.post(
            "/api/repos/alice/upload-empty/upload",
            headers={"Authorization": f"Bearer {pat}"},
            data={"message": "no files"},
        )
        assert response.status_code == 422

    async def test_path_traversal_in_filename_returns_422(
        self,
        app: tuple[TestClient, FastAPI, object],
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        client, _, _ = app
        owner, pat = await _seed_user_with_pat(
            session_factory, username="alice", pat="v4.local.alice"
        )
        await _make_repo(session_factory, owner, name="upload-traversal")

        response = client.post(
            "/api/repos/alice/upload-traversal/upload",
            headers={"Authorization": f"Bearer {pat}"},
            files={"files": ("../escape.txt", b"x", "text/plain")},
        )
        assert response.status_code == 422

    async def test_absolute_path_in_prefix_returns_422(
        self,
        app: tuple[TestClient, FastAPI, object],
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        client, _, _ = app
        owner, pat = await _seed_user_with_pat(
            session_factory, username="alice", pat="v4.local.alice"
        )
        await _make_repo(session_factory, owner, name="upload-abs")

        response = client.post(
            "/api/repos/alice/upload-abs/upload",
            headers={"Authorization": f"Bearer {pat}"},
            data={"path": "/etc"},
            files={"files": ("a.txt", b"x", "text/plain")},
        )
        assert response.status_code == 422

    async def test_per_file_size_cap_returns_413(
        self,
        app: tuple[TestClient, FastAPI, object],
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        from outo_models.server.routers.upload import MAX_FILE_BYTES

        client, _, _ = app
        owner, pat = await _seed_user_with_pat(
            session_factory, username="alice", pat="v4.local.alice"
        )
        await _make_repo(session_factory, owner, name="upload-huge")

        # Build a payload that's bigger than the cap.
        big = b"x" * (MAX_FILE_BYTES + 1)
        response = client.post(
            "/api/repos/alice/upload-huge/upload",
            headers={"Authorization": f"Bearer {pat}"},
            files={"files": ("huge.bin", big, "application/octet-stream")},
        )
        assert response.status_code == 413
        assert "lfs" in response.text.lower()

    async def test_quota_exceeded_returns_413(
        self,
        app: tuple[TestClient, FastAPI, object],
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        client, _, _ = app
        # Cap the user at 1 byte so even the smallest upload trips the check.
        owner, pat = await _seed_user_with_pat(
            session_factory,
            username="quota-user",
            pat="v4.local.quota",
            quota_bytes=1,
        )
        await _make_repo(session_factory, owner, name="upload-overquota")

        response = client.post(
            "/api/repos/quota-user/upload-overquota/upload",
            headers={"Authorization": f"Bearer {pat}"},
            files={"files": ("a.txt", b"x" * 1024, "text/plain")},
        )
        assert response.status_code == 413
        assert "quota" in response.text.lower()


class TestEmptyRepoUpload:
    """An empty repo's first commit is created via upload."""

    async def test_first_commit_on_empty_repo(
        self,
        app: tuple[TestClient, FastAPI, object],
        session_factory: async_sessionmaker[AsyncSession],
        tmp_data_dir: Path,
    ) -> None:
        client, _, _ = app
        owner, pat = await _seed_user_with_pat(
            session_factory, username="alice", pat="v4.local.alice"
        )
        await _make_repo(session_factory, owner, name="upload-empty-repo")

        response = client.post(
            "/api/repos/alice/upload-empty-repo/upload",
            headers={"Authorization": f"Bearer {pat}"},
            data={"message": "first commit"},
            files={"files": ("hello.txt", b"first", "text/plain")},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["files"] == ["hello.txt"]

        # The bare repo now has a main branch with one commit.
        from dulwich.repo import Repo as _DulwichRepo

        from outo_models.repos.storage import repo_fs_path

        bare = _DulwichRepo(str(repo_fs_path("alice", "upload-empty-repo")))
        assert bare.refs.read_ref(b"refs/heads/main") is not None


class TestRepeatedUploadsSameRepo:
    """Second-and-later uploads must fast-forward, not DivergedBranches-500
    (field failure: every upload after the first one failed with HTTP 500)."""

    async def test_two_uploads_both_succeed(
        self,
        app: tuple[TestClient, FastAPI, object],
        session_factory: async_sessionmaker[AsyncSession],
        tmp_data_dir: Path,
    ) -> None:
        client, _, _ = app
        owner, pat = await _seed_user_with_pat(
            session_factory, username="repeat", pat="v4.local.repeat"
        )
        await _make_repo(session_factory, owner, name="twice")
        headers = {"Authorization": f"Bearer {pat}"}
        first = client.post(
            "/api/repos/repeat/twice/upload",
            files={"files": ("a.txt", b"first")},
            data={"message": "one"},
            headers=headers,
        )
        assert first.status_code == 200, first.text
        second = client.post(
            "/api/repos/repeat/twice/upload",
            files={"files": ("b.txt", b"second")},
            data={"message": "two"},
            headers=headers,
        )
        assert second.status_code == 200, second.text

        from sqlalchemy import select

        from outo_models.db.models import Revision

        async with session_factory() as session:
            revs = (await session.execute(select(Revision).order_by(Revision.id))).scalars().all()
            assert len(revs) == 2
            assert revs[1].commit_sha != revs[0].commit_sha
