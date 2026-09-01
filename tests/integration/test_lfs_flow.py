"""End-to-end LFS integration test through the full FastAPI app.

Spins `create_app()` under `uvicorn` on an ephemeral TCP port, then
drives the LFS protocol over the wire via `httpx`:

    - create user + PAT + repo directly in the DB;
    - POST batch (upload) → 200 with one action per object;
    - PUT the object bytes through the action href;
    - POST batch (download) → 200 with one action per object;
    - GET the bytes through the action href and assert byte-for-byte
      equality with what was uploaded;
    - when the user is over quota, the affected object gets a 413
      error entry while the rest of the batch still succeeds.

The `git-lfs` binary is not installed on this dev machine, so the test
talks the LFS wire protocol directly via `httpx`. Run budget: < 30 s
on a workstation.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import socket
import threading
import time
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from uvicorn import Config, Server

from outo_models.auth.passwords import hash_password
from outo_models.auth.tokens import fingerprint
from outo_models.config import get_settings
from outo_models.db import (
    PersonalAccessToken,
    Repo,
    User,
    UserQuota,
    UserUsage,
    dispose_engines,
    get_engine,
    get_session_factory,
)
from outo_models.objectstore import LocalObjectStore
from outo_models.repos.create import create_repo
from outo_models.repos.models import RepoKind, Visibility
from outo_models.server import create_app
from outo_models.utils.paths import lfs_dir

LFS_CT = "application/vnd.git-lfs+json"


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def live_lfs_server(
    tmp_data_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[str]:
    """Boot the FastAPI app on an ephemeral port; yield base URL.

    The lifespan creates the schema via Alembic; tests then seed via
    the session factory without pre-creating any tables themselves.

    `OUTO_DOMAIN=127.0.0.1` is forced so the LFS action hrefs the
    server hands back are anchored on the same IPv4 loopback the test
    is bound to (rather than `localhost`, which resolves to `::1` on
    this machine and would refuse the connection).
    """
    monkeypatch.setenv("OUTO_SECRET_KEY", "test-secret-key-for-lfs-integration")
    monkeypatch.setenv("OUTO_DOMAIN", "127.0.0.1")
    get_settings.cache_clear()
    from outo_models.auth.rate_limit import limiter

    limiter.reset()

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(2048)
    port = sock.getsockname()[1]

    settings = get_settings()
    fastapi_app = create_app(settings)

    config = Config(fastapi_app, log_level="warning", access_log=False)
    server = Server(config=config)

    async def _serve() -> None:
        await server.serve(sockets=[sock])

    thread = threading.Thread(target=asyncio.run, args=(_serve(),), daemon=True)
    thread.start()

    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if server.started:
            break
        time.sleep(0.02)
    else:  # pragma: no cover - defensive
        raise RuntimeError("uvicorn did not start within 10 s")

    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        if thread.is_alive():  # pragma: no cover - defensive
            raise RuntimeError("uvicorn thread did not exit within 10 s")
        sock.close()
        limiter.reset()
        get_settings.cache_clear()


@pytest.fixture
async def session_factory(
    tmp_data_dir: Path,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Fresh per-test sqlite engine bound to the same tmp dir the server uses.

    The server's lifespan creates the schema; we re-use the same engine
    so seeding shares the DB the request handlers see.
    """
    await dispose_engines()
    settings = get_settings()
    engine: AsyncEngine = get_engine(settings)
    factory = get_session_factory(engine)
    try:
        yield factory
    finally:
        await engine.dispose()
        await dispose_engines()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _basic(username: str, password: str) -> str:
    encoded = base64.b64encode(f"{username}:{password}".encode()).decode("ascii")
    return f"Basic {encoded}"


async def _seed_owner_with_pat_and_repo(
    factory: async_sessionmaker[AsyncSession],
    *,
    username: str,
    repo_name: str = "model",
    visibility: Visibility = Visibility.PUBLIC,
    quota_bytes: int | None = None,
    used_bytes: int = 0,
) -> tuple[User, str, Repo]:
    """Insert a user + PAT + repo directly via the ORM.

    Bypasses the public signup / repo-create endpoints so the test
    owns every byte of state; the routes under test (the LFS
    dispatcher) are still exercised end-to-end via httpx.
    """
    raw_pat = f"v4.local.{username}-lfs-integration-token"
    async with factory() as session:
        user = User(
            username=username,
            email=f"{username}@example.com",
            password_hash=hash_password("correct horse battery staple"),
            role="user",
            status="approved",
        )
        session.add(user)
        await session.flush()
        session.add(
            PersonalAccessToken(
                user_id=user.id,
                name="integration",
                fingerprint_hash=fingerprint(raw_pat),
                prefix=raw_pat[:8],
                scopes='["read","write","repos:read","repos:write"]',
            )
        )
        owner_for_repo = (
            await session.execute(select(User).where(User.id == user.id))
        ).scalar_one()
        repo = await create_repo(
            session,
            owner=owner_for_repo,
            name=repo_name,
            kind=RepoKind.MODEL,
            visibility=visibility,
        )
        await session.commit()
        user_id = user.id
        repo_id = repo.id

    async with factory() as session:
        quota = (
            await session.execute(select(UserQuota).where(UserQuota.user_id == user_id))
        ).scalar_one()
        if quota_bytes is not None:
            quota.max_bytes = quota_bytes
        usage = (
            await session.execute(select(UserUsage).where(UserUsage.user_id == user_id))
        ).scalar_one()
        usage.used_bytes = used_bytes
        await session.commit()

    async with factory() as session:
        owner = (await session.execute(select(User).where(User.id == user_id))).scalar_one()
        repo_row = (await session.execute(select(Repo).where(Repo.id == repo_id))).scalar_one()
        return owner, raw_pat, repo_row


