"""Podman REST client used by the Spaces runtime.

# allow: SIZE_OK — single cohesive Podman REST client. The class
# owns lifecycle (build / start / stop / restart / remove / inspect)
# over a unit-of-work boundary that should not be split — every helper
# serves at most one method on the class, and the alternatives (a
# separate helpers module + multiple manager classes) would force
# callers to thread the same `_podman_socket_hint` / `_ERROR_TAIL_MAX_CHARS`
# constants across files. Keeping one module is the smaller diff and
# the easier review target.

The manager is the only module that knows Podman's REST verbs and bodies.
Everything above it (`runtime.py`, the router, the templates) stays in
domain terms: `start`, `stop`, `status`, `list_managed`. That boundary is
intentional — it lets the rest of the server stay agnostic of the runtime
backend.

Wire details
------------

All requests hit the Unix-domain socket Podman exposes via the `podman`
service (`/run/podman/podman.sock` by default). The httpx convention for
a UDS-only client is:

    transport = httpx.AsyncHTTPTransport(uds=settings.podman_socket)
    client    = httpx.AsyncClient(base_url="http://d", transport=transport)

`http://d` is a sentinel host — the actual hostname is irrelevant because
traffic goes over the socket. `Settings.podman_socket` controls the path so
operators can point at a container-mounted socket in production.

Error model
-----------

Two distinct failure surfaces map to typed `OutoError` subclasses (created
inline via the constructor `code=` argument):

* socket unreachable (`httpx.ConnectError`, `OSError`) → ``code="podman_unreachable"``
  with an operator-facing hint telling the operator to mount the socket
  into the container.
* Podman REST 4xx / 5xx → ``code="podman_api"`` carrying the podman error
  body verbatim so the operator can see *why* the call was rejected.

Build is special: a 4xx response from `POST /libpod/build` carries the
build log in its stream. We raise ``code="space_build_failed"`` and splice
the last lines of the body into the message — the caller otherwise has no
visibility into why the image did not build.

Lifecycle
---------

Each public method opens an httpx request through the provided client. The
client is constructed lazily by the manager when the caller does not
supply one — production code lets `create_app` wire a fresh client per
request, tests inject a `MockTransport`. The manager does not start its
own daemon: that job belongs to the operator and the host scripts
(`assets/scripts/`).
"""

from __future__ import annotations

import asyncio
import json
import secrets
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, cast

import httpx

from outo_models.config import Settings
from outo_models.exceptions import OutoError, ValidationFailedError
from outo_models.spaces import cdi
from outo_models.spaces.cdi import device_name

_LIBPOD_PREFIX = "/v4.0.0/libpod"

_SPACE_PORT = 8000

_LABEL_MANAGED = "outo.managed"
_LABEL_MANAGED_VALUE = "true"
_LABEL_SPACE = "outo.space"

_ERROR_TAIL_MAX_CHARS = 2048

_CDI_PROBE_PREFIX = "outo-cdi-probe-"
_LABEL_CDI_PROBE = "outo.cdi-probe"
_CDI_PROBE_TIMEOUT = 10.0
_CDI_PROBE_POLL_INTERVAL = 0.1
_CDI_HOST_CONTEXT = "specs read from the host via a helper container"
_CDI_BOTH_ATTEMPTS = (
    "Both resolution paths were attempted: the server container's own "
    "filesystem first, then the same directories read on the host through a "
    "throwaway podman helper container. Generate the specs on the host "
    "(nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml), make sure the "
    "GPU is assigned to the user, and check that the assigned GPU id exists "
    "in those specs."
)


def container_name(owner: str, name: str) -> str:
    """Return the deterministic podman container name for a Space."""
    return f"outo-space-{owner}-{name}"


def image_tag(owner: str, name: str) -> str:
    """Return the deterministic podman image tag for a Space's latest build."""
    return f"localhost/outo-space-{owner}-{name}:latest"


def _podman_socket_hint() -> str:
    """Return the operator-facing hint appended to `podman_unreachable` errors."""
    return (
        "Cannot connect to the Podman socket. `outo-models start` mounts it "
        "automatically; quadlet and other deployments must mount the host "
        "socket themselves (OUTO_PODMAN_SOCKET, default "
        "/run/podman/podman.sock)."
    )


def _build_failure_tail(body: str) -> str:
    """Trim a podman build-log dump to the last meaningful lines."""
    if not body:
        return ""
    trimmed = body.strip()
    if len(trimmed) <= _ERROR_TAIL_MAX_CHARS:
        return trimmed
    return "…" + trimmed[-_ERROR_TAIL_MAX_CHARS:]


