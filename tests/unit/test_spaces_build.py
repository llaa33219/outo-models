"""Unit tests for `outo_models.spaces.build`.

The build module exports tarballs and on-disk site dumps from a bare
git repo. Tests exercise two surfaces:

    * `make_build_context(owner, name)` — the bytes the runtime manager
      ships to `podman build`. The tar MUST contain every tracked file
      at the right path, MUST NOT contain `.git/`, MUST be gzipped.
    * `export_static_site(owner, name, dest)` — the disk-only static
      dump. Files end up under `dest` in the same tree layout.

Neither subprocesses nor the network; everything is backed by
`dulwich.repo.Repo` on a tempdir.
"""

from __future__ import annotations

import io
import tarfile
from pathlib import Path

import pytest
from dulwich import porcelain

from outo_models.config import get_settings
from outo_models.spaces.build import (
    export_static_site,
    make_build_context,
    static_site_dir,
)


@pytest.fixture
def seeded_repo(tmp_data_dir: Path) -> tuple[str, str]:
    """Seed a bare repo with a couple of files; return (owner, name)."""
    owner = "alice"
    name = "demo"
    work = tmp_data_dir / "src"
    work.mkdir()
    repo_root = tmp_data_dir / "repos" / owner / f"{name}.git"
    repo_root.parent.mkdir(parents=True)
    (work / "owner_dir" / "fake").mkdir(parents=True)
    (work / "owner_dir" / "real").mkdir(parents=True)

    src = work / "demo"
    src.mkdir()
    (src / "app.py").write_text('print("hi")\n')
    (src / "README.md").write_text("# demo\n")
    (src / "src").mkdir()
    (src / "src" / "helper.py").write_text("# helper\n")
    (src / ".gitignore").write_text("node_modules\n")

    porcelain.init(str(src))
    porcelain.add(
        str(src),
        paths=["app.py", "README.md", ".gitignore", "src/helper.py"],
    )
    porcelain.commit(
        str(src),
        message=b"init",
        author=b"alice <a@example.com>",
        committer=b"alice <a@example.com>",
    )
    porcelain.clone(str(src), str(repo_root), bare=True)

    get_settings.cache_clear()
    return owner, name


class TestMakeBuildContext:
    def test_returns_gzipped_tar_bytes(self, seeded_repo: tuple[str, str]) -> None:
        owner, name = seeded_repo
        ctx = make_build_context(owner, name)
        assert isinstance(ctx, bytes)
        assert len(ctx) > 0
        # The first two bytes of any gzipped stream are `\\x1f\\x8b`.
        assert ctx[:2] == b"\x1f\x8b"

    def test_tar_lists_every_tracked_file(self, seeded_repo: tuple[str, str]) -> None:
        owner, name = seeded_repo
        ctx = make_build_context(owner, name)
        with tarfile.open(fileobj=io.BytesIO(ctx)) as tf:
            names = sorted(m.name for m in tf.getmembers())
        assert "app.py" in names
        assert "README.md" in names
        assert ".gitignore" in names
        assert "src/helper.py" in names

    def test_tar_excludes_dot_git(self, seeded_repo: tuple[str, str]) -> None:
        owner, name = seeded_repo
        ctx = make_build_context(owner, name)
        with tarfile.open(fileobj=io.BytesIO(ctx)) as tf:
            names = {m.name for m in tf.getmembers()}
        assert ".git" not in names
        assert not any(n.startswith(".git/") for n in names)

    def test_tar_preserves_file_contents(self, seeded_repo: tuple[str, str]) -> None:
        owner, name = seeded_repo
        ctx = make_build_context(owner, name)
        with tarfile.open(fileobj=io.BytesIO(ctx)) as tf:
            for member in tf.getmembers():
                if member.name == "app.py":
                    extracted = tf.extractfile(member)
                    assert extracted is not None
                    assert extracted.read() == b'print("hi")\n'
                    return
        pytest.fail("app.py not present in tar")


class TestExportStaticSite:
    def test_writes_files_to_dest(self, tmp_data_dir: Path, seeded_repo: tuple[str, str]) -> None:
        owner, name = seeded_repo
        dest = tmp_data_dir / "site_out"
        export_static_site(owner, name, dest)
        assert (dest / "app.py").is_file()
        assert (dest / "README.md").is_file()
        assert (dest / "src" / "helper.py").is_file()
        assert (dest / ".git").is_dir() is False  # type: ignore[comparison-overlap]

    def test_does_not_include_dot_git(
        self, tmp_data_dir: Path, seeded_repo: tuple[str, str]
    ) -> None:
        owner, name = seeded_repo
        dest = tmp_data_dir / "site_out2"
        export_static_site(owner, name, dest)
        assert not (dest / ".git").exists()

    def test_static_site_dir_layout(self, tmp_data_dir: Path) -> None:
        # `static_site_dir` is the layout the router uses to look up
        # files at proxy time. It must be deterministic and rooted under
        # `spaces_dir()`.
        path = static_site_dir("alice", "demo")
        assert path.name == "site"
        assert "alice" in path.parts
        assert "demo" in path.parts