def _oid(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------


class TestFullLfsRoundTrip:
    """Batch upload → PUT object → batch download → GET object — byte-for-byte."""

    async def test_upload_then_download_round_trip(
        self,
        live_lfs_server: str,
        session_factory: async_sessionmaker[AsyncSession],
        tmp_data_dir: Path,
    ) -> None:
        owner, pat, _repo = await _seed_owner_with_pat_and_repo(session_factory, username="alice")

        payload = os.urandom(2 * 1024 * 1024)  # 2 MiB
        oid = _oid(payload)
        store_root = lfs_dir()

        async with httpx.AsyncClient(base_url=live_lfs_server, timeout=15.0) as client:
            # Upload batch.
            upload_resp = await client.post(
                f"/{owner.username}/model.git/info/lfs/objects/batch",
                headers={
                    "accept": LFS_CT,
                    "content-type": LFS_CT,
                    "authorization": _basic(owner.username, pat),
                },
                json={
                    "operation": "upload",
                    "transfers": ["basic"],
                    "objects": [{"oid": oid, "size": len(payload)}],
                },
            )
            assert upload_resp.status_code == 200, upload_resp.text
            upload_body = upload_resp.json()
            assert upload_body["transfer"] == "basic"
            assert len(upload_body["objects"]) == 1
            upload_entry = upload_body["objects"][0]
            assert upload_entry["oid"] == oid
            assert "actions" in upload_entry
            upload_action = upload_entry["actions"]["upload"]

            # PUT the bytes through the href the server returned. The
            # href is a same-origin URL, so we re-anchor the client on it.
            put_url = upload_action["href"]
            put_resp = await client.put(
                put_url,
                headers={
                    "authorization": _basic(owner.username, pat),
                    "content-type": "application/octet-stream",
                    "content-length": str(len(payload)),
                },
                content=payload,
            )
            assert put_resp.status_code == 200, put_resp.text

            # The store must contain the bytes at the sharded path.
            store = LocalObjectStore(store_root, base_url=live_lfs_server, presign_ttl=600)
            assert await store.has_object(oid)
            assert await store.object_size(oid) == len(payload)

            # Download batch.
            download_resp = await client.post(
                f"/{owner.username}/model.git/info/lfs/objects/batch",
                headers={
                    "accept": LFS_CT,
                    "content-type": LFS_CT,
                    "authorization": _basic(owner.username, pat),
                },
                json={
                    "operation": "download",
                    "transfers": ["basic"],
                    "objects": [{"oid": oid, "size": len(payload)}],
                },
            )
            assert download_resp.status_code == 200, download_resp.text
            dl_body = download_resp.json()
            assert len(dl_body["objects"]) == 1
            dl_entry = dl_body["objects"][0]
            assert dl_entry["oid"] == oid
            assert "actions" in dl_entry
            dl_action = dl_entry["actions"]["download"]

            # GET the bytes and assert byte-for-byte equality.
            get_resp = await client.get(
                dl_action["href"],
                headers={"authorization": _basic(owner.username, pat)},
            )
            assert get_resp.status_code == 200
            assert get_resp.content == payload


class TestQuotaExceededPartialSuccess:
    """One over-quota object gets an error entry while siblings succeed."""

    async def test_over_quota_object_errors_others_succeed(
        self,
        live_lfs_server: str,
        session_factory: async_sessionmaker[AsyncSession],
        tmp_data_dir: Path,
    ) -> None:
        # Cap the user so the first object fits but the second does not.
        owner, pat, _repo = await _seed_owner_with_pat_and_repo(
            session_factory,
            username="quota-user",
            quota_bytes=1000,
            used_bytes=0,
        )

        small_payload = b"a" * 100
        big_payload = b"b" * 2000
        small_oid = _oid(small_payload)
        big_oid = _oid(big_payload)

        async with httpx.AsyncClient(base_url=live_lfs_server, timeout=15.0) as client:
            upload_resp = await client.post(
                f"/{owner.username}/model.git/info/lfs/objects/batch",
                headers={
                    "accept": LFS_CT,
                    "content-type": LFS_CT,
                    "authorization": _basic(owner.username, pat),
                },
                json={
                    "operation": "upload",
                    "transfers": ["basic"],
                    "objects": [
                        {"oid": small_oid, "size": len(small_payload)},
                        {"oid": big_oid, "size": len(big_payload)},
                    ],
                },
            )
            assert upload_resp.status_code == 200
            body = upload_resp.json()
            assert len(body["objects"]) == 2

            entries = {o["oid"]: o for o in body["objects"]}

            # The small one gets a `basic` upload action.
            assert "actions" in entries[small_oid]
            assert "upload" in entries[small_oid]["actions"]

            # The big one gets a per-object 413 error — the whole batch
            # still returns 200 because per-spec failures are per-object.
            assert "error" in entries[big_oid]
            assert entries[big_oid]["error"]["code"] == 413

            # PUT the small object's bytes — it must succeed end-to-end.
            put_resp = await client.put(
                entries[small_oid]["actions"]["upload"]["href"],
                headers={
                    "authorization": _basic(owner.username, pat),
                    "content-type": "application/octet-stream",
                    "content-length": str(len(small_payload)),
                },
                content=small_payload,
            )
            assert put_resp.status_code == 200, put_resp.text

            store = LocalObjectStore(lfs_dir(), base_url=live_lfs_server, presign_ttl=600)
            assert await store.has_object(small_oid)
            # The over-quota object must NOT have landed.
            assert not await store.has_object(big_oid)
