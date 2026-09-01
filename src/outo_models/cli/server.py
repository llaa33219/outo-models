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
    help="Server and migration commands run inside the container.",
    add_completion=False,
    no_args_is_help=True,
    rich_markup_mode="rich",
)


@server_app.command("serve")
def serve(
    host: str = typer.Option(
        "127.0.0.1",
        "--host",
        help="uvicorn bind host (Caddy reverse-proxies on 443).",
    ),
    port: int = typer.Option(
        8000,
        "--port",
        help="uvicorn bind port.",
        min=1,
        max=65535,
    ),
) -> None:
    """Boot the FastAPI app under uvicorn (container-internal use only)."""
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
    """Run `alembic upgrade head` against the configured DB URL.

    `update.sh` invokes this in a throwaway container with the new
    image. Exits 0 on success, 1 on failure (the host script checks the
    exit code).
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
