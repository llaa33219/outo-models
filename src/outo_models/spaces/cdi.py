"""Server-side CDI spec resolution for the Spaces GPU fallback.

Why this module exists
----------------------

`specgen.SpecGenerator.GPUs` (json `gpus`) only exists in the libpod API from
podman 6.0 on. On podman 4.x / 5.x the field is a no-op: Go's `encoding/json`
drops unknown keys silently, so an assigned GPU is accepted and then simply
never reaches the container — no error, no device nodes, no `nvidia-smi`.
Those releases have no CDI concept in their REST API at all (the CLI resolved
CDI client-side), so the server has to do the resolution itself.

The create body this server sends names the devices and lets podman resolve
them: `devices: [{"path": "nvidia.com/gpu=0"}]` travels to
`specgen.ExtractCDIDevices` → `libpod.WithCDI` → `registry.InjectDevices`,
so podman applies the complete `containerEdits` itself — device nodes,
mounts, env and OCI hooks alike. There is no server-side translation to
maintain for delivery.

What lives here is the *lookup*, shared by two callers. The delivery asks it
which device name to put on the wire: podman's registry matches names
EXACTLY, so a `gpu-0`-shaped spec answers `unresolvable CDI devices
nvidia.com/gpu=0` — `resolve_names` returns the name the spec itself
defines. The diagnosis asks it the same question after a failed create and
reports what it found — which kinds exist, which devices a spec defines, what
each searched directory actually held. The translation into the three
SpecGenerator fields that exist on every version is kept for exactly that
purpose:

* `devices` — OCI `specs.LinuxDevice` entries (`path`/`type`/`major`/…)
* `mounts`  — OCI `specs.Mount` entries (`source`/`destination`/…)
* `env`     — a `map[string]string` merge

A `containerEdits` key the translation cannot express (additional GIDs,
intel-rdt, net devices) still raises instead of being ignored: a silently
GPU-less container is the exact failure this module removes, and an
unreadable device name is the likeliest cause of a rejected create call.
OCI hooks are the one key the translation cannot express at all — a
`SpecGenerator` body has no field for them — so they are recorded as a
`CdiEdits.notes` entry instead of failing. A note is not silence: the
caller surfaces it, so an operator learns that the container it started
without the spec's hooks rather than discovering it later in an audit log.

The spec files live on the host, so `outo-models start` mounts both
directories read-only into the server container (see `cli/start.py`).
When that plumbing is missing or stale, `runtime_manager` reads the same
directories from the host through a throwaway podman helper container and
feeds the result to `parse_specs` + `resolve_names` / `resolve_edits` — the
IO-free half of this module, so the delivery and the diagnosis have exactly
one implementation.

# allow: SIZE_OK — the whole file is one cohesive unit: parse CDI specs
# (from files or from a text stream) and resolve a device selection against
# them. Splitting the parser from the resolver would
# leave the error paths (which need both the specs and the per-directory
# probe report) reaching across modules, and neither half has an independent
# lifecycle: the resolver exists to serve the parser's output.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from outo_models.exceptions import ValidationFailedError
from outo_models.logging import get_logger

_LOGGER = get_logger(__name__)

#: Host CDI spec directories, in the order the CDI spec's default cache uses.
DEFAULT_SPEC_DIRS: tuple[str, ...] = ("/etc/cdi", "/var/run/cdi")

#: The CDI kind an unqualified GPU assignment is resolved under, and the kind
#: the create body's qualified device names are built with.
DEFAULT_KIND = "nvidia.com/gpu"

_UNRESOLVED_CODE = "space_gpu_cdi_unresolved"

_SPEC_SUFFIXES = (".yaml", ".yml", ".json")

#: The only edit keys we can faithfully express in a SpecGenerator body.
#: Everything else fails loudly (see the module docstring).
_SUPPORTED_EDITS = ("deviceNodes", "mounts", "env")

#: Edit keys podman's own CDI path applies but this translation has no field
#: for. They never fail a resolution: podman — not this module — injects them.
_IGNORED_EDITS = ("hooks",)

_UNSUPPORTED_HINT = (
    "Regenerate the CDI spec without it (see the NVIDIA container toolkit documentation)."
)

#: The `containerEdits` key a `SpecGenerator` body has no field for at all,
#: and the note that says so out loud instead of dropping it in silence.
_HOOKS_EDIT = "hooks"
_HOOKS_NOTE = (
    "CDI hooks dropped for {device} ({hooks}): a container create body cannot "
    "express hooks, and this host's podman did not apply the spec's own."
)

_GPU_INDEX_PREFIX = "GPU-"

#: Cap on the device names quoted back in the unresolved-device error: the
#: list diagnoses a mistyped assignment, it is not a spec dump.
_MAX_REPORTED_DEVICES = 12


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
    """Translated container edits, shaped for `specgen.SpecGenerator`.

    `notes` carries the non-fatal findings of the translation — today only
    dropped OCI hooks. The container they came from starts fine, but it is
    not the container the spec describes, so the caller has to say so
    instead of letting the gap pass unnoticed.
    """

    device_nodes: tuple[dict[str, Any], ...]
    mounts: tuple[dict[str, Any], ...]
    env: dict[str, str]
    notes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class DirProbe:
    """What the resolver observed inside one searched spec directory.

    The searched dirs are mount *targets* inside the server container, so
    "does this path exist?" is diagnostic information, not an implementation
    detail: a dir that was never mounted, a dir the container cannot read and
    a dir full of specs of another kind are otherwise indistinguishable from
    the outside. `describe()` renders the one line that tells them apart.
    """

    path: str
    state: str
    specs: tuple[dict[str, Any], ...]
    warnings: tuple[str, ...]

    def describe(self) -> str:
        if self.state != "present":
            return f"{self.path}: {self.state}"
        readable = len(self.specs)
        plural = "" if readable == 1 else "s"
        kinds = sorted({str(spec.get("kind")) for spec in self.specs if spec.get("kind")})
        skipped = "" if not self.warnings else f" ({len(self.warnings)} unreadable)"
        return (
            f"{self.path}: present, {readable} readable spec{plural}{skipped}, "
            f"kinds: {', '.join(kinds) if kinds else 'none'}"
        )


def _probe_dir(directory: str) -> DirProbe:
    """Observe one directory: its state, the specs in it, and its warnings."""
    try:
        entries = sorted(Path(directory).iterdir())
    except FileNotFoundError:
        return DirProbe(directory, "missing", (), ())
    except NotADirectoryError:
        return DirProbe(directory, "not a directory", (), ())
    except OSError:
        return DirProbe(directory, "unreadable", (), ())
    specs: list[dict[str, Any]] = []
    warnings: list[str] = []
    for path in (entry for entry in entries if entry.suffix in _SPEC_SUFFIXES):
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
    return DirProbe(directory, "present", tuple(specs), tuple(warnings))


def _probe_all(dirs: tuple[str, ...]) -> tuple[DirProbe, ...]:
    return tuple(_probe_dir(directory) for directory in dirs)


def probe_specs(
    dirs: tuple[str | Path, ...] | list[str | Path] | None = None,
) -> tuple[list[dict[str, Any]], list[str], tuple[DirProbe, ...]]:
    """`load_specs` plus the per-directory report the errors render.

    A caller that resolves the specs itself (the delivery does, to learn the
    names podman must be given) still owes the operator the same "what did
    the resolver actually see" lines a failure raises, so the probes travel
    with the specs instead of being re-derived by the resolver.
    """
    searched = tuple(str(d) for d in dirs) if dirs is not None else DEFAULT_SPEC_DIRS
    probes = _probe_all(searched)
    specs = [spec for probe in probes for spec in probe.specs]
    warnings = [warning for probe in probes for warning in probe.warnings]
    return specs, warnings, probes


def load_specs(
    dirs: tuple[str | Path, ...] | list[str | Path] | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Read every CDI spec from `dirs` (default: `DEFAULT_SPEC_DIRS`).

    Returns the parsed specs plus a warning per file that could not be read
    or parsed. A broken third-party spec file must not break GPU injection
    for the specs that *are* readable, so parse errors are collected.
    """
    specs, warnings, _ = probe_specs(dirs)
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


