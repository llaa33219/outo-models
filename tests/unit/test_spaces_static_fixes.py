"""Static-Space defects: path fallback, Podman-free status, Podman-free stop.

CONTRACT (consumed by the parallel UI track):

    For a Space with sdk="static":
    - site dir (`<data_dir>/spaces/<owner>/<name>/site`) exists
      -> state="running" with url=<run url>; missing -> state="stopped",
      url=None.
    - NEVER `failed` due to Podman errors for static; Podman must never be
      contacted for static status.
    - `stop` action on static -> state="stopped", url=None, NO site
      re-export (a stop must not write files), manager.stop() never called.

These tests pin the three static code paths plus the trailing-slash index
resolution of `_file_response_for_static`.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from outo_models.config import get_settings
from outo_models.server.routers.spaces import (
    _file_response_for_static,
    _run_lifecycle,
    _safe_site_path,
)
from outo_models.spaces import (
    RuntimeState,
    SpaceMeta,
    runtime_status,
    static_site_dir,
    write_space_meta,
)


class _ForbiddenManager:
    """Manager double that fails the test if Podman is ever contacted."""

    async def inspect(self, owner: str, name: str) -> object:
        raise AssertionError("Podman must not be contacted for a static Space")

    async def stop(self, owner: str, name: str) -> None:
        raise AssertionError("manager.stop() must not be called for a static Space")

    async def build_image(self, owner: str, name: str) -> None:
        raise AssertionError("manager.build_image() must not be called for a static Space")

    async def start(self, owner: str, name: str, *, gpu_ids: list[str]) -> None:
        raise AssertionError("manager.start() must not be called for a static Space")


class _FakeSession:
    """Minimal async-session double for `_run_lifecycle`'s audit writes."""

    def __init__(self) -> None:
        self.added: list[object] = []
        self.commits = 0

    def add(self, obj: object) -> None:
        self.added.append(obj)

    async def commit(self) -> None:
        self.commits += 1


def _space(owner: str = "alice", name: str = "demo") -> SimpleNamespace:
    return SimpleNamespace(
        id=1,
        name=name,
        owner=SimpleNamespace(id=2, username=owner, role="user"),
    )


class TestFileResponseForStatic:
    """The trailing slash rewrite must KEEP the directory prefix."""

    @pytest.fixture
    def site(self, tmp_data_dir: Path) -> Path:
        write_space_meta("alice", "demo", SpaceMeta(sdk="static"))
        root = static_site_dir("alice", "demo")
        (root / "docs" / "sub").mkdir(parents=True)
        (root / "index.html").write_text("root")
        (root / "docs" / "index.html").write_text("docs")
        (root / "docs" / "sub" / "index.html").write_text("sub")
        (root / "app.js").write_text("js")
        return root

    def _read(self, root: Path, path: str) -> str | None:
        served = _file_response_for_static(root, path)
        if served is None:
            return None
        return Path(served.path).read_text()

    def test_empty_path_serves_root_index(self, site: Path) -> None:
        assert self._read(site, "") == "root"

    def test_trailing_slash_keeps_directory_prefix(self, site: Path) -> None:
        assert self._read(site, "docs/") == "docs"

    def test_nested_trailing_slash_keeps_prefix(self, site: Path) -> None:
        assert self._read(site, "docs/sub/") == "sub"

    def test_leading_slash_is_stripped_before_append(self, site: Path) -> None:
        assert self._read(site, "/docs/") == "docs"

    def test_plain_file_is_served_unchanged(self, site: Path) -> None:
        assert self._read(site, "app.js") == "js"

    def test_missing_file_returns_none(self, site: Path) -> None:
        assert _file_response_for_static(site, "nope.html") is None

    def test_missing_site_root_returns_none(self, site: Path) -> None:
        assert _file_response_for_static(site / "absent", "") is None

    def test_traversal_is_blocked(self, site: Path) -> None:
        assert _file_response_for_static(site, "../secrets.txt") is None

    def test_traversal_guard_direct_call_still_blocks(self, site: Path) -> None:
        assert _safe_site_path(site, "../index.html") is None