def _extract_first(payload: dict[str, Any], *keys: str) -> str:
    """Return the first string-shaped value in `payload` for any of `keys`."""
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


# Single identifier normalizer, shared with the CDI fallback path
# (`cdi.resolve_device` applies the same function to its input).
_cdi_device_name = device_name


_GPU_FIELD_MIN_MAJOR = 6


def _major_version(version: str) -> int:
    """Return the leading integer of a `major.minor.patch` version string."""
    head = version.strip().split(".", 1)[0]
    try:
        return int(head)
    except ValueError:
        return 0


def _cdi_probe_script(dirs: Sequence[str]) -> str:
    """Return the shell program that cats every CDI spec out of `dirs`.

    Each file is preceded by a `---` YAML document separator, which is what
    `cdi.parse_specs` consumes. The `[ -f "$f" ]` guard is what makes an
    unmatched glob a no-op instead of a `cat: no such file` — a directory
    that holds only `.yaml` specs must still succeed.
    """
    return " ".join(
        f'for f in "{directory}"/*{suffix}; do [ -f "$f" ] && {{ echo ---; cat "$f"; }}; done'
        for directory in dirs
        for suffix in (".yaml", ".yml", ".json")
    )


def _split_log_frames(raw: bytes) -> list[bytes]:
    """Split a podman logs body into payloads, or return [] when unframed.

    Podman's libpod logs endpoint always prefixes each line with an 8-byte
    header (`stream`, three zero bytes, uint32 big-endian length); it omits
    the header only for compat requests or a tty container. Both shapes are
    accepted, and anything that does not parse as a clean frame sequence is
    reported as unframed so the raw body is used as-is.
    """
    frames: list[bytes] = []
    offset = 0
    while offset + 8 <= len(raw):
        header = raw[offset : offset + 8]
        if header[0] not in (1, 2) or header[1:4] != b"\x00\x00\x00":
            return []
        offset += 8
        size = int.from_bytes(header[4:8], "big")
        if size == 0 or offset + size > len(raw):
            return []
        frames.append(raw[offset : offset + size])
        offset += size
    return frames if frames and offset == len(raw) else []


def _decode_logs(raw: bytes) -> str:
    """Return the text of a podman logs body, frame headers removed if any."""
    frames = _split_log_frames(raw)
    payload = b"".join(frames) if frames else raw
    return payload.decode("utf-8", errors="replace")


@dataclass(frozen=True, slots=True)
class ManagedContainer:
    """One row returned by `list_managed`."""

    name: str
    host_port: int | None
    raw: dict[str, Any] = field(default_factory=dict)