def _with_context(message: str, context: str) -> str:
    """Append where these specs came from to a resolution error message."""
    return f"{message} ({context})" if context else message


def _hook_names(hooks: Any) -> str:
    """Render the `hookName` of each dropped hook, so the note names them."""
    if not isinstance(hooks, Sequence) or isinstance(hooks, (str, bytes)):
        return "unnamed"
    names = [
        str(entry["hookName"])
        for entry in hooks
        if isinstance(entry, Mapping) and entry.get("hookName")
    ]
    return ", ".join(names) or "unnamed"


def _translate_edits(
    edits: dict[str, Any],
    *,
    device: str,
    nodes: list[dict[str, Any]],
    mounts: list[dict[str, Any]],
    env: dict[str, str],
    notes: list[str],
    context: str = "",
) -> None:
    """Accumulate one `containerEdits` block into the output collections.

    Hooks are recorded as a note rather than refused: no create body has a
    field for them, and their presence says nothing about whether the
    assignment resolves — only that the delivered container misses them.
    """
    for key in sorted(edits):
        if key not in _SUPPORTED_EDITS and key not in _IGNORED_EDITS:
            raise ValidationFailedError(
                _with_context(
                    f"CDI device {device!r} requests {key!r}, which the "
                    f"resolver cannot account for. {_UNSUPPORTED_HINT}",
                    context,
                ),
                code=_UNRESOLVED_CODE,
            )
    if edits.get(_HOOKS_EDIT):
        notes.append(_HOOKS_NOTE.format(device=device, hooks=_hook_names(edits[_HOOKS_EDIT])))
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
                _with_context(
                    f"CDI device {device!r} has a malformed env entry {entry!r} "
                    "(expected KEY=VALUE).",
                    context,
                ),
                code=_UNRESOLVED_CODE,
            )
        env[key] = value


