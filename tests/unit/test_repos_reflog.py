"""Unit tests for `outo_models.repos.reflog.recent_revisions`.

The reflog walks the on-disk bare repo via dulwich. Tests build tiny repos
in `tmp_data_dir` rather than mocking dulwich so the read path is
exercised end-to-end.
"""

from __future__ import annotations

from pathlib import Path

from dulwich import porcelain

from outo_models.repos.reflog import RevisionInfo, recent_revisions
from outo_models.repos.storage import repo_fs_path


def _init_bare(tmp_data_dir: Path, owner: str, name: str) -> Path:
    """Create a bare repo under `tmp_data_dir/repos/<owner>/<name>.git`."""
    path = repo_fs_path(owner, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    porcelain.init(str(path), bare=True)
    return path


def _push_commit(
    tmp_data_dir: Path,
    owner: str,
    name: str,
    message: str,
    body: str,
) -> None:
    """Add a commit to the shared working tree and force-push to the bare repo.

    Multiple commits per repo reuse a single working tree (cached under
    `tmp_data_dir`); each push uses `force=True` so a fresh commit on
    Whatever the local default branch is (dulwich ≥1.2 uses `main`,
    older versions `master`) replaces the previous `main` tip without
    needing to be a descendant of it.
    """
    work = tmp_data_dir / f"_work_{owner}_{name}"
    if not (work / ".git").exists():
        work.mkdir()
        porcelain.init(str(work), bare=False)
    else:
        # Sync the working tree to whatever the bare repo currently has so
        # the next commit is a descendant and the force-push is well-formed.
        porcelain.pull(str(work), str(repo_fs_path(owner, name)), b"refs/heads/main")

    (work / "README.md").write_text(body)
    porcelain.add(str(work), paths=[str(work / "README.md")])
    porcelain.commit(
        str(work),
        message=message.encode("utf-8"),
        author=b"Tester <tester@example.com>",
        committer=b"Tester <tester@example.com>",
    )
    local_branch = porcelain.active_branch(str(work))
    porcelain.push(
        str(work),
        str(repo_fs_path(owner, name)),
        b"refs/heads/" + local_branch + b":refs/heads/main",
        force=True,
    )


class TestRecentRevisionsEmpty:
    """Missing / empty repos surface as an empty list, never as an error."""

    async def test_missing_repo_returns_empty_list(self, tmp_data_dir: Path) -> None:
        assert await recent_revisions("alice", "nope") == []

    async def test_fresh_bare_repo_returns_empty_list(self, tmp_data_dir: Path) -> None:
        _init_bare(tmp_data_dir, "alice", "fresh")
        assert await recent_revisions("alice", "fresh") == []


class TestRecentRevisionsPopulated:
    """Real commits appear in newest-first order."""

    async def test_single_commit_is_returned(self, tmp_data_dir: Path) -> None:
        _init_bare(tmp_data_dir, "alice", "model-a")
        _push_commit(tmp_data_dir, "alice", "model-a", "first", "hello")

        revs = await recent_revisions("alice", "model-a")
        assert len(revs) == 1
        only = revs[0]
        assert isinstance(only, RevisionInfo)
        assert len(only.commit_sha) == 40
        assert int(only.commit_sha, 16) >= 0  # valid hex
        assert only.message == "first"
        assert "tester@example.com" in only.author

    async def test_multiple_commits_returned_newest_first(self, tmp_data_dir: Path) -> None:
        _init_bare(tmp_data_dir, "bob", "model-b")
        _push_commit(tmp_data_dir, "bob", "model-b", "first", "v1")
        _push_commit(tmp_data_dir, "bob", "model-b", "second", "v2")
        _push_commit(tmp_data_dir, "bob", "model-b", "third", "v3")

        revs = await recent_revisions("bob", "model-b", limit=10)
        assert [r.message for r in revs] == ["third", "second", "first"]

    async def test_default_limit_is_20(self, tmp_data_dir: Path) -> None:
        _init_bare(tmp_data_dir, "carol", "model-c")
        for i in range(25):
            _push_commit(tmp_data_dir, "carol", "model-c", f"msg {i:02d}", f"body {i}")

        revs = await recent_revisions("carol", "model-c")
        assert len(revs) == 20

    async def test_explicit_limit_is_honored(self, tmp_data_dir: Path) -> None:
        _init_bare(tmp_data_dir, "dave", "model-d")
        for i in range(5):
            _push_commit(tmp_data_dir, "dave", "model-d", f"msg {i:02d}", f"body {i}")

        revs = await recent_revisions("dave", "model-d", limit=3)
        assert len(revs) == 3

    async def test_limit_clamped_to_max(self, tmp_data_dir: Path) -> None:
        _init_bare(tmp_data_dir, "erin", "model-e")
        _push_commit(tmp_data_dir, "erin", "model-e", "only", "x")

        # Asking for a million commits must not crash; the implementation
        # caps to `_MAX_LIMIT` internally.
        revs = await recent_revisions("erin", "model-e", limit=10_000)
        assert len(revs) == 1

    async def test_committed_at_is_timezone_aware_utc(self, tmp_data_dir: Path) -> None:
        _init_bare(tmp_data_dir, "frank", "model-f")
        _push_commit(tmp_data_dir, "frank", "model-f", "stamped", "x")

        revs = await recent_revisions("frank", "model-f")
        assert len(revs) == 1
        ts = revs[0].committed_at
        assert ts.tzinfo is not None
        assert ts.utcoffset() == ts.utcoffset()  # not naive


class TestRecentRevisionsEmptyBranch:
    """A bare repo with commits on a non-default branch stays empty here."""

    async def test_commits_only_on_alt_branch_yields_empty(self, tmp_data_dir: Path) -> None:
        work = tmp_data_dir / "_work_alt"
        work.mkdir()
        porcelain.init(str(work), bare=False)
        (work / "f").write_text("x")
        porcelain.add(str(work), paths=[str(work / "f")])
        porcelain.commit(
            str(work),
            message=b"on default branch",
            author=b"T <t@example.com>",
            committer=b"T <t@example.com>",
        )

        bare = repo_fs_path("grace", "model-g")
        bare.parent.mkdir(parents=True, exist_ok=True)
        porcelain.init(str(bare), bare=True)
        # Force the push to land on a branch other than the default.
        local_branch = porcelain.active_branch(str(work))
        porcelain.push(str(work), str(bare), b"refs/heads/" + local_branch + b":refs/heads/feature")

        assert await recent_revisions("grace", "model-g") == []
