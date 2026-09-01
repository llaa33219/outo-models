"""Spaces runtime status (v2 — Podman-backed states)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from outo_models.config import Settings
from outo_models.db import Repo
from outo_models.spaces.runtime_manager import (
    SpaceRuntimeManager,
)
from outo_models.spaces.runtime_manager import (
    container_name as _container_name,
)


class RuntimeState(StrEnum):
    DISABLED = "disabled"
    STOPPED = "stopped"
    BUILDING = "building"
    RUNNING = "running"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class RuntimeStatus:
    state: RuntimeState
    message: str
    url: str | None
    container_id: str | None = None
    port: int | None = None


_DISABLED = "Runtime is disabled."
_DISABLED_HINT = (
    "An administrator must set OUTO_SPACES_RUNTIME_ENABLED=true and mount the "
    "Podman socket before trying again."
)
_STOPPED = "The Space is stopped."
_BUILDING = "The Space is building."
_RUNNING = "The Space is running."
_FAILED_PREFIX = "Last run failed: "


def _make_disabled(_settings: Settings) -> RuntimeStatus:
    return RuntimeStatus(
        state=RuntimeState.DISABLED,
        message=f"{_DISABLED} {_DISABLED_HINT}",
        url=None,
    )


def _run_url(settings: Settings, owner: str, name: str) -> str:
    return f"{settings.base_url}/spaces/{owner}/{name}/run/"


def _podman_state(podman_status: str | None) -> RuntimeState:
    if podman_status is None:
        return RuntimeState.STOPPED
    lowered = podman_status.lower()
    if lowered == "running":
        return RuntimeState.RUNNING
    if lowered == "building":
        return RuntimeState.BUILDING
    return RuntimeState.STOPPED


def _podman_host_port(inspect: dict[str, object] | None) -> int | None:
    if inspect is None:
        return None
    ns = inspect.get("NetworkSettings")
    if not isinstance(ns, dict):
        return None
    ports = ns.get("Ports")
    if not isinstance(ports, dict):
        return None
    candidates: list[int] = []
    for mappings in ports.values():
        if not isinstance(mappings, list):
            continue
        for mapping in mappings:
            if not isinstance(mapping, dict):
                continue
            hp = mapping.get("HostPort")
            if hp is None:
                continue
            try:
                candidates.append(int(hp))
            except (TypeError, ValueError):
                continue
    return min(candidates) if candidates else None


def _podman_container_id(inspect: dict[str, object] | None) -> str | None:
    if inspect is None:
        return None
    cid = inspect.get("Id") or inspect.get("id")
    if isinstance(cid, str) and cid:
        return cid
    return None


async def runtime_status(
    space: Repo,
    *,
    settings: Settings,
    manager: SpaceRuntimeManager,
    failed_reason: str | None = None,
) -> RuntimeStatus:
    owner_name = space.owner.username if space.owner is not None else ""
    if not settings.spaces_runtime_enabled:
        return _make_disabled(settings)
    try:
        inspect = await manager.inspect(owner_name, space.name)
    except Exception as exc:
        return RuntimeStatus(
            state=RuntimeState.FAILED,
            message=f"{_FAILED_PREFIX}{exc}",
            url=None,
        )
    if inspect is None:
        return RuntimeStatus(
            state=RuntimeState.STOPPED,
            message=_STOPPED,
            url=None,
        )
    state_value = inspect.get("State")
    podman_status: str | None = None
    if isinstance(state_value, dict):
        raw = state_value.get("Status")
        if isinstance(raw, str):
            podman_status = raw
    mapped = _podman_state(podman_status)
    port = _podman_host_port(inspect)
    container_id = _podman_container_id(inspect)
    url = _run_url(settings, owner_name, space.name) if mapped is RuntimeState.RUNNING else None
    if mapped is RuntimeState.FAILED:
        message = f"{_FAILED_PREFIX}{failed_reason}" if failed_reason else "Last run failed."
        return RuntimeStatus(
            state=RuntimeState.FAILED,
            message=message,
            url=None,
            container_id=container_id,
            port=port,
        )
    if mapped is RuntimeState.RUNNING:
        return RuntimeStatus(
            state=RuntimeState.RUNNING,
            message=_RUNNING,
            url=url,
            container_id=container_id,
            port=port,
        )
    if mapped is RuntimeState.BUILDING:
        return RuntimeStatus(
            state=RuntimeState.BUILDING,
            message=_BUILDING,
            url=None,
        )
    return RuntimeStatus(
        state=RuntimeState.STOPPED,
        message=_STOPPED,
        url=None,
        container_id=container_id,
        port=port,
    )


def container_name_for(owner: str, name: str) -> str:
    return _container_name(owner, name)


__all__ = [
    "RuntimeState",
    "RuntimeStatus",
    "container_name_for",
    "runtime_status",
]
