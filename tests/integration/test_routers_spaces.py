"""Integration tests for the spaces router (CRUD + runtime stub)."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from outo_models.db import WebSetting
from outo_models.server.routers.spaces import _load_owner_gpu_ids

# Fixtures `app`, `factory`, `seed_approved_user` are auto-discovered from
# tests/integration/conftest.py.


class TestCreateSpace:
    """`POST /api/spaces` validates the SDK and writes a sidecar."""

    async def test_create_with_supported_sdk_succeeds(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        client.post(
            "/api/auth/login",
            json={"username": "alice", "password": "correct horse battery staple"},
        )
        response = client.post(
            "/api/spaces",
            json={"name": "demo", "sdk": "gradio", "visibility": "public"},
        )
        assert response.status_code == 201
        body = response.json()
        assert body["name"] == "demo"
        assert body["sdk"] == "gradio"
        assert body["visibility"] == "public"

    async def test_create_with_unsupported_sdk_4xx(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="bob")
        client.post(
            "/api/auth/login",
            json={"username": "bob", "password": "correct horse battery staple"},
        )
        response = client.post(
            "/api/spaces",
            json={"name": "demo", "sdk": "huggingface"},
        )
        # Either 404 (NotFoundError) or 422 (validation) is acceptable;
        # the contract is "not 201".
        assert response.status_code in (404, 422)


class TestGetSpace:
    """`GET /api/spaces/{owner}/{name}` includes the runtime block."""

    async def test_static_runtime_block_ignores_podman(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        # A `static` Space is served from its exported site dir, so an
        # unreachable Podman socket is irrelevant: the state is `stopped`
        # (nothing exported yet), never `failed`.
        client, _, _ = app
        await seed_approved_user(username="carol")
        client.post(
            "/api/auth/login",
            json={"username": "carol", "password": "correct horse battery staple"},
        )
        client.post(
            "/api/spaces",
            json={"name": "showcase", "sdk": "static", "visibility": "public"},
        )
        response = client.get("/api/spaces/carol/showcase")
        assert response.status_code == 200
        runtime = response.json()["runtime"]
        assert runtime["state"] == "stopped"
        assert runtime["url"] is None

    async def test_container_runtime_block_reports_failed_state_when_podman_unreachable(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        # The runtime defaults ON; for a container Space an unreachable
        # Podman socket falls back to `failed` with the operator hint in
        # `message` rather than a 500.
        client, _, _ = app
        await seed_approved_user(username="carol")
        client.post(
            "/api/auth/login",
            json={"username": "carol", "password": "correct horse battery staple"},
        )
        client.post(
            "/api/spaces",
            json={"name": "showcase", "sdk": "docker", "visibility": "public"},
        )
        response = client.get("/api/spaces/carol/showcase")
        assert response.status_code == 200
        runtime = response.json()["runtime"]
        assert runtime["state"] == "failed"
        assert runtime["url"] is None


class TestListSpaces:
    """`GET /api/spaces` respects visibility filters."""

    async def test_anonymous_sees_only_public(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="dave")
        client.post(
            "/api/auth/login",
            json={"username": "dave", "password": "correct horse battery staple"},
        )
        client.post(
            "/api/spaces",
            json={"name": "public-space", "sdk": "static", "visibility": "public"},
        )
        client.post(
            "/api/spaces",
            json={"name": "private-space", "sdk": "static", "visibility": "private"},
        )
        client.post("/api/auth/logout")
        response = client.get("/api/spaces")
        assert response.status_code == 200
        names = [row["name"] for row in response.json()]
        assert "public-space" in names
        assert "private-space" not in names


class TestLoadOwnerGpuIds:
    """`_load_owner_gpu_ids` reads the `gpu:<username>` WebSetting row."""

    async def test_returns_stored_list_when_row_exists(
        self, app: tuple[TestClient, FastAPI, object], factory
    ) -> None:
        _, _, _ = app
        async with factory() as session:
            session.add(WebSetting(key="gpu:alice", value='["gpu-0", "gpu-1"]'))
            await session.commit()
            assert await _load_owner_gpu_ids(session, "alice") == ["gpu-0", "gpu-1"]

    async def test_returns_empty_list_when_row_missing(
        self, app: tuple[TestClient, FastAPI, object], factory
    ) -> None:
        _, _, _ = app
        async with factory() as session:
            assert await _load_owner_gpu_ids(session, "nobody") == []


_DELIVERY_NOTE = (
    "CDI hooks dropped for 0 (createContainer): a container create body cannot "
    "express hooks, and this host's podman did not apply the spec's own."
)


class _StubRuntimeManager:
    """Stands in for the Podman manager: no socket in the test environment."""

    def __init__(self, settings: object, **_: object) -> None:
        self.gpu_ids: list[str] = []

    async def inspect(self, owner: str, name: str) -> dict[str, object]:
        return {
            "Id": "stub-container-id",
            "State": {"Status": "running"},
            "NetworkSettings": {"Ports": [{"host_port": 23456}]},
        }

    async def build_image(self, owner: str, name: str) -> str:
        return "stub-image-id"

    async def start(
        self, owner: str, name: str, *, gpu_ids: list[str] | tuple[str, ...] = ()
    ) -> tuple[str, int, str]:
        self.gpu_ids = list(gpu_ids)
        return "stub-container-id", 23456, _DELIVERY_NOTE

    async def stop(self, owner: str, name: str) -> None:
        return None


class TestStartDeliveryNote:
    """The GPU delivery note reaches the Space's runtime panel.

    The fallback reports what it could not carry; the runtime message is
    where an operator looks right after a start, so a note that stops at the
    server log would be a silent gap.
    """

    async def test_the_note_is_appended_to_the_running_message(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user, factory, monkeypatch
    ) -> None:
        # Given: a container Space whose owner has an assigned GPU, and a
        # manager that reports a dropped-hook note.
        client, _, _ = app
        await seed_approved_user(username="erin")
        client.post(
            "/api/auth/login",
            json={"username": "erin", "password": "correct horse battery staple"},
        )
        client.post(
            "/api/spaces",
            json={"name": "trainer", "sdk": "gradio", "visibility": "public"},
        )
        async with factory() as session:
            session.add(WebSetting(key="gpu:erin", value='["gpu-0"]'))
            await session.commit()
        monkeypatch.setattr(
            "outo_models.server.routers.spaces.SpaceRuntimeManager", _StubRuntimeManager
        )

        # When
        response = client.post("/api/spaces/erin/trainer/start")

        # Then
        assert response.status_code == 200
        assert response.json()["message"] == f"The Space is running. {_DELIVERY_NOTE}"

    async def test_the_base_message_is_unchanged_without_a_note(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user, monkeypatch
    ) -> None:
        # Given: the same Space started without a note — podman's own CDI path
        # delivered everything the spec asked for.
        client, _, _ = app
        await seed_approved_user(username="erin")
        client.post(
            "/api/auth/login",
            json={"username": "erin", "password": "correct horse battery staple"},
        )
        client.post(
            "/api/spaces",
            json={"name": "trainer", "sdk": "gradio", "visibility": "public"},
        )

        class _Quiet(_StubRuntimeManager):
            async def start(
                self, owner: str, name: str, *, gpu_ids: list[str] | tuple[str, ...] = ()
            ) -> tuple[str, int, str]:
                return "stub-container-id", 23456, ""

        monkeypatch.setattr("outo_models.server.routers.spaces.SpaceRuntimeManager", _Quiet)

        # When
        response = client.post("/api/spaces/erin/trainer/start")

        # Then
        assert response.status_code == 200
        assert response.json()["message"] == "The Space is running."
