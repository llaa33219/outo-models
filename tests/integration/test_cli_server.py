"""Tests for `outo-models server migrate` and the `serve` smoke test.

`migrate` is the only in-container command the operator invokes manually
(via `outo-models server migrate`) — `serve` runs implicitly from the
container entrypoint. We exercise both:

    * `migrate` against a tmp sqlite DB: schema is created, idempotent
      on re-run.
    * `serve` — we monkeypatch `uvicorn.Server.run` so we capture the
      server boot without blocking.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from outo_models.cli.main import app


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


class TestMigrate:
    """`outo-models server migrate` runs alembic against the configured DB."""

    def test_migrate_creates_schema(
        self, runner: CliRunner, tmp_data_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        result = runner.invoke(app, ["server", "migrate"])
        assert result.exit_code == 0, result.output

        # Schema exists. Open a session and assert the alembic_version row.
        import asyncio as _aio

        from sqlalchemy import text

        from outo_models.db import get_engine

        async def _check() -> None:
            engine = get_engine()
            async with engine.connect() as conn:
                row = (await conn.execute(text("SELECT version_num FROM alembic_version"))).first()
                assert row is not None
            await engine.dispose()

        _aio.run(_check())

    def test_migrate_idempotent(
        self, runner: CliRunner, tmp_data_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Re-running migrate must succeed (alembic upgrade head is a no-op
        # on a schema that's already at head).
        for _ in range(2):
            result = runner.invoke(app, ["server", "migrate"])
            assert result.exit_code == 0


class TestServe:
    """`outo-models server serve` builds the uvicorn.Config (smoke test)."""

    def test_serve_invokes_uvicorn(
        self, runner: CliRunner, tmp_data_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # We don't actually want uvicorn to bind a port in tests; capture
        # the call and short-circuit it.
        captured: dict[str, Any] = {}

        def _fake_run(app: Any, host: str = "127.0.0.1", port: int = 8000, **kwargs: Any) -> None:
            captured["app"] = app
            captured["host"] = host
            captured["port"] = port
            captured["kwargs"] = kwargs

        import uvicorn

        monkeypatch.setattr(uvicorn, "run", _fake_run)

        result = runner.invoke(app, ["server", "serve", "--host", "127.0.0.1", "--port", "8000"])
        assert captured.get("host") == "127.0.0.1"
        assert captured.get("port") == 8000
        app_obj = captured.get("app")
        assert app_obj is not None
        assert app_obj.title == "outo-models"
        assert "log_config" in captured.get("kwargs", {})
        assert result.exit_code == 0
