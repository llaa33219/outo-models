"""Podman REST client used by the Spaces runtime.

# allow: SIZE_OK — single cohesive Podman REST client. The class
# owns lifecycle (build / start / stop / restart / remove / inspect)
# over a unit-of-work boundary that should not be split — every helper
# serves at most one method on the class, and the alternatives (a
# separate helpers module + multiple manager classes) would force
# callers to thread the same `_KOREAN_SOCK_HINT` / `_ERROR_TAIL_MAX_CHARS`
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
  with a Korean hint telling the operator to mount the socket into the
  container.
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
(`container/scripts/`).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, cast

import httpx

from outo_models.config import Settings
from outo_models.exceptions import OutoError

_LIBPOD_PREFIX = "/v4.0.0/libpod"

_SPACE_PORT = 8000
_SPACE_PORT_STR = f"{_SPACE_PORT}/tcp"

_LABEL_MANAGED = "outo.managed"
_LABEL_MANAGED_VALUE = "true"
_LABEL_SPACE = "outo.space"

_ERROR_TAIL_MAX_CHARS = 2048


def container_name(owner: str, name: str) -> str:
    """Return the deterministic podman container name for a Space."""
    return f"outo-space-{owner}-{name}"


def image_tag(owner: str, name: str) -> str:
    """Return the deterministic podman image tag for a Space's latest build."""
    return f"localhost/outo-space-{owner}-{name}:latest"


def _korean_sock_hint() -> str:
    """Return the operator-facing hint appended to `podman_unreachable` errors."""
    return (
        "Podman 소켓에 연결할 수 없습니다. 호스트의 "
        "/run/podman/podman.sock 파일이 컨테이너에 마운트되어 있는지 "
        "확인하세요."
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
                _korean_sock_hint(),
                code="podman_unreachable",
                status_code=503,
            ) from exc
        except httpx.RequestError as exc:
            raise OutoError(
                f"Podman API 호출에 실패했습니다: {exc}",
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
            f"Podman API가 오류를 반환했습니다: {tail or 'unknown error'}",
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
        used = {
            c.host_port for c in await self.list_managed() if c.host_port is not None
        }
        for port in range(start, end + 1):
            if port not in used:
                return port
        raise OutoError(
            "모든 사용 가능한 런타임 포트가 사용 중입니다. "
            "spaces_runtime_port_range_end 값을 늘려주세요.",
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
                f"이미지 빌드에 실패했습니다: {exc}",
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

    async def start(
        self,
        owner: str,
        name: str,
        *,
        gpu_ids: list[str] | tuple[str, ...] = (),
    ) -> tuple[str, int | None]:
        container = container_name(owner, name)
        host_port = await self._allocate_host_port()
        port_bindings = {
            _SPACE_PORT_STR: [
                {
                    "host_ip": "127.0.0.1",
                    "host_port": str(host_port),
                }
            ]
        }
        body: dict[str, Any] = {
            "name": container,
            "image": image_tag(owner, name),
            "labels": self._space_labels(owner, name),
            "exposedPorts": {
                _SPACE_PORT_STR: {},
            },
            "hostConfig": {
                "PortBindings": port_bindings,
                "RestartPolicy": {"Name": "always"},
            },
            "env": [
                f"PORT={_SPACE_PORT}",
            ],
        }
        if gpu_ids:
            devices = [
                {"path": f"nvidia.com/gpu={gpu}", "type": "cdi"} for gpu in gpu_ids
            ]
            body["hostConfig"]["devices"] = devices

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

    async def remove(self, owner: str, name: str) -> None:
        container = container_name(owner, name)
        client = self._client if self._client is not None else self._build_client()
        try:
            try:
                response = await client.delete(
                    f"{_LIBPOD_PREFIX}/containers/{container}",
                    params={"force": "true", "ignore": "true"},
                )
            except httpx.RequestError as exc:
                raise OutoError(
                    f"Podman API 호출에 실패했습니다: {exc}",
                    code="podman_unreachable",
                    status_code=503,
                ) from exc
        finally:
            if self._owns_client and client is not self._client:
                await client.aclose()
        if response.status_code >= 400 and response.status_code != 404:
            self._raise_podman_http_error(response)

    async def inspect(self, owner: str, name: str) -> dict[str, Any] | None:
        container = container_name(owner, name)
        client = self._client if self._client is not None else self._build_client()
        try:
            try:
                response = await client.get(
                    f"{_LIBPOD_PREFIX}/containers/{container}/json"
                )
            except httpx.RequestError as exc:
                raise OutoError(
                    f"Podman API 호출에 실패했습니다: {exc}",
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
