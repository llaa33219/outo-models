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

import httpx
import pytest

from outo_models.config import Settings, get_settings
from outo_models.exceptions import OutoError
from outo_models.spaces.runtime_manager import (
    ManagedContainer,
    SpaceRuntimeManager,
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

    async def test_create_payload_includes_labels_and_env(self, settings, captured) -> None:
        def h(request: httpx.Request) -> httpx.Response:
            if "/containers/json" in str(request.url):
                return httpx.Response(200, json=[])
            if "/containers/create" in str(request.url):
                return httpx.Response(201, json={"Id": "container-id-2"})
            if "/start" in str(request.url):
                return httpx.Response(204)
            return httpx.Response(200, json={})

        client = _client_for(h, captured)
        manager = SpaceRuntimeManager(settings, client=client)
        await manager.start("alice", "demo", gpu_ids=("0", "1"))
        await client.aclose()
        create_request = next(r for r in captured if str(r.url).endswith("/containers/create"))
        payload = json.loads(create_request.content)
        assert payload["name"] == "outo-space-alice-demo"
        labels = payload["labels"]
        assert labels["outo.space"] == "alice/demo"
        assert labels["outo.managed"] == "true"
        envs = payload["env"]
        assert any(line.startswith("PORT=") for line in envs)
        devices = payload["hostConfig"]["devices"]
        assert {"path": "nvidia.com/gpu=0", "type": "cdi"} in devices
        assert {"path": "nvidia.com/gpu=1", "type": "cdi"} in devices

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