def _global_edits(spec: Mapping[str, Any]) -> dict[str, Any]:
    """Return the spec-wide `containerEdits` block, if any."""
    edits = spec.get("containerEdits")
    return edits if isinstance(edits, dict) else {}


def _warn_bad_device(kind: Any, reason: str, entry: Any) -> None:
    _LOGGER.warning(
        "CDI spec device entry ignored",
        kind=kind,
        reason=reason,
        entry=repr(entry),
    )


def _device_index(spec: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Index one spec's devices by name, accepting both shapes on the wire.

    The canonical schema (cncf-tags/container-device-interface
    `specs-go/config.go`) declares `devices` as a LIST of
    `{name, containerEdits}` entries — what `nvidia-ctk cdi generate` writes.
    A mapping keyed by device name is indexed the same way, because a
    strict reading here would turn a lenient third-party spec into a hard
    failure over a shape the resolver can express perfectly well.

    An entry that is not a mapping, or that carries no usable name, is
    warned about and skipped: one broken device must not cost the host every
    other GPU in the same spec.
    """
    kind = spec.get("kind")
    devices = spec.get("devices")
    index: dict[str, dict[str, Any]] = {}
    if isinstance(devices, Mapping):
        for name, device in devices.items():
            if isinstance(device, Mapping):
                index[str(name)] = dict(device)
            else:
                _warn_bad_device(kind, f"device {name!r} is not a mapping", device)
        return index
    if not isinstance(devices, Sequence) or isinstance(devices, (str, bytes)):
        return index
    for position, entry in enumerate(devices, start=1):
        if not isinstance(entry, Mapping):
            _warn_bad_device(kind, f"entry {position} is not a mapping", entry)
            continue
        name = str(entry.get("name", "")).strip()
        if not name:
            _warn_bad_device(kind, f"entry {position} has no name", entry)
            continue
        index[name] = dict(entry)
    return index


def _find_device(index: Mapping[str, dict[str, Any]], name: str) -> str | None:
    """Return the spec's own device name for `name`, or None.

    The stored spelling comes back, not the entry: podman's CDI registry
    matches device names exactly, so the delivery needs the name the spec
    defines while the caller only knows the assignment's spelling.

    `device_name` runs on both sides, so a spec that names its device
    `gpu-0` answers the assignment `0` and the other way round. An exact
    match always wins: a spec defining both `0` and `gpu-0` hands each
    query the entry it literally named.
    """
    if name in index:
        return name
    alias = device_name(name)
    return next((stored for stored in index if device_name(stored) == alias), None)


@dataclass(frozen=True, slots=True)
class _Where:
    """The error message's only extra input: where the specs came from.

    Diagnostics cannot travel with the specs themselves (a caller that read
    them over the network has no local `Path` to describe), so the search
    sites and the source note are carried next to them.
    """

    searched: tuple[str, ...] = DEFAULT_SPEC_DIRS
    probes: tuple[DirProbe, ...] = ()
    context: str = ""


@dataclass(frozen=True, slots=True)
class _Match:
    """One resolved device: its spec, the spec's own name, and its entry."""

    spec: Mapping[str, Any]
    name: str
    device: dict[str, Any]


def _no_spec_error(kind: str, where: _Where) -> ValidationFailedError:
    """Build the "no spec of this kind" error, with the per-dir report."""
    report = "\n".join(f"  {probe.describe()}" for probe in where.probes)
    return ValidationFailedError(
        _with_context(
            f"No CDI spec of kind {kind!r} found in "
            f"{', '.join(where.searched)}. What the resolver actually saw:\n"
            f"{report or '  (nothing — the spec directories hold no readable spec)'}\n"
            "Generate the specs on the host "
            "(nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml), then "
            "refresh the CLI shim so it forwards the host spec directories "
            "and recreate the server container so it can read them: re-run "
            "scripts/install-cli.sh, then outo-models update.",
            where.context,
        ),
        code=_UNRESOLVED_CODE,
    )


def _undefined_device_error(
    name: str,
    kind: str,
    index: Mapping[str, dict[str, Any]],
    where: _Where,
) -> ValidationFailedError:
    """Build the "not defined" error, listing what the spec does define."""
    return ValidationFailedError(
        _with_context(
            f"CDI device {name!r} of kind {kind!r} is not defined "
            f"in the specs found in {', '.join(where.searched)} — defined devices: "
            f"{_report_devices(index)}.",
            where.context,
        ),
        code=_UNRESOLVED_CODE,
    )


def _match_device(
    specs: Sequence[Mapping[str, Any]],
    identifier: str,
    *,
    kind: str = DEFAULT_KIND,
    where: _Where | None = None,
    indexes: dict[str, dict[str, dict[str, Any]]] | None = None,
) -> _Match:
    """Resolve one identifier against already-parsed specs.

    The single lookup both public resolvers go through, so the delivery
    (`resolve_names`) and the diagnosis (`resolve_edits`) can never disagree
    about which device an assignment means.

    `indexes` caches the per-kind device index: a malformed entry is a defect
    of the spec, not of the assignment, so it must not be reported once per
    device.
    """
    site = where if where is not None else _Where()
    device_kind, name = _split_identifier(device_name(identifier), kind)
    spec = next((s for s in specs if s.get("kind") == device_kind), None)
    if spec is None:
        raise _no_spec_error(device_kind, site)
    cache = indexes if indexes is not None else {}
    if device_kind not in cache:
        cache[device_kind] = _device_index(spec)
    index = cache[device_kind]
    stored = _find_device(index, name)
    if stored is None:
        raise _undefined_device_error(name, device_kind, index, site)
    return _Match(spec=spec, name=stored, device=index[stored])


def _report_devices(index: Mapping[str, dict[str, Any]]) -> str:
    """Render the device names a spec defines, for the unresolved error."""
    names = sorted(index)
    listed = ", ".join(names[:_MAX_REPORTED_DEVICES])
    if len(names) > _MAX_REPORTED_DEVICES:
        listed += "…"
    return listed or "none"


def parse_specs(text: str) -> tuple[list[dict[str, Any]], list[str]]:
    """Parse a `---`-separated stream of CDI specs into mappings.

    The stream format is what a helper container's `cat` produces: each spec
    file is preceded by a document separator, so the IO (reading files, or
    asking the host) stays outside the resolver. Empty documents are skipped
    silently — an empty spec file on the host carries no information — while
    a document that is not a mapping is a real read failure and is reported.

    A stream that does not parse at all yields no specs and one warning: the
    same "collect the broken, keep the good" rule the file loader applies.
    """
    try:
        documents = list(yaml.safe_load_all(text))
    except yaml.YAMLError as exc:
        return [], [f"Ignored unreadable CDI spec stream: {exc}"]
    specs: list[dict[str, Any]] = []
    warnings: list[str] = []
    for position, document in enumerate(documents, start=1):
        if document is None:
            continue
        if not isinstance(document, dict):
            warnings.append(f"Ignored CDI spec document {position}: top-level is not a mapping")
            continue
        specs.append(document)
    return specs, warnings


def resolve_edits(
    specs: Sequence[Mapping[str, Any]],
    identifiers: Sequence[str],
    *,
    kind: str = DEFAULT_KIND,
    searched: Sequence[str] | None = None,
    probes: Sequence[DirProbe] = (),
    context: str = "",
) -> CdiEdits:
    """Translate already-parsed CDI specs into SpecGenerator container edits.

        The pure half of `resolve_device`: same merge rules, same refusals, same
        error codes — the specs arrive as data instead of being read from disk, so
        a caller that obtained them from another filesystem resolves identically.

        `searched`, `probes` and `context` exist only for the error message: they
        say which directories were looked at, what each of them actually held, and
        where these specs came from. Diagnostics cannot travel with the specs
    themselves (a caller that read them over the network has no local `Path` to
        describe), so they are separate keyword arguments by necessity.

        `notes` reports what the translation had to leave behind — dropped OCI
        hooks — so the caller can show the gap instead of shipping a container
        that quietly misses part of its spec.

        Raises:
            ValidationFailedError: no spec / no device for a requested
                identifier, or an edit the diagnostic cannot account for.
                Failing loudly is mandatory — a container that silently starts
                without its GPU is the bug this replaces.
    """
    where = _Where(
        tuple(DEFAULT_SPEC_DIRS if searched is None else searched), tuple(probes), context
    )
    nodes: list[dict[str, Any]] = []
    mounts: list[dict[str, Any]] = []
    env: dict[str, str] = {}
    notes: list[str] = []
    indexes: dict[str, dict[str, dict[str, Any]]] = {}
    for identifier in identifiers:
        match = _match_device(specs, identifier, kind=kind, where=where, indexes=indexes)
        _translate_edits(
            _global_edits(match.spec),
            device=match.name,
            nodes=nodes,
            mounts=mounts,
            env=env,
            notes=notes,
            context=context,
        )
        edits = match.device.get("containerEdits")
        if isinstance(edits, dict):
            _translate_edits(
                edits,
                device=match.name,
                nodes=nodes,
                mounts=mounts,
                env=env,
                notes=notes,
                context=context,
            )
    return CdiEdits(device_nodes=tuple(nodes), mounts=tuple(mounts), env=env, notes=tuple(notes))


def resolve_names(
    specs: Sequence[Mapping[str, Any]],
    identifiers: Sequence[str],
    *,
    kind: str = DEFAULT_KIND,
    searched: Sequence[str] | None = None,
    probes: Sequence[DirProbe] = (),
    context: str = "",
) -> tuple[str, ...]:
    """Return each identifier's device name **as the spec spells it**.

    Podman's CDI registry matches names exactly: a host whose spec defines
    `gpu-0` answers `unresolvable CDI devices nvidia.com/gpu=0`. So the name
    that goes on the wire has to be the one the spec defines, whatever
    spelling the assignment used — the lookup itself is the same normalized
    alias match `resolve_edits` applies, only its result is reported back.

    Raises:
        ValidationFailedError: no spec / no device for a requested
            identifier, with the same diagnosis `resolve_edits` raises.
            Failing before the create is the point: podman's own
            `unresolvable CDI devices` names neither the kind, nor the
            defined devices, nor which spec directories were searched.
    """
    where = _Where(
        tuple(DEFAULT_SPEC_DIRS if searched is None else searched), tuple(probes), context
    )
    indexes: dict[str, dict[str, dict[str, Any]]] = {}
    return tuple(
        _match_device(specs, identifier, kind=kind, where=where, indexes=indexes).name
        for identifier in identifiers
    )


def resolve_device(
    identifiers: Sequence[str],
    *,
    kind: str = DEFAULT_KIND,
    dirs: Sequence[str | Path] | None = None,
) -> CdiEdits:
    """Resolve CDI device identifiers into SpecGenerator-ready container edits.

    `identifiers` are CDI device references — either bare names resolved
    under `kind`, or fully-qualified `vendor.com/class=name`. Every device
    contributes its spec-wide `containerEdits` plus its own
    `containerEdits`; later devices win on conflicting env keys, matching
    CDI's own "last edit applied" behaviour. The result is identical to
    `resolve_edits(load_specs(dirs), ...)` — this is the local-filesystem
    spelling of the same translation.

    Raises:
        ValidationFailedError: no spec / no device for a requested
            identifier, or an edit the diagnostic cannot account for.
    """
    searched = tuple(str(d) for d in dirs) if dirs is not None else DEFAULT_SPEC_DIRS
    probes = _probe_all(searched)
    specs = [spec for probe in probes for spec in probe.specs]
    return resolve_edits(specs, identifiers, kind=kind, searched=searched, probes=probes)


__all__ = [
    "DEFAULT_KIND",
    "DEFAULT_SPEC_DIRS",
    "CdiEdits",
    "DirProbe",
    "device_name",
    "load_specs",
    "parse_specs",
    "probe_specs",
    "resolve_device",
    "resolve_edits",
    "resolve_names",
]