class SpaceRuntimeManager:
    """Async Podman client dedicated to Space lifecycle operations."""

    def __init__(
        self,
        settings: Settings,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._settings = settings
        self._client = client
        self._owns_client = client is None
        self._podman_version_cache: tuple[str | None] | None = None

    async def podman_version(self) -> str | None:
        """Return the podman engine version string, or None when unknowable.

        `GET /libpod/version` answers `{"Version": "4.9.3", ...}`; older
        releases answer the same shape. Any failure (unreachable socket,
        error status, unparseable body) resolves to None — the caller then
        uses the more compatible of the two GPU paths.
        """
        if self._podman_version_cache is not None:
            return self._podman_version_cache[0]
        version: str | None = None
        try:
            response = await self._request("GET", f"{_LIBPOD_PREFIX}/version")
            payload = response.json()
        except (OutoError, ValueError):
            payload = None
        if isinstance(payload, dict):
            candidate = payload.get("Version")
            if isinstance(candidate, str) and candidate:
                version = candidate
        self._podman_version_cache = (version,)
        return version

    def _build_client(self) -> httpx.AsyncClient:
        transport = httpx.AsyncHTTPTransport(uds=self._settings.podman_socket)
        return httpx.AsyncClient(
            base_url="http://d",
            transport=transport,
            timeout=httpx.Timeout(30.0, connect=5.0),
            headers={"Accept": "application/json"},
        )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
        content: bytes | None = None,
        content_type: str | None = None,
    ) -> httpx.Response:
        client = self._client if self._client is not None else self._build_client()
        headers: dict[str, str] = {}
        if content_type is not None:
            headers["Content-Type"] = content_type
        try:
            response = await client.request(
                method,
                path,
                params=params,
                json=json_body,
                content=content,
                headers=headers,
            )
        except httpx.ConnectError as exc:
            raise OutoError(
                _podman_socket_hint(),
                code="podman_unreachable",
                status_code=503,
            ) from exc
        except httpx.RequestError as exc:
            raise OutoError(
                f"Podman API call failed: {exc}",
                code="podman_unreachable",
                status_code=503,
            ) from exc
        finally:
            if self._owns_client and client is not self._client:
                await client.aclose()

        if response.status_code >= 400:
            self._raise_podman_http_error(response)
        return response

    @staticmethod
    def _raise_podman_http_error(response: httpx.Response) -> None:
        text = response.text
        message = text
        try:
            payload = response.json()
        except (json.JSONDecodeError, ValueError):
            payload = None
        if isinstance(payload, dict):
            message = _extract_first(payload, "message", "error", "cause")
            message = message or text
        if response.request.url.path.endswith("/build") or message == text:
            tail = _build_failure_tail(text)
        else:
            tail = _build_failure_tail(message)
        raise OutoError(
            f"Podman API returned an error: {tail or 'unknown error'}",
            code="podman_api",
            status_code=502,
        )

    @staticmethod
    def _space_labels(owner: str, name: str) -> dict[str, str]:
        return {
            _LABEL_MANAGED: _LABEL_MANAGED_VALUE,
            _LABEL_SPACE: f"{owner}/{name}",
        }

    async def list_managed(self) -> list[ManagedContainer]:
        response = await self._request(
            "GET",
            f"{_LIBPOD_PREFIX}/containers/json",
            params={"all": "true", "filter": f"label={_LABEL_MANAGED}={_LABEL_MANAGED_VALUE}"},
        )
        data = response.json()
        if not isinstance(data, list):
            return []
        managed: list[ManagedContainer] = []
        for entry in data:
            if not isinstance(entry, dict):
                continue
            names = entry.get("Names") or []
            primary = ""
            if isinstance(names, list) and names:
                first = names[0]
                if isinstance(first, str):
                    primary = first.lstrip("/")
            host_port = self._extract_primary_host_port(entry)
            managed.append(
                ManagedContainer(
                    name=primary or "",
                    host_port=host_port,
                    raw=cast(dict[str, Any], entry),
                )
            )
        return managed

    @staticmethod
    def _extract_primary_host_port(entry: dict[str, Any]) -> int | None:
        ports_field = entry.get("Ports")
        candidates: list[int] = []
        if isinstance(ports_field, list):
            for raw in ports_field:
                if not isinstance(raw, dict):
                    continue
                hp = raw.get("host_port") or raw.get("HostPort")
                if hp is None:
                    continue
                try:
                    candidates.append(int(hp))
                except (TypeError, ValueError):
                    continue
        ns = entry.get("NetworkSettings")
        if isinstance(ns, dict):
            ns_ports = ns.get("Ports")
            if isinstance(ns_ports, dict):
                for _container_port, mappings in ns_ports.items():
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
        if not candidates:
            return None
        return min(candidates)

    async def _allocate_host_port(self) -> int:
        start = self._settings.spaces_runtime_port_range_start
        end = self._settings.spaces_runtime_port_range_end
        used = {c.host_port for c in await self.list_managed() if c.host_port is not None}
        for port in range(start, end + 1):
            if port not in used:
                return port
        raise OutoError(
            "All available runtime ports are in use. "
            "Increase the spaces_runtime_port_range_end value.",
            code="podman_api",
            status_code=503,
        )

    async def build_image(self, owner: str, name: str) -> str:
        from outo_models.exceptions import OutoError as _Outo
        from outo_models.spaces.build import make_build_context

        context = make_build_context(owner, name)
        tag = image_tag(owner, name)
        try:
            response = await self._request(
                "POST",
                f"{_LIBPOD_PREFIX}/build",
                params={"t": tag},
                content=context,
                content_type="application/x-tar",
            )
        except _Outo as exc:
            raise _Outo(
                f"Image build failed: {exc}",
                code="space_build_failed",
                status_code=502,
            ) from exc
        body = response.text.strip()
        if body.startswith("{"):
            try:
                payload = json.loads(body)
                if isinstance(payload, dict):
                    image_id = payload.get("Id") or payload.get("id")
                    if isinstance(image_id, str) and image_id:
                        return image_id
            except json.JSONDecodeError:
                pass
        return body

    async def _cdi_probe_finished(self, container: str) -> bool:
        """Poll until the helper container has stopped, or the wait times out.

        The exit code is irrelevant — the specs are the output either way —
        so only "no longer running / never started" ends the wait.
        """
        deadline = asyncio.get_running_loop().time() + _CDI_PROBE_TIMEOUT
        pending = {"created", "configured", "initialized", "running"}
        while True:
            payload = (
                await self._request("GET", f"{_LIBPOD_PREFIX}/containers/{container}/json")
            ).json()
            state = payload.get("State") if isinstance(payload, dict) else None
            status = state.get("Status") if isinstance(state, dict) else None
            if not (isinstance(status, str) and status in pending):
                return True
            if asyncio.get_running_loop().time() >= deadline:
                return False
            await asyncio.sleep(_CDI_PROBE_POLL_INTERVAL)

    async def _read_host_cdi_texts(self, dirs: Sequence[str], image: str) -> tuple[str, ...]:
        """Read the CDI spec files of `dirs` on the host through podman.

        The spec directories are host paths. `outo-models start` normally
        bind mounts them into the server container, but that plumbing can be
        missing or stale while the specs themselves are perfectly valid. The
        podman socket is already the server's host-truth channel, so the
        fallback asks podman itself: a throwaway container of the Space's own
        already-built image gets the same directories bind mounted and cats
        them out through the logs endpoint. No image pull, no new dependency.

        Returns an empty tuple on any failure — a broken helper must fall
        through to the loud, self-diagnosing error, never to a GPU-less
        container.
        """
        container = f"{_CDI_PROBE_PREFIX}{secrets.token_hex(4)}"
        body: dict[str, Any] = {
            "name": container,
            "image": image,
            "labels": {_LABEL_MANAGED: _LABEL_MANAGED_VALUE, _LABEL_CDI_PROBE: "1"},
            "command": ["sh", "-c", _cdi_probe_script(dirs)],
            "mounts": [
                {
                    "source": directory,
                    "destination": directory,
                    "type": "bind",
                    "options": ["ro", "rbind"],
                }
                for directory in dirs
            ],
        }
        try:
            await self._request(
                "POST",
                f"{_LIBPOD_PREFIX}/containers/create",
                json_body=body,
            )
            await self._request("POST", f"{_LIBPOD_PREFIX}/containers/{container}/start")
            if not await self._cdi_probe_finished(container):
                return ()
            response = await self._request(
                "GET",
                f"{_LIBPOD_PREFIX}/containers/{container}/logs",
                params={"stdout": "true", "stderr": "false", "follow": "false"},
            )
        except (OutoError, ValueError):
            return ()
        finally:
            await self._discard_helper(container)
        text = _decode_logs(response.content)
        return (text,) if text.strip() else ()

    async def _discard_helper(self, container: str) -> None:
        """Remove the helper container, tolerating a refusal from podman.

        The cleanup runs on the failure path too, so an error here would
        replace the loud, self-diagnosing resolution error with an opaque
        podman one — exactly the diagnosis the operator needs.
        """
        try:
            await self._delete_container(container)
        except OutoError:
            return

    async def _resolve_gpu_edits(
        self,
        identifiers: Sequence[str],
        image: str,
    ) -> cdi.CdiEdits:
        """Resolve GPU identifiers through the local specs, then the host's.

        Step 1 reads the spec directories inside the server container — the
        fast path, free of any extra podman traffic. Only when that finds
        nothing does step 2 read the same host paths through a throwaway
        helper container. When both fail, the error names both attempts so
        the remaining causes (no specs on the host at all, an unknown GPU id)
        are distinguishable.
        """
        try:
            return cdi.resolve_device(identifiers)
        except ValidationFailedError as local_exc:
            texts = await self._read_host_cdi_texts(cdi.DEFAULT_SPEC_DIRS, image)
            if not texts:
                raise self._both_attempts_failed(local_exc) from local_exc
            specs, _ = cdi.parse_specs("\n".join(texts))
            try:
                return cdi.resolve_edits(specs, identifiers, context=_CDI_HOST_CONTEXT)
            except ValidationFailedError as host_exc:
                raise self._both_attempts_failed(host_exc) from host_exc

    @staticmethod
    def _both_attempts_failed(exc: ValidationFailedError) -> ValidationFailedError:
        """Re-raise a resolution failure with both resolution paths named."""
        return ValidationFailedError(
            f"{exc}\n{_CDI_BOTH_ATTEMPTS}",
            code=exc.code,
        )

    async def start(
        self,
        owner: str,
        name: str,
        *,
        gpu_ids: list[str] | tuple[str, ...] = (),
    ) -> tuple[str, int | None]:
        container = container_name(owner, name)
        # Ordering is load-bearing: callers (`_run_lifecycle`) build the image
        # BEFORE calling `start()`, so a failed build must keep the old
        # container. Never hoist this removal above the build.
        if await self.inspect(owner, name) is not None:
            await self.remove(owner, name)
        host_port = await self._allocate_host_port()
        # libpod decodes this as `specgen.SpecGenerator`, not as a Docker-API
        # config: `env` is a map, ports are numeric uint16, `restart_policy` is
        # a bare string. Go silently drops unknown keys, so a Docker-API field
        # name loses intent instead of erroring — keep this body in specgen terms.
        body: dict[str, Any] = {
            "name": container,
            "image": image_tag(owner, name),
            "labels": self._space_labels(owner, name),
            "env": {"PORT": str(_SPACE_PORT)},
            "portmappings": [
                {
                    "host_ip": "127.0.0.1",
                    "container_port": _SPACE_PORT,
                    "host_port": host_port,
                    "protocol": "tcp",
                }
            ],
            "restart_policy": "always",
        }
        if gpu_ids:
            identifiers = [_cdi_device_name(gpu) for gpu in gpu_ids]
            version = await self.podman_version()
            if version is not None and _major_version(version) >= _GPU_FIELD_MIN_MAJOR:
                body["gpus"] = identifiers
            else:
                # podman < 6.0 has no `gpus` field on the create body and
                # silently ignores unknown JSON keys, so translate the host
                # CDI specs into fields every version understands — reading
                # them locally first and from the host only if that misses.
                edits = await self._resolve_gpu_edits(identifiers, image_tag(owner, name))
                body["devices"] = list(edits.device_nodes)
                body["mounts"] = [*body.get("mounts", []), *edits.mounts]
                body["env"].update(edits.env)

        create_response = await self._request(
            "POST",
            f"{_LIBPOD_PREFIX}/containers/create",
            json_body=body,
        )
        container_id = ""
        payload = create_response.json()
        if isinstance(payload, dict):
            cid = payload.get("Id") or payload.get("id")
            if isinstance(cid, str):
                container_id = cid

        await self._request(
            "POST",
            f"{_LIBPOD_PREFIX}/containers/{container}/start",
        )
        return container_id, host_port

    async def stop(self, owner: str, name: str) -> None:
        container = container_name(owner, name)
        await self._request(
            "POST",
            f"{_LIBPOD_PREFIX}/containers/{container}/stop?t=0",
        )

    async def restart(self, owner: str, name: str) -> None:
        container = container_name(owner, name)
        await self._request(
            "POST",
            f"{_LIBPOD_PREFIX}/containers/{container}/restart",
        )

    async def _delete_container(self, container: str) -> None:
        """Force-remove a container by name, ignoring an already-gone one."""
        client = self._client if self._client is not None else self._build_client()
        try:
            try:
                response = await client.delete(
                    f"{_LIBPOD_PREFIX}/containers/{container}",
                    params={"force": "true", "ignore": "true"},
                )
            except httpx.RequestError as exc:
                raise OutoError(
                    f"Podman API call failed: {exc}",
                    code="podman_unreachable",
                    status_code=503,
                ) from exc
        finally:
            if self._owns_client and client is not self._client:
                await client.aclose()
        if response.status_code >= 400 and response.status_code != 404:
            self._raise_podman_http_error(response)

    async def remove(self, owner: str, name: str) -> None:
        await self._delete_container(container_name(owner, name))

    async def inspect(self, owner: str, name: str) -> dict[str, Any] | None:
        container = container_name(owner, name)
        client = self._client if self._client is not None else self._build_client()
        try:
            try:
                response = await client.get(f"{_LIBPOD_PREFIX}/containers/{container}/json")
            except httpx.RequestError as exc:
                raise OutoError(
                    f"Podman API call failed: {exc}",
                    code="podman_unreachable",
                    status_code=503,
                ) from exc
        finally:
            if self._owns_client and client is not self._client:
                await client.aclose()
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            self._raise_podman_http_error(response)
        payload = response.json()
        if not isinstance(payload, dict):
            return None
        return cast(dict[str, Any], payload)


__all__ = [
    "ManagedContainer",
    "SpaceRuntimeManager",
    "container_name",
    "image_tag",
]
