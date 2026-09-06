"""Integration tests for the resolve raw-file endpoint.

Contract under test:

    GET /{owner}/{name}/resolve/{revision}/{path:path}

Coverage:

    * Public file download is byte-exact (anon and owner alike).
    * `Range: bytes=start-end` returns 206 with the exact window.
    * Invalid Range returns 416.
    * LFS pointer files redirect (302) to the existing LFS GET endpoint.
    * Private-repo auth matrix: anon 404, owner-PAT 200, stranger-PAT 404.
    * ETag, Content-Length, Content-Type, Accept-Ranges are present on 200.
    * Streaming works for a 500 MiB blob (range window byte-exact).

The fixture pattern mirrors `test_ui_repo_page._seed_bare_repo_with_readme`:
build a working tree with the desired files, `porcelain.init` +
`porcelain.add` + `porcelain.commit`, then `porcelain.clone --bare` into
the bare-repo slot the API created. This exercises the exact on-disk
state the resolve endpoint reads.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest
from dulwich import porcelain
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from outo_models.auth.tokens import fingerprint
from outo_models.config import get_settings
from outo_models.db import (
    Base,
    PersonalAccessToken,
    User,
    UserQuota,
    UserUsage,
    dispose_engines,
    get_engine,
    get_session_factory,
)
from outo_models.repos.create import create_repo
from outo_models.repos.models import RepoKind, Visibility
from outo_models.repos.storage import repo_fs_path

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


async def _seed_owner(
    factory: async_sessionmaker[AsyncSession],
    *,
    username: str,
    pat: str | None = None,
    pat_scopes: str = '["read","write","repos:read","repos:write"]',
    role: str = "user",
) -> tuple[User, str | None]:
    """Insert an approved user; optionally attach a PAT.

    Returns `(user, raw_pat_or_None)`. `pat` is the plaintext value the
    bearer header must carry.
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
        await session.commit()
        user_id = user.id
    async with factory() as session:
        row = (await session.execute(select(User).where(User.id == user_id))).scalar_one()
        # Default quota rows for users who own repos.
        session.add(UserQuota(user_id=user_id, max_bytes=10 * 1024**3))
        session.add(UserUsage(user_id=user_id, used_bytes=0))
        await session.commit()
        return row, pat


async def _make_repo(
    factory: async_sessionmaker[AsyncSession],
    owner: User,
    *,
    name: str,
    visibility: Visibility = Visibility.PUBLIC,
) -> int:
    """Create a bare repo for `owner`; return the repo id."""
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


def _seed_bare_repo(
    tmp_data_dir: Path,
    *,
    owner: str,
    name: str,
    files: dict[str, bytes],
    branch: str = "main",
) -> None:
    """Replace the bare-repo slot with one populated by `files`.

    `files` is a `{path: content}` mapping; the working tree is
    committed and pushed into the bare repo's default branch.
    """
    work = tmp_data_dir / "_resolve_work"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir()
    for rel_path, content in files.items():
        target = work / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    porcelain.init(str(work))
    porcelain.add(str(work), paths=list(files.keys()))
    porcelain.commit(
        str(work),
        message=b"resolve-test seed",
        author=b"seed <seed@example.com>",
        committer=b"seed <seed@example.com>",
        env={"GIT_AUTHOR_NAME": "seed", "GIT_AUTHOR_EMAIL": "seed@example.com"},
    )
    bare = repo_fs_path(owner, name)
    bare.parent.mkdir(parents=True, exist_ok=True)
    if bare.exists():
        shutil.rmtree(bare)
    porcelain.clone(str(work), str(bare), bare=True)
    # dulwich ≥1.2 initializes `main`; older versions default to `master`.
    # Seed into the actual default branch the bare repo ended up with.
    active = porcelain.active_branch(str(work)).decode("ascii")
    if active != branch:
        # Re-clone into bare + push to the target branch name so
        # resolve_tip_sha finds it.
        refspec = f"refs/heads/{active}:refs/heads/{branch}".encode()
        porcelain.push(str(work), str(bare), refspec, force=True)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestPublicResolve:
    """Anonymous callers can resolve files in public repos."""

    async def test_public_file_download_byte_exact(
        self,
        app: tuple[TestClient, FastAPI, object],
        session_factory: async_sessionmaker[AsyncSession],
        tmp_data_dir: Path,
    ) -> None:
        client, _, _ = app
        owner, _ = await _seed_owner(session_factory, username="alice")
        await _make_repo(session_factory, owner, name="public-resolve")
        content = b"hello world\nsecond line\n"
        _seed_bare_repo(
            tmp_data_dir,
            owner="alice",
            name="public-resolve",
            files={"greeting.txt": content},
        )

        response = client.get("/alice/public-resolve/resolve/main/greeting.txt")
        assert response.status_code == 200
        assert response.content == content
        assert response.headers["Accept-Ranges"] == "bytes"
        assert int(response.headers["Content-Length"]) == len(content)
        # `.txt` is in the content-type table.
        assert response.headers["content-type"].startswith("text/plain")
        # ETag is the blob's git sha1 in quotes.
        assert response.headers["ETag"].startswith('"')
        assert response.headers["ETag"].endswith('"')

    async def test_markdown_file_uses_text_markdown_content_type(
        self,
        app: tuple[TestClient, FastAPI, object],
        session_factory: async_sessionmaker[AsyncSession],
        tmp_data_dir: Path,
    ) -> None:
        client, _, _ = app
        owner, _ = await _seed_owner(session_factory, username="alice")
        await _make_repo(session_factory, owner, name="md-resolve")
        _seed_bare_repo(
            tmp_data_dir,
            owner="alice",
            name="md-resolve",
            files={"README.md": b"# title\n\nbody\n"},
        )

        response = client.get("/alice/md-resolve/resolve/main/README.md")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/markdown")


