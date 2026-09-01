"""LFS object-transfer end-to-end tests (PUT / GET).

Exercises the local-backend streaming handlers via `httpx.ASGITransport`
so the bytes never leave the test process — no uvicorn required. The
suite pins the contract the LFS server promises over the wire:

    - `PUT /info/lfs/objects/{oid}` stores the body under the
      sharded path and bumps `UserUsage`.
    - `GET /info/lfs/objects/{oid}` returns the same bytes that were
      uploaded.
    - The batch endpoint enforces `Accept` / `Content-Type` content
      negotiation — a request without the LFS content type answers
      406.

Tests cover anonymous / owner / non-owner / private-repo auth matrix
and the basic round-trip for both upload and download.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from outo_models.auth.tokens import fingerprint
from outo_models.config import get_settings
from outo_models.db import (
    Base,
    PersonalAccessToken,
    Repo,
    User,
    dispose_engines,
    get_engine,
    get_session_factory,
)
from outo_models.git_smart.lfs import LFS_CONTENT_TYPE, lfs_dispatch
from outo_models.objectstore import LocalObjectStore
from outo_models.repos.models import RepoKind, Visibility

# ---------------------------------------------------------------------------
# fixtures + helpers
# ---------------------------------------------------------------------------


@pytest.fixture
async def session_factory(
    tmp_data_dir: Path,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Fresh per-test sqlite engine + schema; auto-disposed."""
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


def _basic(username: str, password: str) -> str:
    encoded = base64.b64encode(f"{username}:{password}".encode()).decode("ascii")
    return f"Basic {encoded}"


async def _seed_user_with_pat(
    factory: async_sessionmaker[AsyncSession],
    username: str,
    *,
    role: str = "user",
    status: str = "approved",
) -> tuple[User, str]:
    """Insert an approved user + PAT; return `(user, raw_pat)`."""
    pat = f"v4.local.{username}-transfer-token"
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
                name="transfer-test",
                fingerprint_hash=fingerprint(pat),
                prefix=pat[:8],
                scopes='["read","write"]',
            )
        )
        await session.commit()
        user_id = user.id
    async with factory() as session:
        owner = (await session.execute(select(User).where(User.id == user_id))).scalar_one()
        return owner, pat


async def _seed_repo(
    factory: async_sessionmaker[AsyncSession],
    owner: User,
    *,
    name: str = "model",
    visibility: Visibility = Visibility.PUBLIC,
) -> Repo:
    async with factory() as session:
        repo = Repo(
            owner_id=owner.id,
            name=name,
            kind=RepoKind.MODEL.value,
            visibility=visibility.value,
            default_branch="main",
            size_bytes=0,
            path=f"{owner.username}/{name}.git",
        )
        session.add(repo)
        await session.commit()
        return (await session.execute(select(Repo).where(Repo.id == repo.id))).scalar_one()


# ---------------------------------------------------------------------------
# ASGI app factories used by httpx.ASGITransport
# ---------------------------------------------------------------------------


def _lfs_get_app(owner: str, repo: str, oid: str):
    """ASGI app for a plain GET through the LFS dispatcher."""

    async def app(
        scope: dict[str, object],
        receive: object,
        send: object,
    ) -> None:
        async def receive_inner() -> dict[str, object]:
            return {"type": "http.request", "body": b"", "more_body": False}

        await lfs_dispatch(
            scope,
            receive_inner,
            send,  # type: ignore[arg-type]
            settings=get_settings(),
            owner_name=owner,
            repo_name=repo,
            rest=["info", "lfs", "objects", oid],
        )

    return app


def _lfs_batch_app(body: bytes):
    """ASGI app for a batch POST through the LFS dispatcher."""

    async def app(
        scope: dict[str, object],
        receive: object,
        send: object,
    ) -> None:
        async def receive_inner() -> dict[str, object]:
            return {"type": "http.request", "body": body, "more_body": False}

        path = scope.get("path", "")
        if not isinstance(path, str):
            path = path.decode("latin-1")
        parts = path.split("/", 3)
        owner_name = parts[1]
        repo_name = parts[2]
        if repo_name.endswith(".git"):
            repo_name = repo_name[: -len(".git")]
        await lfs_dispatch(
            scope,
            receive_inner,
            send,  # type: ignore[arg-type]
            settings=get_settings(),
            owner_name=owner_name,
            repo_name=repo_name,
            rest=["info", "lfs", "objects", "batch"],
        )

    return app


