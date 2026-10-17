"""Unit tests for `outo_models.spaces.runtime_manager`.

# allow: SIZE_OK — the file covers every public method on
# `SpaceRuntimeManager` plus the docker/CDI label expectations and
# image/tag helpers. Splitting along the existing test-class boundaries
# would create five new test files and dilute the "one manager, one
# test file" convention other repo_* tests follow.

The manager is the only module that talks to Podman. Each test wires an
`httpx.MockTransport` against the UDS sentinel base URL (`http://d`)
and verifies either a wire-shape contract or an error-mapping contract.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from outo_models.config import Settings, get_settings
from outo_models.exceptions import OutoError
from outo_models.spaces.runtime_manager import (
    ManagedContainer,
    SpaceRuntimeManager,
    _cdi_device_name,
    container_name,
    image_tag,
)


@pytest.fixture
def settings(tmp_data_dir) -> Settings:
    """Return a settings whose podman socket path is a stub."""
    get_settings.cache_clear()
    return get_settings()


@pytest.fixture
def captured() -> list[httpx.Request]:
    """Yield a list that the mock handler appends to on every call."""
    return []


def _client_for(handler, captured: list[httpx.Request]) -> httpx.AsyncClient:
    """Build a `MockTransport`-backed client and remember every request."""

    def h(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return handler(request)

    transport = httpx.MockTransport(h)
    return httpx.AsyncClient(
        base_url="http://d",
        transport=transport,
        headers={"Accept": "application/json"},
    )


def _create_ok_handler(request: httpx.Request) -> httpx.Response:
    """Answer the three requests `start()` makes: list, create, start."""
    if "/containers/json" in str(request.url):
        return httpx.Response(200, json=[])
    if "/containers/create" in str(request.url):
        return httpx.Response(201, json={"Id": "container-id"})
    if "/start" in str(request.url):
        return httpx.Response(204)
    return httpx.Response(200, json={})


def _create_payload(captured: list[httpx.Request]) -> dict[str, Any]:
    """Return the decoded JSON body of the Space's `/containers/create` call.

    Keyed on the requested container name, not on the endpoint: the host-CDI
    read fallback creates its own throwaway container through the same path.
    """
    for request in captured:
        if not str(request.url).endswith("/containers/create"):
            continue
        payload: dict[str, Any] = json.loads(request.content)
        if str(payload.get("name", "")).startswith("outo-space-"):
            return payload
    raise AssertionError("no Space container create call was made")


def _probe_calls(captured: list[httpx.Request]) -> list[str]:
    """Reduce the helper-container calls to `METHOD endpoint` strings."""
    calls: list[str] = []
    for request in captured:
        path = request.url.path
        is_probe = "outo-cdi-probe-" in path or (
            path.endswith("/containers/create")
            and "outo-cdi-probe-" in request.content.decode(errors="replace")
        )
        if not is_probe:
            continue
        tail = path.split("/containers/")[-1]
        endpoint = (
            tail.rsplit("/", 1)[-1]
            if "/" in tail
            else ("create" if request.method == "POST" else "container")
        )
        calls.append(f"{request.method} {endpoint}")
    return calls


def _version_handler(version: str):
    """Answer `GET /libpod/version` with `version`, then delegate to the stub."""
    inner = _create_ok_handler

    def h(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/version"):
            return httpx.Response(200, json={"Version": version})
        return inner(request)

    return h


def _call_sequence(captured: list[httpx.Request]) -> list[str]:
    """Reduce captured requests to `METHOD /path` strings for order assertions."""
    return [f"{request.method} {request.url.path}" for request in captured]


class TestContainerNameAndImageTag:
    def test_container_name_deterministic(self) -> None:
        assert container_name("alice", "demo") == "outo-space-alice-demo"

    def test_image_tag_deterministic(self) -> None:
        assert image_tag("alice", "demo") == "localhost/outo-space-alice-demo:latest"


class TestListManaged:
    async def test_returns_managed_containers(self, settings, captured) -> None:
        def h(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json=[
                    {
                        "Names": ["/outo-space-alice-app1"],
                        "Ports": [{"HostPort": 20100}],
                    },
                    {
                        "Names": ["/outo-space-alice-app2"],
                        "NetworkSettings": {
                            "Ports": {"8000/tcp": [{"HostPort": 20101}]},
                        },
                    },
                    {"Names": ["unrelated"], "Ports": [{"HostPort": 9999}]},
                ],
            )

        client = _client_for(h, captured)
        manager = SpaceRuntimeManager(settings, client=client)
        rows = await manager.list_managed()
        await client.aclose()
        assert len(rows) == 3
        assert isinstance(rows[0], ManagedContainer)
        assert rows[0].name == "outo-space-alice-app1"
        assert rows[0].host_port == 20100
        assert rows[1].host_port == 20101
        assert rows[2].host_port == 9999

    async def test_filter_sends_label_query(self, settings, captured) -> None:
        def h(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=[])

        client = _client_for(h, captured)
        manager = SpaceRuntimeManager(settings, client=client)
        await manager.list_managed()
        await client.aclose()
        # `params=` encoding in httpx puts `?all=true&filter=label=outo.managed=true`.
        url = str(captured[-1].url)
        assert "label=outo.managed=true" in url.replace("%3D", "=").replace("%3D", "=")


class TestErrorMapping:
    async def test_unreachable_maps_to_podman_unreachable(self, settings) -> None:
        transport = httpx.MockTransport(
            lambda request: (_ for _ in ()).throw(httpx.ConnectError("socket missing"))
        )
        client = httpx.AsyncClient(base_url="http://d", transport=transport)
        manager = SpaceRuntimeManager(settings, client=client)
        with pytest.raises(OutoError) as excinfo:
            await manager.inspect("alice", "demo")
        assert excinfo.value.code == "podman_unreachable"
        assert "Podman" in str(excinfo.value)
        await client.aclose()

    async def test_api_4xx_maps_to_podman_api(self, settings) -> None:
        def h(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={"message": "no such container"})

        client = _client_for(h, [])
        manager = SpaceRuntimeManager(settings, client=client)
        with pytest.raises(OutoError) as excinfo:
            await manager.stop("alice", "demo")
        assert excinfo.value.code == "podman_api"
        assert excinfo.value.status_code == 502
        assert "no such container" in str(excinfo.value)
        await client.aclose()


class TestBuildImage:
    async def test_build_passes_tar_with_correct_content_type(self, settings, captured) -> None:
        # Seed a tiny repo on disk so make_build_context has something.
        import os

        from dulwich import porcelain

        from outo_models.config import get_settings as _gs

        s = _gs()
        data_dir = s.data_dir / "repos" / "alice" / "demo.git"
        os.makedirs(str(s.data_dir / "repos" / "alice"), exist_ok=True)
        work = s.data_dir / "_work"
        work.mkdir(exist_ok=True)
        (work / "app.py").write_text('print("hi")\n')
        porcelain.init(str(work))
        porcelain.add(str(work), paths=["app.py"])
        porcelain.commit(
            str(work),
            message=b"x",
            author=b"a <a@a>",
            committer=b"a <a@a>",
        )
        porcelain.clone(str(work), str(data_dir), bare=True)

        def h(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="sha256:abc123def\n")

        client = _client_for(h, captured)
        manager = SpaceRuntimeManager(settings, client=client)
        image_id = await manager.build_image("alice", "demo")
        await client.aclose()
        assert image_id == "sha256:abc123def"
        # The request body MUST be the in-memory tar — capture it to
        # confirm the binary header bytes are present.
        request = captured[-1]
        assert request.headers["content-type"] == "application/x-tar"
        body = request.read()
        assert body[:2] == b"\x1f\x8b"  # gzip header

    async def test_build_failure_raises_space_build_failed(self, settings) -> None:
        def h(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/build"):
                return httpx.Response(
                    400,
                    json={"message": "Dockerfile missing"},
                )
            return httpx.Response(200, text="ok")

        client = _client_for(h, [])
        manager = SpaceRuntimeManager(settings, client=client)
        # Seed an on-disk repo so `make_build_context` doesn't blow up
        # with FileNotFoundError before reaching the manager.
        import os

        from dulwich import porcelain

        from outo_models.config import get_settings as _gs

        s = _gs()
        data_dir = s.data_dir / "repos" / "alice" / "demo.git"
        os.makedirs(str(s.data_dir / "repos" / "alice"), exist_ok=True)
        work = s.data_dir / "_w2"
        work.mkdir(exist_ok=True)
        (work / "app.py").write_text("x")
        porcelain.init(str(work))
        porcelain.add(str(work), paths=["app.py"])
        porcelain.commit(str(work), message=b"x", author=b"a <a@a>", committer=b"a <a@a>")
        porcelain.clone(str(work), str(data_dir), bare=True)
        with pytest.raises(OutoError) as excinfo:
            await manager.build_image("alice", "demo")
        assert excinfo.value.code == "space_build_failed"
        assert "Dockerfile missing" in str(excinfo.value)
        await client.aclose()


class TestCdiDeviceName:
    """Assignments are stored verbatim (`gpu-0` in the admin UI); the NVIDIA
    CDI device name is either a bare integer index or `GPU-<uuid>`."""

    @pytest.mark.parametrize(
        ("assignment", "expected"),
        [
            ("gpu-0", "0"),
            ("gpu-01", "1"),
            ("gpu-7", "7"),
            ("GPU-2", "2"),
            ("0", "0"),
            ("  gpu-2  ", "2"),
            ("GPU-abc", "GPU-abc"),
            ("", ""),
            ("nonsense", "nonsense"),
        ],
    )
    def test_normalizes_index_form_when_prefixed(self, assignment: str, expected: str) -> None:
        assert _cdi_device_name(assignment) == expected


class TestStartStopAndInspect:
    async def test_allocates_lowest_free_port(self, settings) -> None:
        def h(request: httpx.Request) -> httpx.Response:
            if "/containers/json" in str(request.url):
                return httpx.Response(
                    200,
                    json=[
                        {"Names": ["/outo-space-bob-b2"], "Ports": [{"HostPort": 20002}]},
                    ],
                )
            if request.method == "POST" and "/containers/create" in str(request.url):
                return httpx.Response(201, json={"Id": "container-id-1"})
            if "/start" in str(request.url):
                return httpx.Response(204)
            return httpx.Response(200, json={})

        client = _client_for(h, [])
        manager = SpaceRuntimeManager(settings, client=client)
        object.__setattr__(settings, "spaces_runtime_port_range_start", 20000)
        object.__setattr__(settings, "spaces_runtime_port_range_end", 20010)
        container_id, port = await manager.start("alice", "demo")
        await client.aclose()
        assert container_id == "container-id-1"
        # 20000 / 20001 should be free, only 20002 is in use → next free is 20000.
        assert port == 20000

    async def test_create_payload_matches_libpod_specgenerator(self, settings, captured) -> None:
        client = _client_for(_create_ok_handler, captured)
        manager = SpaceRuntimeManager(settings, client=client)
        _, host_port = await manager.start("alice", "demo")
        await client.aclose()
        payload = _create_payload(captured)
        assert payload["name"] == "outo-space-alice-demo"
        assert payload["image"] == "localhost/outo-space-alice-demo:latest"
        assert payload["labels"] == {"outo.managed": "true", "outo.space": "alice/demo"}
        # SpecGenerator.Env is map[string]string — a JSON list here is the
        # `cannot unmarshal array into Go struct field SpecGenerator.env` failure.
        assert payload["env"] == {"PORT": "8000"}
        assert payload["restart_policy"] == "always"
        # nettypes.PortMapping ports are uint16 — numbers, not Docker strings.
        assert payload["portmappings"] == [
            {
                "host_ip": "127.0.0.1",
                "container_port": 8000,
                "host_port": host_port,
                "protocol": "tcp",
            }
        ]

    async def test_create_payload_omits_docker_api_fields(self, settings, captured) -> None:
        client = _client_for(_create_ok_handler, captured)
        manager = SpaceRuntimeManager(settings, client=client)
        await manager.start("alice", "demo")
        await client.aclose()
        payload = _create_payload(captured)
        # Go ignores unknown keys, so these fail as silently lost intent
        # (no port binding, no restart policy, no devices) rather than an error.
        assert "hostConfig" not in payload
        assert "exposedPorts" not in payload

    async def test_create_payload_gpus_normalize_assignment_ids(self, settings, captured) -> None:
        client = _client_for(_version_handler("6.0.0"), captured)
        manager = SpaceRuntimeManager(settings, client=client)
        await manager.start("alice", "demo", gpu_ids=("gpu-0", "GPU-abc"))
        await client.aclose()
        payload = _create_payload(captured)
        assert payload["gpus"] == ["0", "GPU-abc"]

    async def test_create_payload_omits_gpus_when_unassigned(self, settings, captured) -> None:
        client = _client_for(_create_ok_handler, captured)
        manager = SpaceRuntimeManager(settings, client=client)
        await manager.start("alice", "demo")
        await client.aclose()
        payload = _create_payload(captured)
        assert "gpus" not in payload

    async def test_start_removes_an_existing_container_before_create(
        self, settings, captured
    ) -> None:
        # Given: podman already holds `outo-space-alice-demo` (running, stopped
        # or created) — create without removing it dies with
        # "the container name ... is already in use by <id>".
        def h(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "/containers/outo-space-alice-demo/json" in url:
                return httpx.Response(200, json={"Id": "old-id", "State": {"Status": "running"}})
            if request.method == "DELETE" and "/containers/outo-space-alice-demo" in url:
                return httpx.Response(200, json={"Id": "old-id"})
            if "/containers/json" in url:
                return httpx.Response(200, json=[])
            if "/containers/create" in url:
                return httpx.Response(201, json={"Id": "new-id"})
            if "/start" in url:
                return httpx.Response(204)
            return httpx.Response(200, json={})

        client = _client_for(h, captured)
        manager = SpaceRuntimeManager(settings, client=client)
        # When
        container_id, _ = await manager.start("alice", "demo")
        await client.aclose()

        # Then: exactly one fresh container, and remove ran before create.
        assert container_id == "new-id"
        sequence = _call_sequence(captured)
        remove_index = sequence.index("DELETE /v4.0.0/libpod/containers/outo-space-alice-demo")
        create_index = sequence.index("POST /v4.0.0/libpod/containers/create")
        assert remove_index < create_index
        start_call = "POST /v4.0.0/libpod/containers/outo-space-alice-demo/start"
        assert sequence[create_index + 1] == start_call
        # The removal is force+ignore so a running container clears too.
        delete_request = captured[remove_index]
        assert "force=true" in str(delete_request.url)

    async def test_start_without_existing_container_skips_remove(self, settings, captured) -> None:
        # Given: first boot — inspect reports 404.
        def h(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "/containers/outo-space-alice-demo/json" in url:
                return httpx.Response(404, json={"message": "no such container"})
            if "/containers/json" in url:
                return httpx.Response(200, json=[])
            if "/containers/create" in url:
                return httpx.Response(201, json={"Id": "container-id-1"})
            if "/start" in url:
                return httpx.Response(204)
            return httpx.Response(200, json={})

        client = _client_for(h, captured)
        manager = SpaceRuntimeManager(settings, client=client)
        # When
        container_id, _ = await manager.start("alice", "demo")
        await client.aclose()

        # Then: no remove call on the lean path.
        assert container_id == "container-id-1"
        assert not [call for call in _call_sequence(captured) if call.startswith("DELETE")]

    async def test_inspect_returns_payload_or_none(self, settings) -> None:
        def h(request: httpx.Request) -> httpx.Response:
            if request.method == "GET" and "/containers/outo-space-alice-demo/json" in str(
                request.url
            ):
                return httpx.Response(
                    200,
                    json={
                        "Id": "i",
                        "State": {"Status": "running"},
                        "NetworkSettings": {
                            "Ports": {"8000/tcp": [{"HostPort": "20300"}]},
                        },
                    },
                )
            return httpx.Response(404, json={"message": "missing"})

        client = _client_for(h, [])
        manager = SpaceRuntimeManager(settings, client=client)
        first = await manager.inspect("alice", "demo")
        assert first is not None
        assert first["State"]["Status"] == "running"
        await client.aclose()

        # Missing-container path: 404 must resolve to `None`.
        def h_missing(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={"message": "missing"})

        client2 = _client_for(h_missing, [])
        manager2 = SpaceRuntimeManager(settings, client=client2)
        result = await manager2.inspect("alice", "demo")
        assert result is None
        await client2.aclose()

    async def test_stop_and_restart_dispatch(self, settings, captured) -> None:
        def h(request: httpx.Request) -> httpx.Response:
            if "/stop" in str(request.url):
                return httpx.Response(204)
            if "/restart" in str(request.url):
                return httpx.Response(204)
            return httpx.Response(404, json={"message": "noop"})

        client = _client_for(h, captured)
        manager = SpaceRuntimeManager(settings, client=client)
        await manager.stop("alice", "demo")
        await manager.restart("alice", "demo")
        await client.aclose()
        paths = [str(r.url) for r in captured]
        assert any("containers/outo-space-alice-demo/stop" in p for p in paths)
        assert any("containers/outo-space-alice-demo/restart" in p for p in paths)


# A spec shaped like the live `nvidia-ctk cdi generate` output: hooks
# included. They must not stop the device from resolving — podman applies
# them itself.
_CDI_SPEC = """\
cdiVersion: 0.5.0
kind: nvidia.com/gpu
containerEdits:
  env:
    - NVIDIA_VISIBLE_DEVICES=all