class TestRangeRequests:
    """`Range: bytes=start-end` returns a 206 with the exact byte window."""

    async def test_partial_range_returns_206_with_exact_window(
        self,
        app: tuple[TestClient, FastAPI, object],
        session_factory: async_sessionmaker[AsyncSession],
        tmp_data_dir: Path,
    ) -> None:
        client, _, _ = app
        owner, _ = await _seed_owner(session_factory, username="alice")
        await _make_repo(session_factory, owner, name="range-resolve")
        content = b"abcdefghij" * 10  # 100 bytes
        _seed_bare_repo(
            tmp_data_dir,
            owner="alice",
            name="range-resolve",
            files={"blob.bin": content},
        )

        response = client.get(
            "/alice/range-resolve/resolve/main/blob.bin",
            headers={"Range": "bytes=10-19"},
        )
        assert response.status_code == 206
        assert response.content == content[10:20]
        assert response.headers["Content-Range"] == "bytes 10-19/100"
        assert int(response.headers["Content-Length"]) == 10

    async def test_open_ended_range_returns_to_end(
        self,
        app: tuple[TestClient, FastAPI, object],
        session_factory: async_sessionmaker[AsyncSession],
        tmp_data_dir: Path,
    ) -> None:
        client, _, _ = app
        owner, _ = await _seed_owner(session_factory, username="alice")
        await _make_repo(session_factory, owner, name="open-range")
        content = b"X" * 256
        _seed_bare_repo(
            tmp_data_dir,
            owner="alice",
            name="open-range",
            files={"big.bin": content},
        )

        response = client.get(
            "/alice/open-range/resolve/main/big.bin",
            headers={"Range": "bytes=200-"},
        )
        assert response.status_code == 206
        assert response.content == content[200:]
        assert response.headers["Content-Range"] == "bytes 200-255/256"

    async def test_invalid_range_returns_416(
        self,
        app: tuple[TestClient, FastAPI, object],
        session_factory: async_sessionmaker[AsyncSession],
        tmp_data_dir: Path,
    ) -> None:
        client, _, _ = app
        owner, _ = await _seed_owner(session_factory, username="alice")
        await _make_repo(session_factory, owner, name="invalid-range")
        _seed_bare_repo(
            tmp_data_dir,
            owner="alice",
            name="invalid-range",
            files={"a.txt": b"x" * 10},
        )

        # start beyond the size is unsatisfiable.
        response = client.get(
            "/alice/invalid-range/resolve/main/a.txt",
            headers={"Range": "bytes=9999-99999"},
        )
        assert response.status_code == 416
        assert response.headers["Content-Range"] == "bytes */10"

    async def test_range_window_matches_a_large_blob(
        self,
        app: tuple[TestClient, FastAPI, object],
        session_factory: async_sessionmaker[AsyncSession],
        tmp_data_dir: Path,
    ) -> None:
        """A 500 MiB blob must stream and serve a Range window byte-exact."""
        client, _, _ = app
        owner, _ = await _seed_owner(session_factory, username="alice")
        await _make_repo(session_factory, owner, name="huge-blob")
        # ~500 MiB of pseudo-random bytes so the pack file is non-trivial.
        size = 500 * 1024 * 1024
        chunk = os.urandom(size)
        # Write directly into the seed working tree to avoid pulling
        # 500 MiB through the in-memory string API.
        work = tmp_data_dir / "_huge_work"
        if work.exists():
            shutil.rmtree(work)
        work.mkdir()
        target = work / "huge.bin"
        with target.open("wb") as fh:
            fh.write(chunk)
        porcelain.init(str(work))
        porcelain.add(str(work), paths=["huge.bin"])
        porcelain.commit(
            str(work),
            message=b"huge",
            author=b"seed <seed@example.com>",
            committer=b"seed <seed@example.com>",
            env={"GIT_AUTHOR_NAME": "seed", "GIT_AUTHOR_EMAIL": "seed@example.com"},
        )
        bare = repo_fs_path("alice", "huge-blob")
        bare.parent.mkdir(parents=True, exist_ok=True)
        if bare.exists():
            shutil.rmtree(bare)
        porcelain.clone(str(work), str(bare), bare=True)

        # Range: last 1 MiB of a 500 MiB blob.
        start = size - 1024 * 1024
        end = size - 1
        response = client.get(
            "/alice/huge-blob/resolve/main/huge.bin",
            headers={"Range": f"bytes={start}-{end}"},
        )
        assert response.status_code == 206
        assert response.headers["Content-Range"] == f"bytes {start}-{end}/{size}"
        assert int(response.headers["Content-Length"]) == end - start + 1
        assert response.content == chunk[start : end + 1]


