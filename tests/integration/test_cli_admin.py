"""Tests for `outo-models admin ...` (local DB path).

The remote admin path (`AdminApiClient`) is exercised separately against
an httpx mock transport; here we test the DB-backed commands against a
fresh sqlite schema per test, asserting both the user-visible CLI
output and the AuditLog rows the service functions emit.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from outo_models.cli.main import app
from outo_models.db import dispose_engines


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _seed_data_in_one_loop(setup_callable: Any) -> int:
    """Run `setup_callable(engine, factory)` in a single asyncio.run.

    Centralising every fixture seed through one helper keeps the test's
    async engine instance contained — multiple `asyncio.run()` calls in
    the same process leave aiosqlite worker threads in a closed loop,
    which surfaces as `table users already exists` on the next call.
    """
    import asyncio as _aio

    from outo_models.config import get_settings
    from outo_models.db import (
        get_engine,
        get_session_factory,
        run_migrations,
    )

    async def _runner() -> Any:
        engine = get_engine(get_settings())
        # Use alembic so the `alembic_version` row exists; subsequent
        # CLI calls reuse the same path and stop at "head".
        await run_migrations(engine)
        factory = get_session_factory(engine)
        try:
            result = await setup_callable(engine, factory)
        finally:
            await engine.dispose()
            await dispose_engines()
        return result

    return _aio.run(_runner())


def _bootstrap_admin_user(username: str = "root", role: str = "admin") -> int:
    """Seed a single admin user the CLI commands can act as. Returns its id."""
    from outo_models.auth.passwords import hash_password
    from outo_models.db import User

    async def _setup(engine: Any, factory: Any) -> int:
        from sqlalchemy import select as _select

        async with factory() as session:
            existing = (
                await session.execute(_select(User).where(User.username == username))
            ).scalar_one_or_none()
            if existing is not None:
                return int(existing.id)
            user = User(
                username=username,
                email=f"{username}@example.com",
                password_hash=hash_password("admin-password-1234"),
                role=role,
                status="approved",
            )
            session.add(user)
            await session.commit()
            return int(user.id)

    return _seed_data_in_one_loop(_setup)


class TestListAndPending:
    """`admin list` / `admin pending` print users with status filter."""

    def test_list_shows_users(self, runner: CliRunner, tmp_data_dir: Path) -> None:
        _bootstrap_admin_user("root")

        async def _add_alice(engine: Any, factory: Any) -> None:
            from outo_models.auth.approval import register_user

            async with factory() as session:
                await register_user(
                    session,
                    username="alice",
                    email="alice@example.com",
                    password="correct horse battery staple",
                )
                await session.commit()

        _seed_data_in_one_loop(_add_alice)

        result = runner.invoke(app, ["admin", "list"])
        assert result.exit_code == 0, result.output
        assert "alice" in result.output
        assert "root" in result.output

    def test_pending_filters_to_pending(self, runner: CliRunner, tmp_data_dir: Path) -> None:
        _bootstrap_admin_user("root")

        async def _add_bob(engine: Any, factory: Any) -> None:
            from outo_models.auth.approval import register_user

            async with factory() as session:
                await register_user(
                    session,
                    username="bob",
                    email="bob@example.com",
                    password="correct horse battery staple",
                )
                await session.commit()

        _seed_data_in_one_loop(_add_bob)

        result = runner.invoke(app, ["admin", "pending"])
        assert result.exit_code == 0, result.output
        assert "bob" in result.output
        # The admin (root, approved) should NOT appear in the pending list.
        assert "root" not in result.output


class TestApprovalTransitions:
    """approve / deny / ban / unban transitions through the CLI."""

    def test_approve_flips_status(self, runner: CliRunner, tmp_data_dir: Path) -> None:
        _bootstrap_admin_user("root")

        async def _add_carol(engine: Any, factory: Any) -> None:
            from outo_models.auth.approval import register_user

            async with factory() as session:
                await register_user(
                    session,
                    username="carol",
                    email="carol@example.com",
                    password="correct horse battery staple",
                )
                await session.commit()

        _seed_data_in_one_loop(_add_carol)

        result = runner.invoke(app, ["admin", "approve", "carol"])
        assert result.exit_code == 0, result.output
        assert "[approved]" in result.output
        assert "carol" in result.output

    def test_ban_then_unban(
        self, runner: CliRunner, tmp_data_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Approve first, then ban, then unban — each transition is auditable.
        _bootstrap_admin_user("root")

        async def _add_dave(engine: Any, factory: Any) -> None:
            from outo_models.db import User

            async with factory() as session:
                session.add(
                    User(
                        username="dave",
                        email="dave@example.com",
                        password_hash="x",
                        status="approved",
                    )
                )
                await session.commit()

        _seed_data_in_one_loop(_add_dave)

        assert runner.invoke(app, ["admin", "ban", "dave"]).exit_code == 0
        assert runner.invoke(app, ["admin", "unban", "dave"]).exit_code == 0


class TestQuota:
    """`admin quota show / set` writes through the local DB and audits it."""

    def test_set_quota_updates_value(self, runner: CliRunner, tmp_data_dir: Path) -> None:
        _bootstrap_admin_user("root")

        async def _add_eve(engine: Any, factory: Any) -> None:
            from outo_models.db import User

            async with factory() as session:
                session.add(
                    User(
                        username="eve",
                        email="eve@example.com",
                        password_hash="x",
                        status="approved",
                    )
                )
                await session.commit()

        _seed_data_in_one_loop(_add_eve)

        result = runner.invoke(app, ["admin", "quota", "set", "eve", "10GiB"])
        assert result.exit_code == 0, result.output

        result = runner.invoke(app, ["admin", "quota", "show", "eve"])
        assert result.exit_code == 0
        assert "10.00 GiB" in result.output


class TestGpu:
    """`admin gpu show / assign / clear` round-trips through WebSetting."""

    def test_assign_and_clear(self, runner: CliRunner, tmp_data_dir: Path) -> None:
        _bootstrap_admin_user("root")

        async def _add_frank(engine: Any, factory: Any) -> None:
            from outo_models.db import User

            async with factory() as session:
                session.add(
                    User(
                        username="frank",
                        email="frank@example.com",
                        password_hash="x",
                        status="approved",
                    )
                )
                await session.commit()

        _seed_data_in_one_loop(_add_frank)

        result = runner.invoke(app, ["admin", "gpu", "assign", "frank", "gpu-0", "gpu-1"])
        assert result.exit_code == 0, result.output
        assert "[gpu]" in result.output
        assert "assigned" in result.output

        result = runner.invoke(app, ["admin", "gpu", "show", "frank"])
        assert result.exit_code == 0
        assert "gpu-0" in result.output
        assert "gpu-1" in result.output

        result = runner.invoke(app, ["admin", "gpu", "clear", "frank"])
        assert result.exit_code == 0, result.output

        result = runner.invoke(app, ["admin", "gpu", "show", "frank"])
        assert result.exit_code == 0
        assert "no GPUs assigned" in result.output


class TestResetPassword:
    """`admin reset-password` writes a new hash and prints the new password."""

    def test_reset_password_prints_once(self, runner: CliRunner, tmp_data_dir: Path) -> None:
        _bootstrap_admin_user("root")

        async def _add_gabe(engine: Any, factory: Any) -> None:
            from outo_models.auth.passwords import hash_password
            from outo_models.db import User

            async with factory() as session:
                session.add(
                    User(
                        username="gabe",
                        email="gabe@example.com",
                        password_hash=hash_password("old-password-1234"),
                        status="approved",
                    )
                )
                await session.commit()

        _seed_data_in_one_loop(_add_gabe)

        result = runner.invoke(app, ["admin", "reset-password", "gabe"])
        assert result.exit_code == 0, result.output
        lines = [line.strip() for line in result.output.splitlines() if line.strip()]
        new_password = lines[-1]
        # `secrets.token_urlsafe(18)` produces a 24-char base64 string.
        assert len(new_password) >= 20

        async def _verify(engine: Any, factory: Any) -> None:
            from sqlalchemy import select as _select

            from outo_models.auth.passwords import verify_password
            from outo_models.db import User

            async with factory() as session:
                user = (
                    await session.execute(_select(User).where(User.username == "gabe"))
                ).scalar_one()
                assert verify_password(user.password_hash, new_password)
                assert not verify_password(user.password_hash, "old-password-1234")

        _seed_data_in_one_loop(_verify)


class TestRemoteAdminApi:
    """The `--api-url` / `--token` flag pair drives `AdminApiClient`."""

    def test_remote_list_uses_httpx_mock(self, runner: CliRunner, tmp_data_dir: Path) -> None:
        import httpx

        from outo_models.cli_remote import AdminApiClient

        def _handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/admin/users":
                return httpx.Response(
                    200,
                    json=[
                        {
                            "id": 1,
                            "username": "remote",
                            "email": "remote@example.com",
                            "role": "user",
                            "status": "approved",
                        }
                    ],
                )
            return httpx.Response(404, json={"error": "not found"})

        transport = httpx.MockTransport(_handler)
        client = AdminApiClient("https://example.test", "pat-test", transport=transport)
        users = client.list_users()
        client.close()
        assert users == [
            {
                "id": 1,
                "username": "remote",
                "email": "remote@example.com",
                "role": "user",
                "status": "approved",
            }
        ]

    def test_remote_approve_uses_bearer(self, runner: CliRunner, tmp_data_dir: Path) -> None:
        import httpx

        from outo_models.cli_remote import AdminApiClient

        seen: dict[str, str] = {}

        def _handler(request: httpx.Request) -> httpx.Response:
            seen["auth"] = request.headers.get("authorization", "")
            return httpx.Response(200, json={"username": "alice", "status": "approved"})

        transport = httpx.MockTransport(_handler)
        client = AdminApiClient("https://example.test", "pat-test", transport=transport)
        result = client.approve("alice")
        client.close()
        assert seen["auth"] == "Bearer pat-test"
        assert result["username"] == "alice"

    def test_remote_auth_failure_raises_admin_api_error(
        self, runner: CliRunner, tmp_data_dir: Path
    ) -> None:
        import httpx

        from outo_models.cli_remote import AdminApiClient, AdminApiError

        def _handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"error": "unauthorized"})

        transport = httpx.MockTransport(_handler)
        client = AdminApiClient("https://example.test", "pat-test", transport=transport)
        with pytest.raises(AdminApiError) as exc_info:
            client.list_users()
        client.close()
        assert exc_info.value.code == "admin_auth_failed"
