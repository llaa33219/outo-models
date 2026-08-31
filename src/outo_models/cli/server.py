"""`outo-models serve` and `outo-models migrate` — the in-container commands.

These two commands run INSIDE the podman container (see `Containerfile`):

    * `serve`  — boots `uvicorn` against the FastAPI app returned by
                 `outo_models.server.create_app`.
    * `migrate` — runs `alembic upgrade head` against the configured DB
                 URL so `container/scripts/update.sh` can call it from a
                 throwaway container without booting the whole app.

Both commands share the same `Settings` singleton the rest of the
codebase uses, so an operator who runs `setup` on the host and then
`podman exec outo-models outo-models serve` gets the same database URL,
data dir, and domain as the host-configured install.

Why two separate commands and not one?
    * The update script needs `migrate` to run without uvicorn, so it can
      apply schema changes before the new image's process boots.
    * `serve` should not re-run migrations on every restart — the lifespan
      in `server.app.create_app` already does that. Exposing `migrate`
      separately means we can run it during the update *before* the new
      container starts.
"""

from __future__ import annotations

from pathlib import Path

import typer

from outo_models.cli import render_error, typer_exit
from outo_models.config import get_settings
from outo_models.db.engine import dispose_engines, get_engine, run_migrations

# Typer sub-app: `outo-models serve` / `outo-models migrate`. The `invoke
# without command` is suppressed because this is a leaf app — it's always
# invoked from the parent Typer `app` in `main.py`.
server_app = typer.Typer(
    name="server",
    help="컨테이너 내부에서 실행되는 서버 / 마이그레이션 명령",
    add_completion=False,
    no_args_is_help=True,
    rich_markup_mode="rich",
)


@server_app.command("serve")
def serve(
    host: str = typer.Option(
        "127.0.0.1",
        "--host",
        help="uvicorn 바인딩 호스트 (Caddy가 443에서 reverse-proxy).",
    ),
    port: int = typer.Option(
        8000,
        "--port",
        help="uvicorn 바인딩 포트.",
        min=1,
        max=65535,
    ),
) -> None:
    """FastAPI 앱을 uvicorn으로 부팅합니다 (컨테이너 내부 전용)."""
    try:
        import uvicorn

        from outo_models.server import create_app

        settings = get_settings()
        app = create_app(settings)
        # `uvicorn.run` blocks until the server stops; we never need to
        # dispose engines afterwards because uvicorn owns the process
        # lifecycle from here on.
        uvicorn.run(app, host=host, port=port, log_config=None)
    except Exception as exc:
        render_error(exc)
        raise typer_exit(1) from exc


@server_app.command("migrate")
def migrate() -> None:
    """설정된 DB URL에 대해 `alembic upgrade head`를 실행합니다.

    `update.sh`가 새 이미지로 throwaway 컨테이너에서 호출합니다. 성공 시
    0, 실패 시 1로 종료합니다 (호스트 스크립트가 exit code를 검사).
    """
    import asyncio

    async def _run() -> None:
        settings = get_settings()
        engine = get_engine(settings)
        try:
            await run_migrations(engine)
        finally:
            # Must share the event loop with `run_migrations` so the engine's
            # pool is disposed by the same loop that created it.
            await dispose_engines()

    try:
        asyncio.run(_run())
    except Exception as exc:
        render_error(exc)
        raise typer_exit(1) from exc


# `Path` is referenced so ruff does not complain about an unused import
# when callers strip the helper — `host` / `port` in this file are plain
# strings/ints, but future overrides might want to expose config-file
# paths. The marker keeps the import explicit.
_ = Path


__all__ = ["migrate", "serve", "server_app"]