class TestLfsPointerRedirect:
    """LFS pointer files redirect (302) to the existing LFS GET endpoint."""

    async def test_lfs_pointer_redirects_to_lfs_get(
        self,
        app: tuple[TestClient, FastAPI, object],
        session_factory: async_sessionmaker[AsyncSession],
        tmp_data_dir: Path,
    ) -> None:
        client, _, _ = app
        owner, _ = await _seed_owner(session_factory, username="alice")
        await _make_repo(
            session_factory,
            owner,
            name="lfs-pointer",
            visibility=Visibility.PUBLIC,
        )
        oid = "a" * 64
        size = 12345
        pointer = (
            b"version https://git-lfs.github.com/spec/v1\n"
            b"oid sha256:" + oid.encode() + b"\n"
            b"size " + str(size).encode() + b"\n"
        )
        _seed_bare_repo(
            tmp_data_dir,
            owner="alice",
            name="lfs-pointer",
            files={"weights.bin": pointer},
        )

        response = client.get(
            "/alice/lfs-pointer/resolve/main/weights.bin",
            follow_redirects=False,
        )
        assert response.status_code == 302
        assert response.headers["Location"] == (f"/alice/lfs-pointer.git/info/lfs/objects/{oid}")


class TestRevisionResolution:
    """`revision` accepts branch names, tags, and bare SHAs."""

    async def test_bare_sha_resolves_to_commit(
        self,
        app: tuple[TestClient, FastAPI, object],
        session_factory: async_sessionmaker[AsyncSession],
        tmp_data_dir: Path,
    ) -> None:
        client, _, _ = app
        owner, _ = await _seed_owner(session_factory, username="alice")
        await _make_repo(session_factory, owner, name="sha-resolve", visibility=Visibility.PUBLIC)
        content = b"sha lookup target"
        _seed_bare_repo(
            tmp_data_dir,
            owner="alice",
            name="sha-resolve",
            files={"f.txt": content},
        )

        # Look up the SHA via the refs the bare repo stores.
        from dulwich.repo import Repo as _DulwichRepo

        bare = _DulwichRepo(str(repo_fs_path("alice", "sha-resolve")))
        sha = bare.refs[b"refs/heads/main"].decode("ascii")

        response = client.get(f"/alice/sha-resolve/resolve/{sha}/f.txt")
        assert response.status_code == 200
        assert response.content == content

    async def test_missing_revision_returns_404(
        self,
        app: tuple[TestClient, FastAPI, object],
        session_factory: async_sessionmaker[AsyncSession],
        tmp_data_dir: Path,
    ) -> None:
        client, _, _ = app
        owner, _ = await _seed_owner(session_factory, username="alice")
        await _make_repo(session_factory, owner, name="missing-rev")
        _seed_bare_repo(
            tmp_data_dir,
            owner="alice",
            name="missing-rev",
            files={"f.txt": b"x"},
        )

        response = client.get(
            "/alice/missing-rev/resolve/no-such-branch/f.txt",
        )
        assert response.status_code == 404


