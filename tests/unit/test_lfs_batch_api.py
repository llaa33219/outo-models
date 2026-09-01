"""Unit tests for the Git LFS batch handler logic.

Two layers covered here:

    1. Pure helpers in `outo_models.git_smart.lfs_api`
       (`parse_batch_body`, `dedup_objects`) — no I/O.
    2. Per-object decisions in `handle_batch` (dedup of existing
       objects, per-object 413 quota / size errors, per-object 404 on
       download-missing, content-type enforcement).

The full ASGI surface (auth, body buffering, JSON envelope) is covered
in `test_lfs_transfer.py` and `test_lfs_flow.py`.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from outo_models.auth.tokens import fingerprint
from outo_models.config import get_settings
from outo_models.db import (
    Base,
    PersonalAccessToken,
    Repo,
    User,
    UserQuota,
    UserUsage,
    dispose_engines,
    get_engine,
    get_session_factory,
)
from outo_models.git_smart.lfs_api import (
    BatchObjectRequest,
    dedup_objects,
    handle_batch,
    parse_batch_body,
)
from outo_models.objectstore import LocalObjectStore
from outo_models.repos.models import RepoKind, Visibility

# ----------------------------------------------------------------------------------------
# fixtures
# ----------------------------------------------------------------------------------------


@pytest.fixture
async def session_factory(
    tmp_data_dir: Path,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Per-test sqlite-backed engine + schema; auto-disposed."""
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


async def _seed_user(
    factory: async_sessionmaker[AsyncSession],
    username: str,
    *,
    quota_bytes: int | None = None,
    used_bytes: int = 0,
) -> User:
    """Insert an approved user with quota + usage rows; return the row."""
    from outo_models.repos.quota import ensure_quota_rows

    async with factory() as session:
        user = User(
            username=username,
            email=f"{username}@example.com",
            password_hash="h",
            role="user",
            status="approved",
        )
        session.add(user)
        await session.commit()
        user_id = user.id
    async with factory() as session:
        owner = (
            await session.execute(select(User).where(User.id == user_id))
        ).scalar_one()
        await ensure_quota_rows(session, owner)
        if quota_bytes is not None:
            quota = (
                await session.execute(
                    select(UserQuota).where(UserQuota.user_id == owner.id)
                )
            ).scalar_one()
            quota.max_bytes = quota_bytes
            usage = (
                await session.execute(
                    select(UserUsage).where(UserUsage.user_id == owner.id)
                )
            ).scalar_one()
            usage.used_bytes = used_bytes
            await session.commit()
        async with factory() as session2:
            return (
                await session2.execute(select(User).where(User.id == user_id))
            ).scalar_one()


async def _mint_pat(
    factory: async_sessionmaker[AsyncSession], user: User, raw: str
) -> None:
    async with factory() as session:
        session.add(
            PersonalAccessToken(
                user_id=user.id,
                name="lfs-test",
                fingerprint_hash=fingerprint(raw),
                prefix=raw[:8],
                scopes='["read","write"]',
            )
        )
        await session.commit()


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
        return (
            await session.execute(select(Repo).where(Repo.id == repo.id))
        ).scalar_one()


from sqlalchemy import select  # noqa: E402  (placed after use in fixtures)


def _store(tmp_path: Path) -> LocalObjectStore:
    root = tmp_path / "lfs"
    root.mkdir(exist_ok=True)
    return LocalObjectStore(root, base_url="http://lfs.test", presign_ttl=600)


