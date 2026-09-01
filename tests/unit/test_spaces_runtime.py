"""Unit tests for `outo_models.spaces.runtime`.

The v2 runtime dispatcher `runtime_status(space, *, settings, manager)`
must report one of five `RuntimeState` values for every codepath:

    DISABLED  — runtime is off in settings.
    STOPPED   — no container exists for the Space.
    BUILDING  — podman reports `building`.
    RUNNING   — podman reports `running`; URL is populated.
    FAILED    — manager raised an error.

The tests below pin each transition with a small `SpaceRuntimeManager`
fake: we do not mock httpx here because the unit under test is the
*dispatcher*, not the manager wire format — the manager itself is
covered exhaustively in `test_spaces_runtime_manager.py`.
"""

from __future__ import annotations

import dataclasses
from types import SimpleNamespace

import pytest

from outo_models.config import Settings, get_settings
from outo_models.spaces import runtime as runtime_mod
from outo_models.spaces.runtime import (
    RuntimeState,
    RuntimeStatus,
    runtime_status,
)


class _FakeManager:
    """Single-method double that satisfies the dispatcher's `inspect` call."""

    def __init__(
        self,
        inspect_return: object = None,
        inspect_exc: BaseException | None = None,
    ) -> None:
        self._inspect_return = inspect_return
        self._inspect_exc = inspect_exc
        self.calls: list[tuple[str, str]] = []

    async def inspect(self, owner: str, name: str) -> object:
        self.calls.append((owner, name))
        if self._inspect_exc is not None:
            raise self._inspect_exc
        return self._inspect_return


def _settings(runtime_enabled: bool = True) -> Settings:
    """Return a `Settings` whose runtime toggle matches the call site."""
    get_settings.cache_clear()
    settings = get_settings()
    object.__setattr__(settings, "spaces_runtime_enabled", runtime_enabled)
    return settings


def _space(owner_username: str = "alice", name: str = "demo") -> SimpleNamespace:
    """Build a bare `Repo`-shaped dummy.

    The dispatcher only reads `.owner.username` and `.name`, so a plain
    `SimpleNamespace` is enough — no ORM setup needed.
    """
    return SimpleNamespace(name=name, owner=SimpleNamespace(username=owner_username))


class TestRuntimeStateMembers:
    def test_members_present(self) -> None:
        members = set(RuntimeState.__members__)
        assert members == {
            "DISABLED",
            "STOPPED",
            "BUILDING",
            "RUNNING",
            "FAILED",
        }

    def test_values_are_lowercase_strings(self) -> None:
        assert RuntimeState.DISABLED.value == "disabled"
        assert RuntimeState.STOPPED.value == "stopped"
        assert RuntimeState.BUILDING.value == "building"
        assert RuntimeState.RUNNING.value == "running"
        assert RuntimeState.FAILED.value == "failed"

    def test_string_equality_for_serialization(self) -> None:
        assert RuntimeState.RUNNING == "running"
        assert RuntimeState.STOPPED != "running"


class TestRuntimeStatusShape:
    def test_is_frozen(self) -> None:
        status = RuntimeStatus(
            state=RuntimeState.STOPPED,
            message="m",
            url=None,
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            status.message = "new"  # type: ignore[misc]

    def test_container_id_and_port_default_to_none(self) -> None:
        status = RuntimeStatus(
            state=RuntimeState.STOPPED,
            message="m",
            url=None,
        )
        assert status.container_id is None
        assert status.port is None

    def test_module_exports(self) -> None:
        for name in (
            "RuntimeState",
            "RuntimeStatus",
            "runtime_status",
            "container_name_for",
        ):
            assert hasattr(runtime_mod, name), f"runtime missing {name!r}"


class TestDispatcher:
    async def test_disabled_when_runtime_off(self) -> None:
        settings = _settings(runtime_enabled=False)
        manager = _FakeManager(inspect_return={"State": {"Status": "running"}})
        status = await runtime_status(_space(), settings=settings, manager=manager)
        assert status.state is RuntimeState.DISABLED
        assert status.url is None
        assert "비활성화" in status.message
        assert "OUTO_SPACES_RUNTIME_ENABLED" in status.message
        assert manager.calls == []

    async def test_stopped_when_manager_returns_none(self) -> None:
        settings = _settings()
        manager = _FakeManager(inspect_return=None)
        status = await runtime_status(_space(), settings=settings, manager=manager)
        assert status.state is RuntimeState.STOPPED
        assert status.url is None
        assert "중지" in status.message

    async def test_running_state_carries_url_and_port(self) -> None:
        settings = _settings()
        payload = {
            "Id": "abc123",
            "State": {"Status": "running"},
            "NetworkSettings": {"Ports": {"8000/tcp": [{"HostPort": "20123"}]}},
        }
        manager = _FakeManager(inspect_return=payload)
        status = await runtime_status(_space(), settings=settings, manager=manager)
        assert status.state is RuntimeState.RUNNING
        assert status.container_id == "abc123"
        assert status.port == 20123
        assert status.url is not None
        assert status.url.endswith("/spaces/alice/demo/run/")

    async def test_building_state_maps_to_runtime_state_building(self) -> None:
        settings = _settings()
        payload = {"State": {"Status": "building"}}
        manager = _FakeManager(inspect_return=payload)
        status = await runtime_status(_space(), settings=settings, manager=manager)
        assert status.state is RuntimeState.BUILDING
        assert status.url is None

    async def test_stopped_state_for_exited_container(self) -> None:
        settings = _settings()
        payload = {"State": {"Status": "exited"}}
        manager = _FakeManager(inspect_return=payload)
        status = await runtime_status(_space(), settings=settings, manager=manager)
        assert status.state is RuntimeState.STOPPED

    async def test_failed_state_when_manager_raises(self) -> None:
        settings = _settings()
        manager = _FakeManager(inspect_exc=RuntimeError("container inspect failed"))
        status = await runtime_status(_space(), settings=settings, manager=manager)
        assert status.state is RuntimeState.FAILED
        assert "container inspect failed" in status.message

    async def test_failed_state_includes_passed_reason(self) -> None:
        settings = _settings()
        manager = _FakeManager(inspect_exc=RuntimeError("boom"))
        status = await runtime_status(
            _space(),
            settings=settings,
            manager=manager,
            failed_reason="port-range exhausted",
        )
        assert status.state is RuntimeState.FAILED
        assert "boom" in status.message