class TestStaticRuntimeStatus:
    """`runtime_status` must answer from the site dir for static Spaces."""

    async def test_running_when_site_dir_exists(self, tmp_data_dir: Path) -> None:
        write_space_meta("alice", "demo", SpaceMeta(sdk="static"))
        static_site_dir("alice", "demo").mkdir(parents=True)
        status = await runtime_status(
            _space(),
            settings=get_settings(),
            manager=_ForbiddenManager(),  # type: ignore[arg-type]
        )
        assert status.state is RuntimeState.RUNNING
        assert status.url is not None
        assert status.url.endswith("/spaces/alice/demo/run/")

    async def test_stopped_when_site_dir_missing(self, tmp_data_dir: Path) -> None:
        write_space_meta("alice", "demo", SpaceMeta(sdk="static"))
        status = await runtime_status(
            _space(),
            settings=get_settings(),
            manager=_ForbiddenManager(),  # type: ignore[arg-type]
        )
        assert status.state is RuntimeState.STOPPED
        assert status.url is None

    async def test_site_dir_alongside_meta_does_not_confuse(self, tmp_data_dir: Path) -> None:
        write_space_meta("alice", "demo", SpaceMeta(sdk="static"))
        (tmp_data_dir / "spaces" / "alice" / "demo").mkdir(parents=True, exist_ok=True)
        status = await runtime_status(
            _space(),
            settings=get_settings(),
            manager=_ForbiddenManager(),  # type: ignore[arg-type]
        )
        assert status.state is RuntimeState.STOPPED


class TestStaticStopAction:
    """`stop` on a static Space is a pure state transition."""

    async def test_stop_does_not_touch_podman_or_files(self, tmp_data_dir: Path) -> None:
        write_space_meta("alice", "demo", SpaceMeta(sdk="static"))
        static_site_dir("alice", "demo").mkdir(parents=True)
        (static_site_dir("alice", "demo") / "index.html").write_text("original")
        db = _FakeSession()
        status = await _run_lifecycle(
            db=db,  # type: ignore[arg-type]
            user=_space().owner,  # type: ignore[arg-type]
            settings=get_settings(),
            manager=_ForbiddenManager(),  # type: ignore[arg-type]
            repo=_space(),  # type: ignore[arg-type]
            action="stop",
            audit_target_id="1",
            gpu_ids=[],
        )
        assert status.state is RuntimeState.STOPPED
        assert status.url is None
        assert (static_site_dir("alice", "demo") / "index.html").read_text() == "original"

    async def test_stop_does_not_create_the_site_dir(self, tmp_data_dir: Path) -> None:
        write_space_meta("alice", "demo", SpaceMeta(sdk="static"))
        db = _FakeSession()
        status = await _run_lifecycle(
            db=db,  # type: ignore[arg-type]
            user=_space().owner,  # type: ignore[arg-type]
            settings=get_settings(),
            manager=_ForbiddenManager(),  # type: ignore[arg-type]
            repo=_space(),  # type: ignore[arg-type]
            action="stop",
            audit_target_id="1",
            gpu_ids=[],
        )
        assert status.state is RuntimeState.STOPPED
        assert not static_site_dir("alice", "demo").exists()


class TestStripHopHeaders:
    """Upstream framing headers must not kill the preview."""

    def test_upstream_framing_headers_are_stripped(self) -> None:
        import httpx

        from outo_models.server.routers.spaces import _strip_hop_headers

        upstream = httpx.Headers(
            {
                "x-frame-options": "DENY",
                "content-security-policy": "default-src 'none'",
                "content-type": "text/html",
                "content-length": "12",
                "connection": "keep-alive",
            }
        )
        cleaned = _strip_hop_headers(upstream)
        assert "x-frame-options" not in cleaned
        assert "content-security-policy" not in cleaned
        assert cleaned["content-type"] == "text/html"
        assert "content-length" not in cleaned