def _oid(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


async def _aiter_bytes(data: bytes, chunk: int = 4096) -> AsyncIterator[bytes]:
    for i in range(0, len(data), chunk):
        yield data[i : i + chunk]


# ----------------------------------------------------------------------------------------
# parse_batch_body
# ----------------------------------------------------------------------------------------


class TestParseBatchBody:
    """Body parsing: malformed input → ValidationFailedError (HTTP 422)."""

    def test_upload_round_trips(self) -> None:
        req = parse_batch_body(
            json.dumps(
                {
                    "operation": "upload",
                    "transfers": ["basic"],
                    "objects": [{"oid": "ab" * 32, "size": 12}],
                }
            ).encode("utf-8")
        )
        assert req.operation == "upload"
        assert len(req.objects) == 1
        assert req.objects[0].oid == "ab" * 32
        assert req.objects[0].size == 12

    def test_download_round_trips(self) -> None:
        req = parse_batch_body(
            json.dumps(
                {
                    "operation": "download",
                    "objects": [{"oid": "cd" * 32, "size": 99}],
                }
            ).encode("utf-8")
        )
        assert req.operation == "download"
        assert req.transfers == ["basic"]  # default

    def test_rejects_non_basic_transfer(self) -> None:
        from outo_models.exceptions import ValidationFailedError

        with pytest.raises(ValidationFailedError):
            parse_batch_body(
                json.dumps(
                    {
                        "operation": "upload",
                        "transfers": ["custom"],
                        "objects": [{"oid": "ab" * 32, "size": 1}],
                    }
                ).encode("utf-8")
            )

    def test_rejects_bad_operation(self) -> None:
        from outo_models.exceptions import ValidationFailedError

        with pytest.raises(ValidationFailedError):
            parse_batch_body(
                json.dumps(
                    {
                        "operation": "push",
                        "objects": [{"oid": "ab" * 32, "size": 1}],
                    }
                ).encode("utf-8")
            )

    def test_rejects_bad_oid_length(self) -> None:
        from outo_models.exceptions import ValidationFailedError

        with pytest.raises(ValidationFailedError):
            parse_batch_body(
                json.dumps(
                    {
                        "operation": "upload",
                        "objects": [{"oid": "ab", "size": 1}],
                    }
                ).encode("utf-8")
            )

    def test_rejects_invalid_json(self) -> None:
        from outo_models.exceptions import ValidationFailedError

        with pytest.raises(ValidationFailedError):
            parse_batch_body(b"{not json")


# ----------------------------------------------------------------------------------------
# dedup_objects
# ----------------------------------------------------------------------------------------


class TestDedupObjects:
    """Duplicates of the same (oid, size) collapse to one entry."""

    def test_collapses_exact_duplicates(self) -> None:
        objs = [
            BatchObjectRequest(oid="ab" * 32, size=10),
            BatchObjectRequest(oid="ab" * 32, size=10),
            BatchObjectRequest(oid="cd" * 32, size=20),
        ]
        out = dedup_objects(objs)
        assert len(out) == 2

    def test_keeps_same_oid_at_different_sizes(self) -> None:
        objs = [
            BatchObjectRequest(oid="ab" * 32, size=10),
            BatchObjectRequest(oid="ab" * 32, size=20),
        ]
        out = dedup_objects(objs)
        assert len(out) == 2


# ----------------------------------------------------------------------------------------
# handle_batch — per-object decisions
# ----------------------------------------------------------------------------------------


class TestHandleBatchUpload:
    """Upload batch: dedup, size-limit, quota-limit, present-objects skip."""

    async def test_present_object_entry_has_no_actions(
        self, tmp_path: Path, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        store = _store(tmp_path)
        owner = await _seed_user(session_factory, "alice")
        await _seed_repo(session_factory, owner)

        # Pre-store one object so the upload is a no-op for it.
        existing_payload = b"already-there"
        existing_oid = _oid(existing_payload)
        await store.write_object(
            existing_oid, _aiter_bytes(existing_payload), expected_size=len(existing_payload)
        )

        # Second object will need a real upload.
        new_payload = b"to-upload"
        new_oid = _oid(new_payload)

        req = parse_batch_body(
            json.dumps(
                {
                    "operation": "upload",
                    "objects": [
                        {"oid": existing_oid, "size": len(existing_payload)},
                        {"oid": new_oid, "size": len(new_payload)},
                    ],
                }
            ).encode("utf-8")
        )

        async with session_factory() as session:
            response = await handle_batch(
                request=req,
                store=store,
                owner_name="alice",
                repo_name="model",
                actor_id=owner.id,
                session=session,
                settings=get_settings(),
            )

        assert len(response.objects) == 2

        existing_entry = response.objects[0]
        new_entry = response.objects[1]
        assert existing_entry.oid == existing_oid
        assert existing_entry.actions is None  # present → no actions

        assert new_entry.oid == new_oid
        assert new_entry.actions is not None
        assert "upload" in new_entry.actions
        upload_action = new_entry.actions["upload"]
        assert upload_action["href"].endswith(f"/objects/{new_oid}")
        assert upload_action["expires_in"] == 600

    async def test_per_object_size_limit_emits_413(
        self,
        tmp_path: Path,
        session_factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("OUTO_LFS_MAX_OBJECT_BYTES", "10")
        get_settings.cache_clear()
        try:
            store = _store(tmp_path)
            owner = await _seed_user(session_factory, "alice")
            await _seed_repo(session_factory, owner)

            big_payload = b"x" * 11
            big_oid = _oid(big_payload)
            small_payload = b"ok"
            small_oid = _oid(small_payload)

            req = parse_batch_body(
                json.dumps(
                    {
                        "operation": "upload",
                        "objects": [
                            {"oid": big_oid, "size": len(big_payload)},
                            {"oid": small_oid, "size": len(small_payload)},
                        ],
                    }
                ).encode("utf-8")
            )
            async with session_factory() as session:
                response = await handle_batch(
                    request=req,
                    store=store,
                    owner_name="alice",
                    repo_name="model",
                    actor_id=owner.id,
                    session=session,
                    settings=get_settings(),
                )

            assert len(response.objects) == 2

            big = next(o for o in response.objects if o.oid == big_oid)
            assert big.actions is None
            assert big.error == {"code": 413, "message": big.error["message"]}

            small = next(o for o in response.objects if o.oid == small_oid)
            assert small.error is None
            assert small.actions is not None
        finally:
            monkeypatch.delenv("OUTO_LFS_MAX_OBJECT_BYTES", raising=False)
            get_settings.cache_clear()

    async def test_per_object_quota_limit_emits_413(
        self, tmp_path: Path, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        store = _store(tmp_path)
        owner = await _seed_user(
            session_factory, "alice", quota_bytes=1000, used_bytes=500
        )
        await _seed_repo(session_factory, owner)

        objs = [
            BatchObjectRequest(oid=_oid(b"a" * 400), size=400),
            BatchObjectRequest(oid=_oid(b"b" * 600), size=600),
            BatchObjectRequest(oid=_oid(b"c" * 700), size=700),
        ]

        from outo_models.git_smart.lfs_api import BatchRequest

        request = BatchRequest(operation="upload", objects=objs)
        async with session_factory() as session:
            response = await handle_batch(
                request=request,
                store=store,
                owner_name="alice",
                repo_name="model",
                actor_id=owner.id,
                session=session,
                settings=get_settings(),
            )

        assert len(response.objects) == 3
        outcomes = [o.error for o in response.objects]
        assert outcomes[0] is None
        assert outcomes[1] is not None and outcomes[1]["code"] == 413
        assert outcomes[2] is not None and outcomes[2]["code"] == 413


class TestHandleBatchDownload:
    """Download batch: present objects get a download action; missing → 404."""

    async def test_missing_object_emits_404_per_object(
        self, tmp_path: Path, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        store = _store(tmp_path)
        owner = await _seed_user(session_factory, "alice")
        await _seed_repo(session_factory, owner)

        present_payload = b"present"
        present_oid = _oid(present_payload)
        await store.write_object(
            present_oid, _aiter_bytes(present_payload), expected_size=len(present_payload)
        )
        missing_oid = "ee" * 32

        req = parse_batch_body(
            json.dumps(
                {
                    "operation": "download",
                    "objects": [
                        {"oid": present_oid, "size": len(present_payload)},
                        {"oid": missing_oid, "size": 99},
                    ],
                }
            ).encode("utf-8")
        )
        async with session_factory() as session:
            response = await handle_batch(
                request=req,
                store=store,
                owner_name="alice",
                repo_name="model",
                actor_id=None,  # anonymous OK for public download
                session=session,
                settings=get_settings(),
            )

        assert len(response.objects) == 2
        present = next(o for o in response.objects if o.oid == present_oid)
        missing = next(o for o in response.objects if o.oid == missing_oid)
        assert present.actions is not None and "download" in present.actions
        assert missing.error == {"code": 404, "message": "object not found"}


# ----------------------------------------------------------------------------------------
# auth matrix (the authorize() gate) — covered here because the
# dispatcher delegates auth decisions to `outo_models.git_smart.auth`.
# ----------------------------------------------------------------------------------------


class TestAuthMatrix:
    """Authorize decisions the LFS dispatcher relies on.

    These tests live here so the policy lives next to the policy-
    consuming helpers, even though the actual gate is in
    `outo_models.git_smart.auth`.
    """

    async def test_anonymous_download_of_public_repo_allowed(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        from outo_models.git_smart.auth import GitAction, authorize

        owner = await _seed_user(session_factory, "alice")
        repo = await _seed_repo(
            session_factory, owner, visibility=Visibility.PUBLIC
        )
        # No user → must not raise.
        await authorize(None, repo=repo, owner=owner, action=GitAction.PULL)

    async def test_anonymous_upload_raises_unauthorized(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        from outo_models.exceptions import UnauthorizedError
        from outo_models.git_smart.auth import GitAction, authorize

        owner = await _seed_user(session_factory, "alice")
        repo = await _seed_repo(
            session_factory, owner, visibility=Visibility.PUBLIC
        )
        with pytest.raises(UnauthorizedError):
            await authorize(None, repo=repo, owner=owner, action=GitAction.PUSH)

    async def test_non_owner_push_raises_forbidden(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        from outo_models.exceptions import ForbiddenError
        from outo_models.git_smart.auth import GitAction, authorize

        owner = await _seed_user(session_factory, "alice")
        intruder = await _seed_user(session_factory, "mallory")
        repo = await _seed_repo(
            session_factory, owner, visibility=Visibility.PUBLIC
        )
        with pytest.raises(ForbiddenError):
            await authorize(intruder, repo=repo, owner=owner, action=GitAction.PUSH)