devices:
  "0":
    containerEdits:
      hooks:
        - hookName: createContainer
          path: /bin/true
      deviceNodes:
        - path: /dev/nvidia0
          type: c
          major: 195
          minor: 0
          fileMode: 0666
      mounts:
        - hostPath: /usr/lib/libnvidia-container.so.1
          containerPath: /usr/lib/libnvidia-container.so.1
"""


@pytest.fixture
def cdi_spec_dir(tmp_path, monkeypatch) -> Path:
    """Point the CDI resolver at a synthetic nvidia spec on disk."""
    from outo_models.spaces import cdi

    directory = tmp_path / "cdi"
    directory.mkdir()
    (directory / "nvidia.yaml").write_text(_CDI_SPEC, encoding="utf-8")
    monkeypatch.setattr(cdi, "DEFAULT_SPEC_DIRS", (str(directory),))
    return directory


class TestGpuVersionGate:
    """`gpus` only exists on the libpod API from podman 6.0 on; older releases
    drop the unknown JSON key silently, so the create body names the devices
    as CDI qualified names and podman resolves them itself
    (`ExtractCDIDevices` → `libpod.WithCDI` → `registry.InjectDevices`,
    which applies the full containerEdits, hooks included)."""

    async def test_modern_podman_uses_gpus_field(self, settings, captured) -> None:
        client = _client_for(_version_handler("6.0.0"), captured)
        manager = SpaceRuntimeManager(settings, client=client)
        await manager.start("alice", "demo", gpu_ids=("gpu-0",))
        await client.aclose()
        payload = _create_payload(captured)
        assert payload["gpus"] == ["0"]
        assert "devices" not in payload
        assert "mounts" not in payload

    async def test_old_podman_gets_qualified_cdi_device_names(
        self, settings, captured, cdi_spec_dir
    ) -> None:
        client = _client_for(_version_handler("4.9.3"), captured)
        manager = SpaceRuntimeManager(settings, client=client)
        await manager.start("alice", "demo", gpu_ids=("gpu-0",))
        await client.aclose()
        payload = _create_payload(captured)
        # `gpus` on 4.x is silently dropped by Go — never send it there.
        assert "gpus" not in payload
        assert payload["devices"] == [{"path": "nvidia.com/gpu=0"}]

    async def test_old_podman_body_carries_no_translated_edits(
        self, settings, captured, cdi_spec_dir
    ) -> None:
        # The field failure this delivery replaces: the server resolved the
        # spec itself and merged device nodes, NVIDIA library mounts and
        # NVIDIA_* env into the body — which silently dropped the spec's
        # hooks. Nothing but the device names goes over the wire now.
        client = _client_for(_version_handler("4.9.3"), captured)
        manager = SpaceRuntimeManager(settings, client=client)
        await manager.start("alice", "demo", gpu_ids=("gpu-0",))
        await client.aclose()
        payload = _create_payload(captured)
        assert payload["devices"] == [{"path": "nvidia.com/gpu=0"}]
        assert "mounts" not in payload
        assert payload["env"] == {"PORT": "8000"}

    async def test_podman_five_uses_the_fallback(self, settings, captured, cdi_spec_dir) -> None:
        client = _client_for(_version_handler("5.6.0"), captured)
        manager = SpaceRuntimeManager(settings, client=client)
        await manager.start("alice", "demo", gpu_ids=("gpu-0",))
        await client.aclose()
        payload = _create_payload(captured)
        assert "gpus" not in payload
        assert payload["devices"] == [{"path": "nvidia.com/gpu=0"}]

    async def test_version_fetch_failure_falls_back_to_cdi(
        self, settings, captured, cdi_spec_dir
    ) -> None:
        # An unreachable / version-less podman must still get a working
        # container: qualified device names are the more compatible spelling.
        def h(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/version"):
                return httpx.Response(500, json={"message": "boom"})
            return _create_ok_handler(request)

        client = _client_for(h, captured)
        manager = SpaceRuntimeManager(settings, client=client)
        await manager.start("alice", "demo", gpu_ids=("gpu-0",))
        await client.aclose()
        payload = _create_payload(captured)
        assert "gpus" not in payload
        assert payload["devices"] == [{"path": "nvidia.com/gpu=0"}]

    async def test_every_assigned_gpu_is_named(self, settings, captured, cdi_spec_dir) -> None:
        client = _client_for(_version_handler("4.9.3"), captured)
        manager = SpaceRuntimeManager(settings, client=client)
        await manager.start("alice", "demo", gpu_ids=("gpu-0", "GPU-abc"))
        await client.aclose()
        assert _create_payload(captured)["devices"] == [
            {"path": "nvidia.com/gpu=0"},
            {"path": "nvidia.com/gpu=GPU-abc"},
        ]

    async def test_successful_start_never_runs_the_spec_ladder(
        self, settings, captured, cdi_spec_dir
    ) -> None:
        # The resolver costs a podman round-trip; on the happy path podman
        # resolved the names itself, so the local read and the helper
        # container must both stay untouched.
        client = _client_for(_version_handler("4.9.3"), captured)
        manager = SpaceRuntimeManager(settings, client=client)
        await manager.start("alice", "demo", gpu_ids=("gpu-0",))
        await client.aclose()
        assert _probe_calls(captured) == []

    async def test_modern_podman_never_touches_the_helper(
        self, settings, captured, no_local_specs
    ) -> None:
        # `gpus` needs no names and no translation, so the ladder — local
        # read included — must not run at all.
        client = _client_for(_version_handler("6.0.0"), captured)
        manager = SpaceRuntimeManager(settings, client=client)
        await manager.start("alice", "demo", gpu_ids=("gpu-0",))
        await client.aclose()
        assert _probe_calls(captured) == []

    async def test_version_is_fetched_once_per_manager(
        self, settings, captured, cdi_spec_dir
    ) -> None:
        client = _client_for(_version_handler("4.9.3"), captured)
        manager = SpaceRuntimeManager(settings, client=client)
        await manager.start("alice", "demo", gpu_ids=("gpu-0",))
        await manager.start("alice", "demo2", gpu_ids=("gpu-0",))
        await client.aclose()
        version_calls = [c for c in _call_sequence(captured) if c.endswith("/version")]
        assert len(version_calls) == 1

    async def test_version_is_not_fetched_without_gpus(self, settings, captured) -> None:
        # No GPU assignment → no version round-trip on the hot path.
        client = _client_for(_create_ok_handler, captured)
        manager = SpaceRuntimeManager(settings, client=client)
        await manager.start("alice", "demo")
        await client.aclose()
        assert not [c for c in _call_sequence(captured) if c.endswith("/version")]


_PROBE_SPEC_STREAM = f'---\n{_CDI_SPEC}---\nkind: example.com/gpu\ndevices:\n  "0": {{}}\n'

_PODMAN_REFUSAL_TEXT = "no such device nvidia.com/gpu=0"

_PODMAN_REFUSAL = httpx.Response(500, json={"message": _PODMAN_REFUSAL_TEXT})


def _probe_handler(logs: httpx.Response, *, refusal: httpx.Response = _PODMAN_REFUSAL):
    """Answer the helper-container lifecycle, refuse the Space's own create."""

    def h(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if "outo-cdi-probe-" in path:
            if path.endswith("/logs"):
                return logs
            if path.endswith("/start"):
                return httpx.Response(204)
            if path.endswith("/json"):
                return httpx.Response(200, json={"State": {"Status": "exited"}})
            return httpx.Response(200, json={"Id": "probe-id"})
        if path.endswith("/containers/create") and "outo-cdi-probe" in request.content.decode():
            return httpx.Response(201, json={"Id": "probe-id"})
        if path.endswith("/containers/create"):
            return refusal
        return _version_handler("4.9.3")(request)

    return h


@pytest.fixture
def no_local_specs(tmp_path, monkeypatch):
    """Give the resolver an empty directory, as an unmounted /etc/cdi looks."""
    from outo_models.spaces import cdi

    empty = tmp_path / "empty-cdi"
    empty.mkdir()
    monkeypatch.setattr(cdi, "DEFAULT_SPEC_DIRS", (str(empty),))
    return empty


class TestGpuCreateFailureDiagnosis:
    """The ladder runs only after podman refused the container.

    Podman's own CDI path is the delivery, so its refusal is the only thing
    the server's resolver can add anything to. It then answers with what it
    actually found — or, when it does find the device, keeps podman's own
    message, which is then the truth.
    """

    async def test_missing_cdi_spec_fails_loudly_on_old_podman(
        self, settings, captured, tmp_path, monkeypatch
    ) -> None:
        # Given: podman 4.x refuses the create and no CDI spec exists on the
        # host either. A silent GPU-less container is what this prevents.
        from outo_models.spaces import cdi

        empty = tmp_path / "empty-cdi"
        empty.mkdir()
        monkeypatch.setattr(cdi, "DEFAULT_SPEC_DIRS", (str(empty),))
        client = _client_for(
            _probe_handler(httpx.Response(200, content=b""), refusal=_PODMAN_REFUSAL), captured
        )
        manager = SpaceRuntimeManager(settings, client=client)
        with pytest.raises(OutoError) as excinfo:
            await manager.start("alice", "demo", gpu_ids=("gpu-0",))
        await client.aclose()
        message = str(excinfo.value)
        assert excinfo.value.code == "space_gpu_cdi_unresolved"
        assert "nvidia.com/gpu" in message
        # The operator sees both: the resolver's diagnosis and podman's text.
        assert "What the resolver actually saw" in message
        assert _PODMAN_REFUSAL_TEXT in message

    async def test_undefined_device_reports_the_defined_ones(
        self, settings, captured, no_local_specs
    ) -> None:
        # Given: the helper reads the specs, but the assigned id is not one
        # of them — the message must list what IS defined, next to podman's.
        client = _client_for(
            _probe_handler(httpx.Response(200, content=_PROBE_SPEC_STREAM.encode())), captured
        )
        manager = SpaceRuntimeManager(settings, client=client)
        with pytest.raises(OutoError) as excinfo:
            await manager.start("alice", "demo", gpu_ids=("GPU-nonexistent-uuid",))
        await client.aclose()
        message = str(excinfo.value)
        assert excinfo.value.code == "space_gpu_cdi_unresolved"
        assert "defined devices: 0" in message
        assert "helper container" in message
        assert "Both resolution paths were attempted" in message
        assert _PODMAN_REFUSAL_TEXT in message

    async def test_resolvable_device_keeps_the_podman_error(
        self, settings, captured, no_local_specs
    ) -> None:
        # Given: the specs on the host DO define the device. Then podman's
        # refusal is the truth — the resolver must not overwrite it.
        client = _client_for(
            _probe_handler(httpx.Response(200, content=_PROBE_SPEC_STREAM.encode())), captured
        )
        manager = SpaceRuntimeManager(settings, client=client)
        with pytest.raises(OutoError) as excinfo:
            await manager.start("alice", "demo", gpu_ids=("gpu-0",))
        await client.aclose()
        assert excinfo.value.code == "podman_api"
        assert _PODMAN_REFUSAL_TEXT in str(excinfo.value)
        assert "space_gpu_cdi_unresolved" not in str(excinfo.value)

    async def test_mounted_specs_answer_the_diagnosis_without_the_helper(
        self, settings, captured, cdi_spec_dir
    ) -> None:
        # Given: the specs are mounted and define the device, so the local
        # read answers the refusal and the host read is never needed.
        client = _client_for(_probe_handler(httpx.Response(200, content=b"")), captured)
        manager = SpaceRuntimeManager(settings, client=client)
        with pytest.raises(OutoError) as excinfo:
            await manager.start("alice", "demo", gpu_ids=("gpu-0",))
        await client.aclose()
        assert excinfo.value.code == "podman_api"
        assert _PODMAN_REFUSAL_TEXT in str(excinfo.value)
        assert _probe_calls(captured) == []

    async def test_helper_failure_still_reports_the_resolver_diagnosis(
        self, settings, captured, no_local_specs
    ) -> None:
        # Given: the helper itself broke (podman refused the logs call).
        # A silent GPU-less container is what this whole path prevents.
        client = _client_for(
            _probe_handler(httpx.Response(500, json={"message": "log gone"})), captured
        )
        manager = SpaceRuntimeManager(settings, client=client)
        with pytest.raises(OutoError) as excinfo:
            await manager.start("alice", "demo", gpu_ids=("gpu-0",))
        await client.aclose()
        assert excinfo.value.code == "space_gpu_cdi_unresolved"
        assert "Both resolution paths were attempted" in str(excinfo.value)
        # The helper is removed even on the failure path.
        assert _probe_calls(captured)[-1] == "DELETE container"

    async def test_failed_helper_removal_does_not_mask_the_diagnosis(
        self, settings, captured, no_local_specs
    ) -> None:
        # Given: podman refuses the cleanup call as well. The ladder must
        # still end in the self-diagnosing error, not in an opaque podman_api
        # that hides which GPU could not be resolved.
        inner = _probe_handler(httpx.Response(500, json={"message": "log gone"}))

        def h(request: httpx.Request) -> httpx.Response:
            if request.method == "DELETE" and "outo-cdi-probe-" in request.url.path:
                return httpx.Response(500, json={"message": "still busy"})
            return inner(request)

        client = _client_for(h, captured)
        manager = SpaceRuntimeManager(settings, client=client)
        with pytest.raises(OutoError) as excinfo:
            await manager.start("alice", "demo", gpu_ids=("gpu-0",))
        await client.aclose()
        assert excinfo.value.code == "space_gpu_cdi_unresolved"
        assert "Both resolution paths were attempted" in str(excinfo.value)

    async def test_helper_is_removed_when_it_never_exits(
        self, settings, captured, no_local_specs, monkeypatch
    ) -> None:
        # A probe container stuck in `running` must not block the Space
        # start forever; it is removed and the loud error is raised.
        from outo_models.spaces import runtime_manager

        monkeypatch.setattr(runtime_manager, "_CDI_PROBE_TIMEOUT", 0.2)
        monkeypatch.setattr(runtime_manager, "_CDI_PROBE_POLL_INTERVAL", 0.01)

        def h(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "outo-cdi-probe-" in url and url.endswith("/json"):
                return httpx.Response(200, json={"State": {"Status": "running"}})
            return _probe_handler(httpx.Response(200, content=b""))(request)

        client = _client_for(h, captured)
        manager = SpaceRuntimeManager(settings, client=client)
        with pytest.raises(OutoError) as excinfo:
            await manager.start("alice", "demo", gpu_ids=("gpu-0",))
        await client.aclose()
        assert excinfo.value.code == "space_gpu_cdi_unresolved"
        assert _probe_calls(captured)[-1] == "DELETE container"

    async def test_transport_failure_is_enriched_too(
        self, settings, captured, no_local_specs
    ) -> None:
        # Given: the socket drops on the create call itself. The unreachable
        # error still carries the resolver's diagnosis rather than swallowing
        # it, because "podman is down" and "podman rejected the device" need
        # different fixes.
        def h(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if "outo-cdi-probe-" in path:
                return _probe_handler(httpx.Response(200, content=b""))(request)
            if path.endswith("/containers/create"):
                raise httpx.ConnectError("socket gone")
            return _version_handler("4.9.3")(request)

        client = _client_for(h, captured)
        manager = SpaceRuntimeManager(settings, client=client)
        with pytest.raises(OutoError) as excinfo:
            await manager.start("alice", "demo", gpu_ids=("gpu-0",))
        await client.aclose()
        message = str(excinfo.value)
        assert excinfo.value.code == "space_gpu_cdi_unresolved"
        assert "Both resolution paths were attempted" in message
        assert "Podman refused the container" in message


class TestHostCdiReadFallback:
    """The server container does not always see the host's CDI specs.

    With the directories mounted the resolver reads local files at zero cost.
    When that plumbing is missing or stale the specs live only on the host, so
    the fallback reads them through a throwaway podman helper container that
    bind mounts the same host paths and cats them out. A deployment that
    never mounted anything must therefore not need an operator step.
    """

    async def test_reads_the_host_specs_through_the_helper(
        self, settings, captured, no_local_specs
    ) -> None:
        # Given: podman 4.x refused the create, GPU assigned, empty spec dirs
        # in the server container — the specs exist only on the host.
        client = _client_for(
            _probe_handler(httpx.Response(200, content=_PROBE_SPEC_STREAM.encode())), captured
        )
        manager = SpaceRuntimeManager(settings, client=client)

        # When
        with pytest.raises(OutoError):
            await manager.start("alice", "demo", gpu_ids=("gpu-0",))
        await client.aclose()

        # Then: the create body still carried only the qualified name, and
        # the helper ran exactly create → start → inspect → logs → remove.
        assert _create_payload(captured)["devices"] == [{"path": "nvidia.com/gpu=0"}]
        assert _probe_calls(captured) == [
            "POST create",
            "POST start",
            "GET json",
            "GET logs",
            "DELETE container",
        ]

    async def test_helper_create_body_binds_the_host_dirs_read_only(
        self, settings, captured, tmp_path, monkeypatch
    ) -> None:
        # Given: specs the resolver cannot see locally, on host paths the
        # server container never received a mount for.
        from outo_models.spaces import cdi

        host_dirs = ("/etc/cdi", "/opt/cdi")
        monkeypatch.setattr(cdi, "DEFAULT_SPEC_DIRS", host_dirs)
        client = _client_for(
            _probe_handler(httpx.Response(200, content=_PROBE_SPEC_STREAM.encode())), captured
        )
        manager = SpaceRuntimeManager(settings, client=client)

        # When
        with pytest.raises(OutoError):
            await manager.start("alice", "demo", gpu_ids=("gpu-0",))
        await client.aclose()

        # Then: the helper bind mounts each host dir onto itself, read-only,
        # and cats every spec suffix out of it. The image is the Space's own
        # already-built image — the helper runs no new image pull.
        body = json.loads(
            next(
                request.content
                for request in captured
                if str(request.url).endswith("/containers/create")
                and "outo-cdi-probe-" in request.content.decode()
            )
        )
        assert body["image"] == "localhost/outo-space-alice-demo:latest"
        assert body["labels"] == {"outo.managed": "true", "outo.cdi-probe": "1"}
        assert body["mounts"] == [
            {
                "source": directory,
                "destination": directory,
                "type": "bind",
                "options": ["ro", "rbind"],
            }
            for directory in host_dirs
        ]
        assert body["command"][:2] == ["sh", "-c"]
        script = body["command"][2]
        assert '"/opt/cdi"/*.json' in script
        assert '"/etc/cdi"/*.yaml' in script
        assert "cat" in script

    async def test_unmatched_globs_do_not_fail_the_helper_script(
        self, settings, captured, no_local_specs
    ) -> None:
        # A dir with only .yaml specs still globs .yml/.json — the shell must
        # skip the unmatched pattern instead of handing `cat` a literal one.
        client = _client_for(
            _probe_handler(httpx.Response(200, content=_PROBE_SPEC_STREAM.encode())), captured
        )
        manager = SpaceRuntimeManager(settings, client=client)
        with pytest.raises(OutoError):
            await manager.start("alice", "demo", gpu_ids=("gpu-0",))
        await client.aclose()
        script = json.loads(
            next(
                request.content
                for request in captured
                if str(request.url).endswith("/containers/create")
                and "outo-cdi-probe-" in request.content.decode()
            )
        )["command"][2]
        assert '[ -f "$f" ]' in script
        assert _create_payload(captured)["devices"] == [{"path": "nvidia.com/gpu=0"}]

    async def test_multiplexed_log_frames_are_decoded(
        self, settings, captured, no_local_specs
    ) -> None:
        # Given: podman's libpod logs endpoint writes an 8-byte stream frame
        # before every line (it omits those only for compat requests).
        payload = _PROBE_SPEC_STREAM.encode()
        framed = b"\x01\x00\x00\x00" + len(payload).to_bytes(4, "big") + payload
        client = _client_for(_probe_handler(httpx.Response(200, content=framed)), captured)
        manager = SpaceRuntimeManager(settings, client=client)

        # When
        with pytest.raises(OutoError) as excinfo:
            await manager.start("alice", "demo", gpu_ids=("gpu-0",))
        await client.aclose()

        # Then: the frames decoded into a stream the pure parser accepts, so
        # the device resolved and podman's own refusal is what remains.
        assert excinfo.value.code == "podman_api"
        assert _PODMAN_REFUSAL_TEXT in str(excinfo.value)

    async def test_plain_log_text_is_accepted_unchanged(
        self, settings, captured, no_local_specs
    ) -> None:
        client = _client_for(
            _probe_handler(httpx.Response(200, content=_PROBE_SPEC_STREAM.encode())), captured
        )
        manager = SpaceRuntimeManager(settings, client=client)
        with pytest.raises(OutoError) as excinfo:
            await manager.start("alice", "demo", gpu_ids=("gpu-0",))
        await client.aclose()
        assert excinfo.value.code == "podman_api"
        assert _PODMAN_REFUSAL_TEXT in str(excinfo.value)
