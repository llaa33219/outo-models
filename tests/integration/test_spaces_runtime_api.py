"""Integration tests for the Spaces runtime API + reverse-proxy.

These tests run the full FastAPI stack (`create_app`) so every layer
(DB, auth, settings, slowapi, exception handlers, routers) is
exercised. Two surfaces are mocked so the test does not need a real
podman daemon:

    1. The `SpaceRuntimeManager` is patched to use httpx.MockTransport;
       the mock answers every podman REST call the router makes.
    2. The reverse-proxy's downstream call (to `127.0.0.1:<port>`) is
       captured by `respx.mock`, so the upstream "container" is the test
       process itself.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from outo_models.auth.passwords import hash_password
from outo_models.config import get_settings
from outo_models.db import AuditLog, User, UserQuota, UserUsage
from outo_models.exceptions import OutoError

# ---------------------------------------------------------------------------
# Helpers: seed a user + a space row directly so we don't depend on the
# signup API for cases that are not specifically about the signup flow.
# ---------------------------------------------------------------------------


async def _seed_user(
    factory: async_sessionmaker[AsyncSession],
    *,
    username: str,
    role: str = "user",
) -> int:
    async with factory() as session:
        user = User(
            username=username,
            email=f"{username}@example.com",
            password_hash=hash_password("correct horse battery staple"),
            role=role,
            status="approved",
        )
        session.add(user)
        await session.commit()
        user_id = user.id
        session.add(UserQuota(user_id=user_id, max_bytes=10 * 1024**3))
        session.add(UserUsage(user_id=user_id, used_bytes=0))
        await session.commit()
        return user_id


async def _seed_space(
    factory: async_sessionmaker[AsyncSession],
    *,
    owner_username: str,
    name: str = "demo",
    sdk: str = "static",
    visibility: str = "public",
) -> int:
    """Insert a Space row + SDK sidecar; return `repo.id`."""
    from outo_models.repos.models import Visibility
    from outo_models.spaces import create_space

    vis_enum = Visibility(visibility)
    async with factory() as session:
        user = (
            await session.execute(select(User).where(User.username == owner_username))
        ).scalar_one()
        repo = await create_space(
            session,
            owner=user,
            name=name,
            sdk=sdk,
            visibility=vis_enum,
            description=None,
        )
        await session.commit()
        return int(repo.id)


# ---------------------------------------------------------------------------
# Fake manager + helpers
# ---------------------------------------------------------------------------


class _FakeSpaceRuntimeManager:
    """A drop-in for `SpaceRuntimeManager` whose every method is a knob.

    `inspect` returns whatever payload the active scenario needs.
    `port_for` is the deterministic host port the proxy wants to reach.
    """

    def __init__(
        self,
        *,
        settings,
        inspect_payload: dict[str, Any] | None = None,
        port: int | None = None,
        raise_: BaseException | None = None,
    ) -> None:
        self._settings = settings
        self._inspect_payload = inspect_payload
        self._port = port
        self._raise = raise_
        self.start_calls: list[tuple[str, str, tuple[str, ...]]] = []
        self.stop_calls: list[tuple[str, str]] = []
        self.restart_calls: list[tuple[str, str]] = []
        self.build_calls: list[tuple[str, str]] = []
        self.list_calls = 0

    async def list_managed(self) -> list:
        self.list_calls += 1
        return []

    async def build_image(self, owner: str, name: str) -> str:
        self.build_calls.append((owner, name))
        return f"sha256:{owner}-{name}"

    async def start(
        self,
        owner: str,
        name: str,
        *,
        gpu_ids: list[str] | tuple[str, ...] = (),
    ) -> tuple[str, int]:
        if self._raise is not None:
            raise self._raise
        self.start_calls.append((owner, name, tuple(gpu_ids)))
        return ("container-id-xyz", self._port or 20000)

    async def stop(self, owner: str, name: str) -> None:
        if self._raise is not None:
            raise self._raise
        self.stop_calls.append((owner, name))

    async def restart(self, owner: str, name: str) -> None:
        if self._raise is not None:
            raise self._raise
        self.restart_calls.append((owner, name))

    async def remove(self, owner: str, name: str) -> None:
        del owner, name

    async def inspect(
        self, owner: str, name: str
    ) -> dict[str, Any] | None:
        if self._raise is not None:
            raise self._raise
        return self._inspect_payload


@pytest.fixture
def fake_managers() -> list[_FakeSpaceRuntimeManager]:
    """List shared by the patcher so tests can read what the fake recorded."""
    return []


def _set_runtime_enabled(fastapi_app, settings, *, enabled: bool) -> None:
    """Push the runtime-enabled flag onto both the settings + the app."""
    object.__setattr__(settings, "spaces_runtime_enabled", enabled)
    fastapi_app.state.settings = settings


def _enable_runtime(app_fixture) -> None:
    """Enable runtime for this test (mutates the live Settings + app state)."""
    get_settings.cache_clear()
    settings = get_settings()
    _client, fastapi_app, _ = app_fixture
    _set_runtime_enabled(fastapi_app, settings, enabled=True)


def _disable_runtime(app_fixture) -> None:
    """Disable runtime for this test."""
    get_settings.cache_clear()
    settings = get_settings()
    _client, fastapi_app, _ = app_fixture
    _set_runtime_enabled(fastapi_app, settings, enabled=False)


def _patch_router_manager(
    monkeypatch,
    fake_managers: list[_FakeSpaceRuntimeManager],
    inspect_payload: dict[str, Any] | None,
    port: int | None,
) -> _FakeSpaceRuntimeManager:
    """Replace `SpaceRuntimeManager` in the router with a captured fake."""
    manager = _FakeSpaceRuntimeManager(
        settings=get_settings(),
        inspect_payload=inspect_payload,
        port=port,
    )
    fake_managers.append(manager)

    def factory(settings, *, client=None):
        for m in fake_managers:
            if m._settings is settings:
                return m
        return manager

    monkeypatch.setattr(
        "outo_models.server.routers.spaces.SpaceRuntimeManager", factory
    )
    return manager


# ---------------------------------------------------------------------------
# Static-SDK tests
# ---------------------------------------------------------------------------


class TestStaticSpace:
    async def test_start_static_sdk_exports_and_returns_running(
        self,
        app: tuple[TestClient, FastAPI, object],
        seed_approved_user,
        factory: async_sessionmaker[AsyncSession],
        fake_managers,
        tmp_data_dir,
        monkeypatch,
    ) -> None:
        client, _, _ = app
        _enable_runtime(app)
        await seed_approved_user(username="alice")
        client.post(
            "/api/auth/login",
            json={"username": "alice", "password": "correct horse battery staple"},
        )
        space_id = await _seed_space(factory, owner_username="alice", sdk="static")
        manager = _patch_router_manager(
            monkeypatch, fake_managers, inspect_payload=None, port=None
        )
        response = client.post("/api/spaces/alice/demo/start")
        assert response.status_code == 200, response.json()
        body = response.json()
        assert body["state"] == "running"
        assert body["url"].endswith("/spaces/alice/demo/run/")
        # The static export must have materialised files under the site dir.
        site = tmp_data_dir / "spaces" / "alice" / "demo" / "site"
        # No tracked files exist yet — but the dir itself was created.
        assert site.exists()
        # No podman calls were made — `manager.start` should not have been invoked.
        assert manager.start_calls == []
        # An audit row was written.
        async with factory() as session:
            rows = (
                await session.execute(
                    select(AuditLog).where(AuditLog.action == "space.start")
                )
            ).scalars().all()
        assert len(rows) >= 1
        latest = rows[-1]
        assert latest.target_id == str(space_id)


# ---------------------------------------------------------------------------
# Container-SDK tests
# ---------------------------------------------------------------------------


def _running_inspect_payload(host_port: int = 20000) -> dict[str, Any]:
    """Podman inspect response for a running Space container."""
    return {
        "Id": "container-id-xyz",
        "State": {"Status": "running"},
        "NetworkSettings": {
            "Ports": {"8000/tcp": [{"HostPort": str(host_port)}]},
        },
        "Config": {"Labels": {"outo.space": "alice/demo"}},
    }


class TestStartStopRestartApi:
    async def test_anonymous_status_on_public_space_returns_disabled_state(
        self,
        app: tuple[TestClient, FastAPI, object],
        seed_approved_user,
        factory: async_sessionmaker[AsyncSession],
        fake_managers,
        monkeypatch,
    ) -> None:
        client, _, _ = app
        _enable_runtime(app)
        await seed_approved_user(username="alice")
        client.post(
            "/api/auth/login",
            json={"username": "alice", "password": "correct horse battery staple"},
        )
        await _seed_space(
            factory, owner_username="alice", name="demo", sdk="static", visibility="public"
        )
        _patch_router_manager(
            monkeypatch, fake_managers, inspect_payload=None, port=None
        )
        client.post("/api/auth/logout")
        response = client.get("/api/spaces/alice/demo/status")
        assert response.status_code == 200
        assert response.json()["state"] == "stopped"

    async def test_anonymous_status_on_private_space_is_404(
        self,
        app: tuple[TestClient, FastAPI, object],
        seed_approved_user,
        factory: async_sessionmaker[AsyncSession],
        monkeypatch,
    ) -> None:
        client, _, _ = app
        _enable_runtime(app)
        await seed_approved_user(username="alice")
        client.post(
            "/api/auth/login",
            json={"username": "alice", "password": "correct horse battery staple"},
        )
        await _seed_space(
            factory, owner_username="alice", name="secret", sdk="static", visibility="private"
        )
        client.post("/api/auth/logout")
        response = client.get("/api/spaces/alice/secret/status")
        assert response.status_code == 404
        assert response.json()["error"] == "not_found"

    async def test_start_by_non_owner_is_403(
        self,
        app: tuple[TestClient, FastAPI, object],
        seed_approved_user,
        factory: async_sessionmaker[AsyncSession],
        fake_managers,
        monkeypatch,
    ) -> None:
        client, _, _ = app
        _enable_runtime(app)
        await seed_approved_user(username="alice")
        await seed_approved_user(username="bob")
        client.post(
            "/api/auth/login",
            json={"username": "bob", "password": "correct horse battery staple"},
        )
        # alice's space; bob does NOT own it
        # Seed without going through create_space because we need the sidecar too
        await _seed_space(factory, owner_username="alice", name="demo")
        manager = _patch_router_manager(
            monkeypatch, fake_managers, inspect_payload=None, port=None
        )
        response = client.post("/api/spaces/alice/demo/start")
        assert response.status_code == 403
        assert manager.start_calls == []

    async def test_start_when_runtime_disabled_returns_503(
        self,
        app: tuple[TestClient, FastAPI, object],
        seed_approved_user,
        factory: async_sessionmaker[AsyncSession],
        fake_managers,
        monkeypatch,
    ) -> None:
        client, _, _ = app
        _disable_runtime(app)
        await seed_approved_user(username="alice")
        client.post(
            "/api/auth/login",
            json={"username": "alice", "password": "correct horse battery staple"},
        )
        await _seed_space(factory, owner_username="alice", sdk="static")
        manager = _patch_router_manager(
            monkeypatch, fake_managers, inspect_payload=None, port=None
        )
        response = client.post("/api/spaces/alice/demo/start")
        assert response.status_code == 503
        assert response.json()["error"] == "runtime_disabled"
        assert manager.start_calls == []

    async def test_container_start_by_owner_calls_podman(
        self,
        app: tuple[TestClient, FastAPI, object],
        seed_approved_user,
        factory: async_sessionmaker[AsyncSession],
        fake_managers,
        monkeypatch,
    ) -> None:
        client, _, _ = app
        _enable_runtime(app)
        await seed_approved_user(username="alice")
        client.post(
            "/api/auth/login",
            json={"username": "alice", "password": "correct horse battery staple"},
        )
        # Seed a gradio space; need a Containerfile in the repo too. Use a
        # gradio SDK so `runtime.py` doesn't require a Dockerfile.
        await _seed_space(factory, owner_username="alice", sdk="gradio")
        manager = _patch_router_manager(
            monkeypatch,
            fake_managers,
            inspect_payload=_running_inspect_payload(20000),
            port=20000,
        )
        response = client.post("/api/spaces/alice/demo/start")
        assert response.status_code == 200
        body = response.json()
        assert body["state"] == "running"
        assert body["port"] == 20000
        assert manager.start_calls == [("alice", "demo", ())]
        # build was called too.
        assert manager.build_calls == [("alice", "demo")]


class TestStaticProxyPathTraversal:
    """A malicious path MUST NOT escape the static site dir."""

    async def test_dotdot_escape_is_404(
        self,
        app: tuple[TestClient, FastAPI, object],
        seed_approved_user,
        factory: async_sessionmaker[AsyncSession],
        tmp_data_dir,
        monkeypatch,
    ) -> None:
        client, _, _ = app
        _enable_runtime(app)
        await seed_approved_user(username="alice")
        client.post(
            "/api/auth/login",
            json={"username": "alice", "password": "correct horse battery staple"},
        )
        # Seed the static site dir with a known file at the root.
        site = tmp_data_dir / "spaces" / "alice" / "demo" / "site"
        site.mkdir(parents=True, exist_ok=True)
        (site / "index.html").write_text("<h1>ok</h1>\n")
        # Also seed a top-level secret we'd never want exposed.
        secret_path = tmp_data_dir / "spaces" / "alice" / "secret.txt"
        secret_path.write_text("THIS IS SECRET")

        await _seed_space(factory, owner_username="alice", sdk="static")
        # Request a path that escapes via ../..
        response = client.get(
            "/spaces/alice/demo/run/../../secret.txt"
        )
        # Path traversal is rejected — the test should NOT have received
        # the secret contents.
        assert response.status_code in (400, 404)
        assert b"THIS IS SECRET" not in response.content

    async def test_static_serves_index_for_slash(
        self,
        app: tuple[TestClient, FastAPI, object],
        seed_approved_user,
        factory: async_sessionmaker[AsyncSession],
        tmp_data_dir,
        monkeypatch,
    ) -> None:
        client, _, _ = app
        _enable_runtime(app)
        await seed_approved_user(username="alice")
        client.post(
            "/api/auth/login",
            json={"username": "alice", "password": "correct horse battery staple"},
        )
        site = tmp_data_dir / "spaces" / "alice" / "demo" / "site"
        site.mkdir(parents=True, exist_ok=True)
        (site / "index.html").write_text("<h1>ok</h1>\n")
        await _seed_space(factory, owner_username="alice", sdk="static")
        response = client.get("/spaces/alice/demo/run/")
        assert response.status_code == 200
        assert b"ok" in response.content


class TestContainerProxy:
    """When the container is RUNNING, the proxy forwards method + body."""

    async def test_proxy_forwards_method_and_body(
        self,
        app: tuple[TestClient, FastAPI, object],
        seed_approved_user,
        factory: async_sessionmaker[AsyncSession],
        fake_managers,
        monkeypatch,
    ) -> None:
        client, _, _ = app
        _enable_runtime(app)
        await seed_approved_user(username="alice")
        client.post(
            "/api/auth/login",
            json={"username": "alice", "password": "correct horse battery staple"},
        )
        await _seed_space(factory, owner_username="alice", sdk="gradio")
        _patch_router_manager(
            monkeypatch,
            fake_managers,
            inspect_payload=_running_inspect_payload(20000),
            port=20000,
        )

        captured: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["method"] = request.method
            captured["url"] = str(request.url)
            captured["body"] = request.read()
            return httpx.Response(
                200,
                content=b"proxy-ok",
                headers={"content-type": "text/plain"},
            )

        transport = httpx.MockTransport(handler)
        real_async_client = httpx.AsyncClient

        class _ProxyAsyncClient:
            """`httpx.AsyncClient` substitute that always routes through the test handler."""
            def __init__(self, *args, **kwargs) -> None:
                timeout = kwargs.get("timeout", httpx.Timeout(30.0, connect=5.0))
                self._client = real_async_client(
                    transport=transport,
                    timeout=timeout,
                )

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                await self._client.aclose()
                return False

            async def request(self, method, url, **kwargs):
                return await self._client.request(method, url, **kwargs)

            async def aclose(self) -> None:
                await self._client.aclose()

        monkeypatch.setattr(
            "outo_models.server.routers.spaces.httpx.AsyncClient",
            _ProxyAsyncClient,
        )

        response = client.post(
            "/spaces/alice/demo/run/api/v1/run",
            content=b'{"input":"hi"}',
            headers={"content-type": "application/json"},
        )

        assert response.status_code == 200
        assert response.content == b"proxy-ok"
        assert captured["method"] == "POST"
        assert str(captured["url"]).startswith("http://127.0.0.1:20000/api/v1/run")
        assert captured["body"] == b'{"input":"hi"}'

    async def test_proxy_returns_503_when_not_running(
        self,
        app: tuple[TestClient, FastAPI, object],
        seed_approved_user,
        factory: async_sessionmaker[AsyncSession],
        fake_managers,
        monkeypatch,
    ) -> None:
        client, _, _ = app
        _enable_runtime(app)
        await seed_approved_user(username="alice")
        client.post(
            "/api/auth/login",
            json={"username": "alice", "password": "correct horse battery staple"},
        )
        await _seed_space(factory, owner_username="alice", sdk="gradio")
        _patch_router_manager(
            monkeypatch, fake_managers, inspect_payload=None, port=None
        )
        response = client.get("/spaces/alice/demo/run/index")
        assert response.status_code == 503
        assert response.json()["error"] == "space_not_running"

    async def test_proxy_requires_runtime_enabled(
        self,
        app: tuple[TestClient, FastAPI, object],
        seed_approved_user,
        factory: async_sessionmaker[AsyncSession],
        fake_managers,
        monkeypatch,
    ) -> None:
        client, _, _ = app
        _disable_runtime(app)
        await seed_approved_user(username="alice")
        client.post(
            "/api/auth/login",
            json={"username": "alice", "password": "correct horse battery staple"},
        )
        await _seed_space(factory, owner_username="alice", sdk="gradio")
        response = client.get("/spaces/alice/demo/run/index")
        assert response.status_code == 503
        assert response.json()["error"] == "runtime_disabled"


class TestAuditLogRows:
    """Lifecycle endpoints MUST leave a trail in the audit log."""

    async def test_start_writes_audit_log(
        self,
        app: tuple[TestClient, FastAPI, object],
        seed_approved_user,
        factory: async_sessionmaker[AsyncSession],
        fake_managers,
        monkeypatch,
    ) -> None:
        client, _, _ = app
        _enable_runtime(app)
        await seed_approved_user(username="alice")
        client.post(
            "/api/auth/login",
            json={"username": "alice", "password": "correct horse battery staple"},
        )
        await _seed_space(factory, owner_username="alice", sdk="static")
        _patch_router_manager(
            monkeypatch, fake_managers, inspect_payload=None, port=None
        )
        response = client.post("/api/spaces/alice/demo/start")
        assert response.status_code == 200
        async with factory() as session:
            rows = (
                await session.execute(
                    select(AuditLog)
                    .where(AuditLog.action == "space.start")
                    .order_by(AuditLog.id)
                )
            ).scalars().all()
        assert len(rows) >= 1
        assert rows[-1].detail is not None
        decoded = json.loads(rows[-1].detail)
        assert decoded["ok"] is True
        assert decoded["state"] == "running"

    async def test_podman_error_audit_row(
        self,
        app: tuple[TestClient, FastAPI, object],
        seed_approved_user,
        factory: async_sessionmaker[AsyncSession],
        fake_managers,
        monkeypatch,
    ) -> None:
        client, _, _ = app
        _enable_runtime(app)
        await seed_approved_user(username="alice")
        client.post(
            "/api/auth/login",
            json={"username": "alice", "password": "correct horse battery staple"},
        )
        await _seed_space(factory, owner_username="alice", sdk="gradio")
        # Make the manager raise on `start` → a 502 / space_build_failed
        # propagates; the audit row records `ok: false`.
        manager = _FakeSpaceRuntimeManager(
            settings=get_settings(),
            inspect_payload=None,
            raise_=OutoError(
                "Image build failed",
                code="space_build_failed",
                status_code=502,
            ),
        )
        fake_managers.append(manager)
        monkeypatch.setattr(
            "outo_models.server.routers.spaces.SpaceRuntimeManager",
            lambda settings, *, client=None: manager,
        )
        response = client.post("/api/spaces/alice/demo/start")
        # The build/start path raised before flushing its OK row, so the
        # router emits a `ok: false` row. Status is 502 from the OutoError.
        assert response.status_code == 502
        async with factory() as session:
            rows = (
                await session.execute(
                    select(AuditLog)
                    .where(AuditLog.action == "space.start")
                    .order_by(AuditLog.id)
                )
            ).scalars().all()
        assert len(rows) >= 1
        decoded = json.loads(rows[-1].detail)
        assert decoded["ok"] is False
        assert decoded["error_code"] == "space_build_failed"
