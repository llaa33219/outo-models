"""Server-side CDI spec resolution for the Spaces GPU fallback.

Why this module exists
----------------------

`specgen.SpecGenerator.GPUs` (json `gpus`) only exists in the libpod API from
podman 6.0 on. On podman 4.x / 5.x the field is a no-op: Go's `encoding/json`
drops unknown keys silently, so an assigned GPU is accepted and then simply
never reaches the container — no error, no device nodes, no `nvidia-smi`.
Those releases have no CDI concept in their REST API at all (the CLI resolved
CDI client-side), so the server has to do the resolution itself.

This module reads the same CDI spec files the podman CLI would read
(`/etc/cdi`, `/var/run/cdi`) and translates the selected devices into the
three SpecGenerator fields that exist on every version:

* `devices` — OCI `specs.LinuxDevice` entries (`path`/`type`/`major`/…)
* `mounts`  — OCI `specs.Mount` entries (`source`/`destination`/…)
* `env`     — a `map[string]string` merge

Anything the translation cannot express (OCI hooks, additional GIDs,
intel-rdt, net devices) raises instead of being dropped: a silently
GPU-less container is the exact failure this module removes.

The spec files live on the host, so `outo-models start` mounts both
directories read-only into the server container (see `cli/start.py`).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from outo_models.exceptions import ValidationFailedError

#: Host CDI spec directories, in the order the CDI spec's default cache uses.
DEFAULT_SPEC_DIRS: tuple[str, ...] = ("/etc/cdi", "/var/run/cdi")

_SPEC_SUFFIXES = (".yaml", ".yml", ".json")

#: The only edit keys we can faithfully express in a SpecGenerator body.
#: Everything else fails loudly (see the module docstring).
_SUPPORTED_EDITS = ("deviceNodes", "mounts", "env")

_UNSUPPORTED_HINT = (
    "Regenerate the CDI spec without it (see the NVIDIA container toolkit "
    "documentation), or run the Space on a host where the podman CLI handles "
    "CDI itself."
)

_GPU_INDEX_PREFIX = "GPU-"


def device_name(assignment_id: str) -> str:
    """Map a stored GPU assignment to an NVIDIA CDI device name.

    Assignments are operator input stored verbatim (`gpu-0` in the admin UI),
    while a valid NVIDIA CDI device name is either a bare integer index or
    `GPU-<uuid>`. Strip a `gpu-` prefix when the remainder is an integer;
    anything else passes through untouched — the UI track owns input rules.
    This is the single normalizer both GPU paths go through: the podman >= 6
    `gpus` field and the CDI fallback below.
    """
    candidate = assignment_id.strip()
    if candidate.lower().startswith(_GPU_INDEX_PREFIX.lower()):
        remainder = candidate[len(_GPU_INDEX_PREFIX) :]
        if remainder.isdigit():
            return str(int(remainder))
    return candidate


@dataclass(frozen=True, slots=True)
class CdiEdits:
    """Translated container edits, shaped for `specgen.SpecGenerator`."""

    device_nodes: tuple[dict[str, Any], ...]
    mounts: tuple[dict[str, Any], ...]
    env: dict[str, str]


def _spec_files(dirs: tuple[str | Path, ...]) -> list[Path]:
    """Return every spec-looking file in `dirs`, sorted for determinism."""
    files: list[Path] = []
    for directory in dirs:
        root = Path(directory)
        try:
            if not root.is_dir():
                continue
            entries = sorted(root.iterdir())
        except OSError:
            # An unreadable directory (permissions, vanished tmpfs) must not
            # take the Space down; the missing-device error names the dirs.
            continue
        files.extend(entry for entry in entries if entry.suffix in _SPEC_SUFFIXES)
    return files


def load_specs(
    dirs: tuple[str | Path, ...] | list[str | Path] | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Read every CDI spec from `dirs` (default: `DEFAULT_SPEC_DIRS`).

    Returns the parsed specs plus a warning per file that could not be read
    or parsed. A broken third-party spec file must not break GPU injection
    for the specs that *are* readable, so parse errors are collected.
    """
    specs: list[dict[str, Any]] = []
    warnings: list[str] = []
    for path in _spec_files(tuple(dirs) if dirs is not None else DEFAULT_SPEC_DIRS):
        try:
            with path.open("r", encoding="utf-8") as fh:
                payload = yaml.safe_load(fh)
        except (OSError, yaml.YAMLError) as exc:
            warnings.append(f"Ignored unreadable CDI spec {path}: {exc}")
            continue
        if not isinstance(payload, dict):
            warnings.append(f"Ignored CDI spec {path}: top-level is not a mapping")
            continue
        specs.append(payload)
    return specs, warnings


def _split_identifier(identifier: str, default_kind: str) -> tuple[str, str]:
    """Return `(kind, name)` for `vendor.com/class=name` or a bare name."""
    qualified, separator, name = identifier.rpartition("=")
    if not separator:
        return default_kind, identifier.strip()
    kind = qualified.strip()
    if "/" not in kind or "." not in kind:
        # Not a qualified CDI identifier — treat the whole token as a name.
        return default_kind, identifier.strip()
    return kind, name.strip()


