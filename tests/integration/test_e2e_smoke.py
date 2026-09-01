"""End-to-end smoke suite for outo-models.

Ties the whole stack together: signup auto-approve → login → repo
creation → PAT mint → real `git` CLI clone / commit / push → fresh
clone → byte-exact content match, plus three guard tests for the
operational surface (security headers, OpenAPI schema, dry-run reset).

The full-stack test spins the actual `FastAPI` app under `uvicorn` on
an ephemeral TCP port (mirroring `test_git_smart_http.py`'s fixture
pattern) and drives it through `httpx` + the real `git(1)` binary.
Nothing here goes through `TestClient` — the point is to exercise the
running app exactly the way a developer / CI would.

Run budget: < 90 s for the entire module. Every test binds its own
ephemeral port, owns its own tmp dir, and tears the uvicorn thread
down on exit so the suite stays parallel-safe on multi-agent
workstations.
"""
from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner
from uvicorn import Config, Server

from outo_models.cli.main import app as cli_app
from outo_models.config import get_settings
from outo_models.server import create_app

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def open_signup_env(
    tmp_data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Path]:
    """Drop `OUTO_REQUIRE_APPROVAL=false` so signup auto-approves in tests.

    The autouse `_isolate_outo_env` (top-level conftest) strips every
    `OUTO_*` env var before each test, so we set ours here and clear
    the cached `Settings()` so `create_app` sees the relaxed policy.
    """
    monkeypatch.setenv("OUTO_REQUIRE_APPROVAL", "false")
    monkeypatch.setenv("OUTO_SECRET_KEY", "test-secret-key-for-e2e-smoke-1234567890")
    get_settings.cache_clear()
    try:
        yield tmp_data_dir
    finally:
        get_settings.cache_clear()


@pytest.fixture
def fresh_limiter() -> Iterator[None]:
    """Reset slowapi's rate-limit buckets before each test.

    The login endpoint is 5/min and signup is 3/min — left over buckets
    from a previous run would 429 us. Mirrors what the per-test
    integration `app` fixture does.
    """
    from outo_models.auth.rate_limit import limiter

    limiter.reset()
    try:
        yield
    finally:
        limiter.reset()


@pytest.fixture
def live_server(
    open_signup_env: Path, fresh_limiter: None
) -> Iterator[str]:
    """Boot the full `create_app` under uvicorn on an ephemeral port.

    Yields the base URL (`http://127.0.0.1:<port>`); shuts the server
    down cleanly on exit so the suite stays parallel-safe.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(2048)
    port = sock.getsockname()[1]

    settings = get_settings()
    fastapi_app = create_app(settings)

    config = Config(fastapi_app, log_level="warning", access_log=False)
    server = Server(config=config)

    async def _serve() -> None:
        await server.serve(sockets=[sock])

    thread = threading.Thread(target=asyncio.run, args=(_serve(),), daemon=True)
    thread.start()

    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if server.started:
            break
        time.sleep(0.02)
    else:  # pragma: no cover - defensive
        raise RuntimeError("uvicorn did not start within 10 s")

    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        if thread.is_alive():  # pragma: no cover - defensive
            raise RuntimeError("uvicorn thread did not exit within 10 s")
        sock.close()


@pytest.fixture
def git_env(tmp_path: Path) -> dict[str, str]:
    """Per-test git env that disables prompts and pins a fake identity.

    Modelled on the same fixture in `test_git_smart_http.py` so the
    real `git(1)` binary on the host behaves the same way.
    """
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_AUTHOR_NAME"] = "Smoke"
    env["GIT_AUTHOR_EMAIL"] = "smoke@example.com"
    env["GIT_COMMITTER_NAME"] = "Smoke"
    env["GIT_COMMITTER_EMAIL"] = "smoke@example.com"
    env.pop("GIT_CONFIG_GLOBAL", None)
    env.pop("GIT_CONFIG_NOSYSTEM", None)
    env["HOME"] = str(tmp_path / "home")
    (tmp_path / "home").mkdir()
    return env


async def _run_git(
    args: list[str], *, cwd: Path, env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    """Run `git <args>` and raise with full output if it fails."""
    proc = await asyncio.to_thread(
        subprocess.run,
        ["git", *args],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise AssertionError(
            f"git {args!r} failed in {cwd}\n"
            f"--- stdout ---\n{proc.stdout}\n"
            f"--- stderr ---\n{proc.stderr}"
        )
    return proc


def _repo_url(base: str, owner: str, name: str) -> str:
    return f"{base}/{owner}/{name}.git"


def _with_basic(url: str, username: str, password: str) -> str:
    """Inject `username:password` into the URL's userinfo slot."""
    scheme, rest = url.split("://", 1)
    return f"{scheme}://{username}:{password}@{rest}"


# ---------------------------------------------------------------------------
# Full stack
# ---------------------------------------------------------------------------


