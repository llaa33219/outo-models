"""Unit tests for `outo_models.spaces.runtime`.

The v1 runtime is a deliberate stub: every space — public or private,
fresh or 404 — reports `PREVIEW_UNAVAILABLE` with a Korean roadmap
message and a pointer to the docs. The tests below lock that contract so
later contributors cannot accidentally turn a metadata-only Spaces v1
into a half-implemented runtime.
"""

from __future__ import annotations

from outo_models.spaces import runtime as runtime_mod
from outo_models.spaces.runtime import RuntimeState, RuntimeStatus, runtime_status


class TestRuntimeState:
    """`RuntimeState` is the small enum the router matches against."""

    def test_only_preview_unavailable_is_defined(self) -> None:
        assert set(RuntimeState.__members__) == {"PREVIEW_UNAVAILABLE"}

    def test_value_is_string(self) -> None:
        # Routers serialize the state into JSON; the value MUST round-trip
        # as a plain string for the API contract to hold.
        assert RuntimeState.PREVIEW_UNAVAILABLE.value == "preview_unavailable"
        assert RuntimeState.PREVIEW_UNAVAILABLE == "preview_unavailable"


class TestRuntimeStatusDataclass:
    """`RuntimeStatus` is a frozen, slotted value object — nothing fancier."""

    def test_is_frozen(self) -> None:
        status = RuntimeStatus(
            state=RuntimeState.PREVIEW_UNAVAILABLE,
            message="m",
            docs_url="/docs/spaces",
        )
        # `frozen=True` raises on attribute assignment.
        import dataclasses

        with __import__("pytest").raises(dataclasses.FrozenInstanceError):
            status.message = "new"  # type: ignore[misc]


class TestRuntimeStatus:
    """`runtime_status(space)` always reports PREVIEW_UNAVAILABLE in v1."""

    def test_returns_preview_unavailable_state(self) -> None:
        # A bare `Repo()` instance is enough — the v1 stub ignores the row.
        from outo_models.db.models.repo import Repo

        space = Repo(name="demo", kind="space", visibility="private", path="x")
        status = runtime_status(space)

        assert isinstance(status, RuntimeStatus)
        assert status.state is RuntimeState.PREVIEW_UNAVAILABLE

    def test_message_is_korean_roadmap_notice(self) -> None:
        from outo_models.db.models.repo import Repo

        space = Repo(name="demo", kind="space", visibility="private", path="x")
        status = runtime_status(space)

        # The message MUST be in Korean and MUST mention that runtime
        # execution is not supported in v1. The exact wording is allowed to
        # evolve, but the gist is locked.
        assert any(0xAC00 <= ord(ch) <= 0xD7A3 for ch in status.message), (
            "runtime message must be in Korean"
        )
        assert "런타임" in status.message
        assert "v1" in status.message or "로드맵" in status.message

    def test_docs_url_points_at_spaces_docs(self) -> None:
        from outo_models.db.models.repo import Repo

        space = Repo(name="demo", kind="space", visibility="private", path="x")
        status = runtime_status(space)

        assert status.docs_url == "/docs/spaces"

    def test_status_is_independent_of_visibility(self) -> None:
        # Both PUBLIC and PRIVATE spaces are equally "preview unavailable"
        # in v1 — there is no runtime to gate on, so visibility is a no-op.
        from outo_models.db.models.repo import Repo

        public = Repo(
            name="a", kind="space", visibility="public", path="x"
        )
        private = Repo(
            name="b", kind="space", visibility="private", path="x"
        )

        assert runtime_status(public) == runtime_status(private)

    def test_module_exports_match_documented_surface(self) -> None:
        for name in ("RuntimeState", "RuntimeStatus", "runtime_status"):
            assert hasattr(runtime_mod, name), f"runtime missing {name!r}"