def _device_node(
    entry: dict[str, Any], device: str
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Translate one `deviceNodes` entry into (device, optional bind mount).

    A node with `hostPath` is provided by the host, so it must be bind
    mounted into the container; the device node itself is still emitted when
    the spec carries the numeric fields, which is what gives the container
    the cgroup permission to open it.
    """
    path = str(entry.get("path", ""))
    host_path = str(entry.get("hostPath", ""))
    node: dict[str, Any] = {"path": path}
    for key in ("type", "major", "minor", "fileMode", "uid", "gid"):
        if entry.get(key) is not None:
            node[key] = entry[key]
    mount: dict[str, Any] | None = None
    if host_path:
        mount = {
            "source": host_path,
            "destination": path,
            "type": "bind",
            "options": ["bind"],
        }
    return node, mount


def _translate_edits(
    edits: dict[str, Any],
    *,
    device: str,
    nodes: list[dict[str, Any]],
    mounts: list[dict[str, Any]],
    env: dict[str, str],
) -> None:
    """Accumulate one `containerEdits` block into the output collections."""
    for key in sorted(edits):
        if key not in _SUPPORTED_EDITS:
            raise ValidationFailedError(
                f"CDI device {device!r} requests {key!r}, which cannot be "
                f"translated into a podman create body. {_UNSUPPORTED_HINT}",
                code="space_gpu_cdi_unresolved",
            )
    for entry in edits.get("deviceNodes") or []:
        node, mount = _device_node(entry, device)
        nodes.append(node)
        if mount is not None:
            mounts.append(mount)
    for entry in edits.get("mounts") or []:
        mount = {
            "source": str(entry.get("hostPath", "")),
            "destination": str(entry.get("containerPath", "")),
        }
        if entry.get("type") is not None:
            mount["type"] = entry["type"]
        if entry.get("options") is not None:
            mount["options"] = [str(option) for option in entry["options"]]
        mounts.append(mount)
    for entry in edits.get("env") or []:
        key, separator, value = str(entry).partition("=")
        if not separator:
            raise ValidationFailedError(
                f"CDI device {device!r} has a malformed env entry {entry!r} (expected KEY=VALUE).",
                code="space_gpu_cdi_unresolved",
            )
        env[key] = value


def _global_edits(spec: dict[str, Any]) -> dict[str, Any]:
    """Return the spec-wide `containerEdits` block, if any."""
    edits = spec.get("containerEdits")
    return edits if isinstance(edits, dict) else {}


def resolve_device(
    identifiers: list[str] | tuple[str, ...],
    *,
    kind: str = "nvidia.com/gpu",
    dirs: tuple[str | Path, ...] | list[str | Path] | None = None,
) -> CdiEdits:
    """Resolve CDI device identifiers into SpecGenerator-ready container edits.

    `identifiers` are CDI device references — either bare names resolved
    under `kind`, or fully-qualified `vendor.com/class=name`. Every device
    contributes its spec-wide `containerEdits` plus its own
    `containerEdits`; later devices win on conflicting env keys, matching
    CDI's own "last edit applied" behaviour.

    Raises:
        ValidationFailedError: no spec / no device for a requested
            identifier, or an edit that cannot be expressed as a podman
            create-body field. Failing loudly is mandatory — a container
            that silently starts without its GPU is the bug this replaces.
    """
    searched = tuple(str(d) for d in dirs) if dirs is not None else DEFAULT_SPEC_DIRS
    specs, _warnings = load_specs(searched)
    nodes: list[dict[str, Any]] = []
    mounts: list[dict[str, Any]] = []
    env: dict[str, str] = {}
    for identifier in identifiers:
        device_kind, name = _split_identifier(device_name(identifier), kind)
        spec = next((s for s in specs if s.get("kind") == device_kind), None)
        if spec is None:
            raise ValidationFailedError(
                f"No CDI spec of kind {device_kind!r} found (searched "
                f"{', '.join(searched)}). Install the CDI specs on the host "
                "or mount them into the server container.",
                code="space_gpu_cdi_unresolved",
            )
        devices = spec.get("devices")
        device = devices.get(name) if isinstance(devices, dict) else None
        if not isinstance(device, dict):
            raise ValidationFailedError(
                f"CDI device {name!r} of kind {device_kind!r} is not defined "
                f"in the specs found in {', '.join(searched)}.",
                code="space_gpu_cdi_unresolved",
            )
        _translate_edits(_global_edits(spec), device=name, nodes=nodes, mounts=mounts, env=env)
        edits = device.get("containerEdits")
        if isinstance(edits, dict):
            _translate_edits(edits, device=name, nodes=nodes, mounts=mounts, env=env)
    return CdiEdits(device_nodes=tuple(nodes), mounts=tuple(mounts), env=env)


__all__ = ["DEFAULT_SPEC_DIRS", "CdiEdits", "device_name", "load_specs", "resolve_device"]