class TestPrivateRepoAuth:
    """Private-repo auth matrix: anon 404, owner 200, stranger 404."""

    async def test_anonymous_on_private_repo_returns_404(
        self,
        app: tuple[TestClient, FastAPI, object],
        session_factory: async_sessionmaker[AsyncSession],
        tmp_data_dir: Path,
    ) -> None:
        client, _, _ = app
        owner, _ = await _seed_owner(session_factory, username="alice")
        await _make_repo(session_factory, owner, name="priv-1", visibility=Visibility.PRIVATE)
        _seed_bare_repo(
            tmp_data_dir,
            owner="alice",
            name="priv-1",
            files={"secret.txt": b"top secret"},
        )

        # Anonymous
        response = client.get("/alice/priv-1/resolve/main/secret.txt")
        assert response.status_code == 404

    async def test_owner_pat_on_private_repo_serves_file(
        self,
        app: tuple[TestClient, FastAPI, object],
        session_factory: async_sessionmaker[AsyncSession],
        tmp_data_dir: Path,
    ) -> None:
        import base64

        client, _, _ = app
        owner, pat = await _seed_owner(session_factory, username="alice", pat="v4.local.alice-pat")
        await _make_repo(session_factory, owner, name="priv-2", visibility=Visibility.PRIVATE)
        _seed_bare_repo(
            tmp_data_dir,
            owner="alice",
            name="priv-2",
            files={"secret.txt": b"top secret"},
        )

        basic = base64.b64encode(f"alice:{pat}".encode()).decode()
        response = client.get(
            "/alice/priv-2/resolve/main/secret.txt",
            headers={"Authorization": f"Basic {basic}"},
        )
        assert response.status_code == 200
        assert response.content == b"top secret"

    async def test_stranger_pat_on_private_repo_returns_404(
        self,
        app: tuple[TestClient, FastAPI, object],
        session_factory: async_sessionmaker[AsyncSession],
        tmp_data_dir: Path,
    ) -> None:
        import base64

        client, _, _ = app
        owner, _ = await _seed_owner(session_factory, username="alice")
        _intruder, intruder_pat = await _seed_owner(
            session_factory, username="mallory", pat="v4.local.mallory-pat"
        )
        await _make_repo(session_factory, owner, name="priv-3", visibility=Visibility.PRIVATE)
        _seed_bare_repo(
            tmp_data_dir,
            owner="alice",
            name="priv-3",
            files={"secret.txt": b"top secret"},
        )
        del _intruder  # only the PAT matters for this test

        basic = base64.b64encode(f"mallory:{intruder_pat}".encode()).decode()
        response = client.get(
            "/alice/priv-3/resolve/main/secret.txt",
            headers={"Authorization": f"Basic {basic}"},
        )
        assert response.status_code == 404

    async def test_missing_private_repo_returns_404_for_anon(
        self,
        app: tuple[TestClient, FastAPI, object],
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        client, _, _ = app
        # No repo seeded — anon must see 404 without leaking existence.
        response = client.get("/ghost/nonexistent/resolve/main/f.txt")
        assert response.status_code == 404


class TestPathTraversal:
    """Path traversal is rejected with 404 (does not escape the repo)."""

    async def test_dotdot_segment_returns_404(
        self,
        app: tuple[TestClient, FastAPI, object],
        session_factory: async_sessionmaker[AsyncSession],
        tmp_data_dir: Path,
    ) -> None:
        client, _, _ = app
        owner, _ = await _seed_owner(session_factory, username="alice")
        await _make_repo(session_factory, owner, name="traversal")
        _seed_bare_repo(
            tmp_data_dir,
            owner="alice",
            name="traversal",
            files={"safe.txt": b"ok"},
        )

        response = client.get("/alice/traversal/resolve/main/../etc/passwd")
        assert response.status_code == 404