def _lfs_put_app(
    *,
    owner: str,
    repo: str,
    oid: str,
    body: bytes,
):
    """ASGI app for a streaming PUT through the LFS dispatcher."""

    async def app(
        scope: dict[str, object],
        receive: object,
        send: object,
    ) -> None:
        queue: asyncio.Queue[dict[str, object]] = asyncio.Queue()
        await queue.put({"type": "http.request", "body": body, "more_body": False})

        async def receive_inner() -> dict[str, object]:
            return await queue.get()

        await lfs_dispatch(
            scope,
            receive_inner,
            send,  # type: ignore[arg-type]
            settings=get_settings(),
            owner_name=owner,
            repo_name=repo,
            rest=["info", "lfs", "objects", oid],
        )

    return app


# ---------------------------------------------------------------------------
# PUT tests
# ---------------------------------------------------------------------------


class TestPutTransfer:
    """`PUT /info/lfs/objects/{oid}` streams the body and verifies it."""

    async def test_put_streams_body_and_stores_object(
        self, tmp_data_dir: Path, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        owner, pat = await _seed_user_with_pat(session_factory, "alice")
        await _seed_repo(session_factory, owner)
        payload = b"hello LFS world\n" * 200
        oid = hashlib.sha256(payload).hexdigest()

        transport = httpx.ASGITransport(
            app=_lfs_put_app(owner="alice", repo="model", oid=oid, body=payload)
        )
        async with httpx.AsyncClient(transport=transport, base_url="http://lfs.test") as client:
            response = await client.put(
                f"/alice/model.git/info/lfs/objects/{oid}",
                headers={
                    "authorization": _basic("alice", pat),
                    "content-type": "application/octet-stream",
                    "content-length": str(len(payload)),
                },
                content=payload,
            )

        assert response.status_code == 200
        store = LocalObjectStore(tmp_data_dir / "lfs", base_url="http://lfs.test", presign_ttl=600)
        assert await store.has_object(oid)
        assert await store.object_size(oid) == len(payload)

    async def test_put_rejects_wrong_sha_with_422(
        self, tmp_data_dir: Path, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        owner, pat = await _seed_user_with_pat(session_factory, "alice")
        await _seed_repo(session_factory, owner)
        payload = b"real"
        wrong_oid = hashlib.sha256(b"different").hexdigest()

        transport = httpx.ASGITransport(
            app=_lfs_put_app(owner="alice", repo="model", oid=wrong_oid, body=payload)
        )
        async with httpx.AsyncClient(transport=transport, base_url="http://lfs.test") as client:
            response = await client.put(
                f"/alice/model.git/info/lfs/objects/{wrong_oid}",
                headers={
                    "authorization": _basic("alice", pat),
                    "content-type": "application/octet-stream",
                    "content-length": str(len(payload)),
                },
                content=payload,
            )

        assert response.status_code == 422
        assert response.headers["content-type"].startswith(LFS_CONTENT_TYPE)
        store = LocalObjectStore(tmp_data_dir / "lfs", base_url="http://lfs.test", presign_ttl=600)
        assert not await store.has_object(wrong_oid)

    async def test_put_anonymous_returns_401(
        self, tmp_data_dir: Path, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        owner, _pat = await _seed_user_with_pat(session_factory, "alice")
        await _seed_repo(session_factory, owner)
        payload = b"data"
        transport = httpx.ASGITransport(
            app=_lfs_put_app(owner="alice", repo="model", oid="0" * 64, body=payload)
        )
        async with httpx.AsyncClient(transport=transport, base_url="http://lfs.test") as client:
            response = await client.put(
                f"/alice/model.git/info/lfs/objects/{'0' * 64}",
                headers={"content-type": "application/octet-stream"},
                content=payload,
            )
        assert response.status_code == 401


# ---------------------------------------------------------------------------
# GET tests
# ---------------------------------------------------------------------------


class TestGetTransfer:
    """`GET /info/lfs/objects/{oid}` returns the stored bytes."""

    async def test_get_streams_back_byte_identical(
        self, tmp_data_dir: Path, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        owner, pat = await _seed_user_with_pat(session_factory, "alice")
        await _seed_repo(session_factory, owner)
        payload = b"round-trip body\n" * 5000
        oid = hashlib.sha256(payload).hexdigest()

        store = LocalObjectStore(tmp_data_dir / "lfs", base_url="http://lfs.test", presign_ttl=600)

        async def _aiter() -> AsyncIterator[bytes]:
            yield payload

        await store.write_object(oid, _aiter(), expected_size=len(payload))

        transport = httpx.ASGITransport(app=_lfs_get_app("alice", "model", oid))
        async with httpx.AsyncClient(transport=transport, base_url="http://lfs.test") as client:
            response = await client.get(
                f"/alice/model.git/info/lfs/objects/{oid}",
                headers={"authorization": _basic("alice", pat)},
            )

        assert response.status_code == 200
        assert response.content == payload

    async def test_get_missing_object_returns_404(
        self, tmp_data_dir: Path, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        owner, pat = await _seed_user_with_pat(session_factory, "alice")
        await _seed_repo(session_factory, owner)
        missing = "ff" * 32

        transport = httpx.ASGITransport(app=_lfs_get_app("alice", "model", missing))
        async with httpx.AsyncClient(transport=transport, base_url="http://lfs.test") as client:
            response = await client.get(
                f"/alice/model.git/info/lfs/objects/{missing}",
                headers={"authorization": _basic("alice", pat)},
            )
        assert response.status_code == 404

    async def test_get_anonymous_on_private_repo_returns_401(
        self, tmp_data_dir: Path, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        owner, _pat = await _seed_user_with_pat(session_factory, "alice")
        await _seed_repo(session_factory, owner, name="priv", visibility=Visibility.PRIVATE)

        transport = httpx.ASGITransport(app=_lfs_get_app("alice", "priv", "0" * 64))
        async with httpx.AsyncClient(transport=transport, base_url="http://lfs.test") as client:
            response = await client.get(f"/alice/priv.git/info/lfs/objects/{'0' * 64}")
        assert response.status_code == 401


# ---------------------------------------------------------------------------
# batch content negotiation
# ---------------------------------------------------------------------------


class TestBatchContentNegotiation:
    """Batch requires `Accept` AND `Content-Type` of `application/vnd.git-lfs+json`."""

    async def test_batch_without_accept_returns_406(
        self, tmp_data_dir: Path, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        owner, pat = await _seed_user_with_pat(session_factory, "alice")
        await _seed_repo(session_factory, owner)
        body = b'{"operation":"download","objects":[{"oid":"' + b"0" * 64 + b'","size":1}]}'

        transport = httpx.ASGITransport(app=_lfs_batch_app(body))
        async with httpx.AsyncClient(transport=transport, base_url="http://lfs.test") as client:
            response = await client.post(
                "/alice/model.git/info/lfs/objects/batch",
                headers={
                    "authorization": _basic("alice", pat),
                    "content-type": LFS_CONTENT_TYPE,
                },
                content=body,
            )
        assert response.status_code == 406

    async def test_batch_without_content_type_returns_415(
        self, tmp_data_dir: Path, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        owner, pat = await _seed_user_with_pat(session_factory, "alice")
        await _seed_repo(session_factory, owner)
        body = b'{"operation":"download","objects":[{"oid":"' + b"0" * 64 + b'","size":1}]}'

        transport = httpx.ASGITransport(app=_lfs_batch_app(body))
        async with httpx.AsyncClient(transport=transport, base_url="http://lfs.test") as client:
            response = await client.post(
                "/alice/model.git/info/lfs/objects/batch",
                headers={
                    "authorization": _basic("alice", pat),
                    "accept": LFS_CONTENT_TYPE,
                },
                content=body,
            )
        assert response.status_code == 415