class TestFullStackSignupToPush:
    """Signup → repo → PAT → push → clone → byte-exact match."""

    async def test_full_stack_signup_to_push(
        self,
        live_server: str,
        tmp_data_dir: Path,
        git_env: dict[str, str],
    ) -> None:
        username = "smokeuser"
        password = "correct horse battery staple"
        repo_name = "smoke-model"
        payload = b"hello from the smoke suite\n"

        async with httpx.AsyncClient(base_url=live_server, timeout=15.0) as client:
            # 1. Signup (auto-approved because OUTO_REQUIRE_APPROVAL=false).
            signup_resp = await client.post(
                "/api/auth/signup",
                json={
                    "username": username,
                    "email": f"{username}@example.com",
                    "password": password,
                },
            )
            assert signup_resp.status_code == 201, signup_resp.text
            assert signup_resp.json()["status"] == "approved"

            # 2. Login (rotates the session cookie onto the client).
            login_resp = await client.post(
                "/api/auth/login",
                json={"username": username, "password": password},
            )
            assert login_resp.status_code == 200, login_resp.text

            # 3. Create a public model repo via the JSON API.
            create_resp = await client.post(
                "/api/repos",
                json={
                    "name": repo_name,
                    "kind": "model",
                    "visibility": "public",
                },
            )
            assert create_resp.status_code == 201, create_resp.text
            created = create_resp.json()
            assert created["name"] == repo_name
            assert created["owner"] == username
            assert created["visibility"] == "public"

            # 4. Mint a PAT the real git client will use for push auth.
            token_resp = await client.post(
                "/api/auth/tokens",
                json={"name": "smoke", "scopes": ["read", "write"]},
            )
            assert token_resp.status_code == 201, token_resp.text
            pat = token_resp.json()["token"]
            assert pat.startswith("v4.")

        # 5. Real `git(1)` round-trip: push a commit as the owner.
        push_workdir = tmp_data_dir / "push-source"
        push_workdir.mkdir()
        await _run_git(["init"], cwd=push_workdir, env=git_env)
        (push_workdir / "README.md").write_bytes(payload)
        await _run_git(["add", "README.md"], cwd=push_workdir, env=git_env)
        await _run_git(
            ["commit", "-m", "smoke push"], cwd=push_workdir, env=git_env
        )

        push_url = _with_basic(
            _repo_url(live_server, username, repo_name), username, pat
        )
        await _run_git(
            ["push", push_url, "HEAD:refs/heads/master"],
            cwd=push_workdir,
            env=git_env,
        )

        # 6. Fresh anonymous clone pulls the commit; content matches byte-for-byte.
        clone_workdir = tmp_data_dir / "clone-fresh"
        clone_workdir.mkdir()
        await _run_git(
            ["clone", _repo_url(live_server, username, repo_name)],
            cwd=clone_workdir,
            env=git_env,
        )
        cloned_path = clone_workdir / repo_name / "README.md"
        assert cloned_path.is_file()
        assert cloned_path.read_bytes() == payload


# ---------------------------------------------------------------------------
# Security headers
# ---------------------------------------------------------------------------


class TestSecurityHeadersOnSmokePath:
    """Every API response carries the security header bundle."""

    async def test_security_headers_on_smoke_path(self, live_server: str) -> None:
        async with httpx.AsyncClient(base_url=live_server, timeout=15.0) as client:
            # GET an API endpoint — guarantees a real router response.
            response = await client.get("/api/repos")

        assert response.status_code == 200
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["x-frame-options"] == "DENY"
        assert response.headers["referrer-policy"] == "strict-origin-when-cross-origin"
        assert response.headers["permissions-policy"].startswith("camera=()")
        assert response.headers["content-security-policy"].startswith(
            "default-src 'self'"
        )
        # Loopback domain → no HSTS.
        assert "strict-transport-security" not in response.headers


# ---------------------------------------------------------------------------
# Reset dry-run default
# ---------------------------------------------------------------------------


class TestResetDryRunDefault:
    """`reset` with no flags is a no-op dry-run; never touches the data dir."""

    def test_reset_dry_run_is_default(
        self,
        tmp_data_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        fresh_limiter: None,
    ) -> None:
        # Plant a marker inside the data dir; the dry-run must NOT delete it.
        marker = tmp_data_dir / "do-not-touch"
        marker.write_bytes(b"keep me")

        # The CLI reads its config from env, so pin it to the same tmp path
        # the rest of the test suite uses (mirrors `test_cli_reset.py`).
        monkeypatch.setenv("OUTO_DATA_DIR", str(tmp_data_dir))
        monkeypatch.setenv("OUTO_CONFIG", str(tmp_data_dir / "cfg.yaml"))
        # Strip `OUTO_REQUIRE_APPROVAL` in case a sibling fixture leaked it;
        # `_isolate_outo_env` already cleared it, but be defensive.
        monkeypatch.delenv("OUTO_REQUIRE_APPROVAL", raising=False)
        get_settings.cache_clear()

        runner = CliRunner()
        result = runner.invoke(cli_app, ["reset"])

        assert result.exit_code == 0, result.output
        # Dry-run summary lines that AGENTS.md §2.2 mandates.
        assert "would be deleted" in result.output
        assert "volume: outo-models-data" in result.output
        # Marker file survived — dry-run must not delete anything.
        assert marker.is_file()
        assert marker.read_bytes() == b"keep me"


# ---------------------------------------------------------------------------
# OpenAPI schema
# ---------------------------------------------------------------------------


class TestOpenAPISchema:
    """`/openapi.json` is served and lists the headline routes."""

    async def test_openapi_schema_valid(self, live_server: str) -> None:
        async with httpx.AsyncClient(base_url=live_server, timeout=15.0) as client:
            response = await client.get("/openapi.json")

        assert response.status_code == 200
        body = response.json()
        paths = set(body.get("paths", {}).keys())
        assert "/api/auth/signup" in paths
        assert "/api/repos" in paths


__all__ = [
    "TestFullStackSignupToPush",
    "TestOpenAPISchema",
    "TestResetDryRunDefault",
    "TestSecurityHeadersOnSmokePath",
]
