"""FastAPI application factory.

`create_app` is the single entry point the ASGI server (`uvicorn`) boots
from. It:

    1. Configures structlog via the active environment.
    2. Ensures the on-disk data directories exist.
    3. Runs `alembic upgrade head` against the resolved DB URL.
    4. Wires the periodic job scheduler (cert renewal, quota reconcile,
       audit prune). A failing scheduler must NOT prevent the app from
       booting in development — we log the failure and continue.
    5. Mounts the JSON routers (auth, users, repos, spaces, admin, webhooks).
    6. Mounts the UI router (Jinja + form POSTs).
    7. Mounts the git smart-HTTP service at root (`/{owner}/{name}.git/...`)
       so clone URLs are HF-style.
    8. Registers exception handlers and the security-headers middleware.

The factory accepts an optional `Settings` override so tests can wire in
their own tmpdir-backed config without monkey-patching `get_settings()`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from outo_models import version
from outo_models.auth.rate_limit import limiter
from outo_models.config import Settings, get_settings
from outo_models.db import dispose_engines, get_engine, run_migrations
from outo_models.git_smart import GitSmartService
from outo_models.logging import configure_logging, get_logger
from outo_models.server.errors import register_exception_handlers
from outo_models.server.middleware import SecurityHeadersMiddleware
from outo_models.server.routers import admin as admin_router
from outo_models.server.routers import auth as auth_router
from outo_models.server.routers import repos as repos_router
from outo_models.server.routers import spaces as spaces_router
from outo_models.server.routers import ui as ui_router
from outo_models.server.routers import users as users_router
from outo_models.server.routers import webhooks as webhooks_router
from outo_models.tasks.scheduler import TaskScheduler
from outo_models.tls.caddy_manager import CaddyManager, TlsConfig
from outo_models.utils.paths import ensure_dirs

_LOGGER = get_logger(__name__)


def _caddy_manager_factory(settings: Settings) -> Callable[[], CaddyManager]:
    """Build a closure that constructs a `CaddyManager` for the scheduler.

    The factory captures the active `settings` so the scheduler can rebuild
    a fresh manager on every tick without re-reading the global state.
    The admin URL defaults to `http://localhost:2019` (matches the
    bundled Caddy container); operators override via `OUTO_CADDY_ADMIN_URL`
    in production setups.
    """

    def _factory() -> CaddyManager:
        return CaddyManager(
            TlsConfig(
                domain=settings.domain,
                email=f"admin@{settings.domain}",
                admin_url="http://localhost:2019",
            )
        )

    return _factory


def _register_routes_and_middleware(
    app: FastAPI, settings: Settings
) -> None:
    """Attach every router + middleware the contract specifies."""
    # Stash the settings so deps.py can resolve them per-request without
    # touching the global cache. Tests build apps with their own Settings;
    # production code lets `get_settings()` return the env-bound singleton.
    app.state.settings = settings

    # Routers: order matters only for URL-prefix collision; we use distinct
    # prefixes everywhere so the order is documentation, not behavior.
    app.include_router(auth_router.router)
    app.include_router(users_router.router)
    app.include_router(repos_router.router)
    app.include_router(spaces_router.router)
    app.include_router(admin_router.router)
    app.include_router(webhooks_router.router)
    app.include_router(ui_router.router)

    # Security headers wrap every outgoing response, including the git
    # smart-HTTP stream — the middleware operates on the ASGI send
    # primitive, so it is compatible with the streaming body the git
    # service emits.
    app.add_middleware(SecurityHeadersMiddleware, settings=settings)

    # Slowapi: stash the limiter on `app.state` and add its middleware so
    # `@limiter.limit(...)` decorators on routes take effect. The exception
    # handler returns a JSON body in the same envelope every other error uses.
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)  # type: ignore[arg-type]
    app.add_middleware(SlowAPIMiddleware)


@asynccontextmanager
async def _lifespan(
    app: FastAPI, settings: Settings
) -> AsyncIterator[None]:
    """Boot the app: migrations, scheduler; teardown the scheduler, dispose engine.

    Scheduler failures during boot are tolerated in development so an
    empty `data_dir` does not prevent the developer from reaching the
    admin setup page. In production the same behavior is desirable:
    if Caddy is unreachable on boot, the operator still wants to be able
    to land on the dashboard and fix the configuration.
    """
    configure_logging(settings.env)
    ensure_dirs()
    engine = get_engine(settings)
    await run_migrations(engine)

    scheduler = TaskScheduler(settings, caddy_manager_factory=_caddy_manager_factory(settings))
    try:
        scheduler.start()
    except Exception:
        _LOGGER.exception("scheduler_start_failed")
    app.state.scheduler = scheduler

    # Git smart-HTTP service mounted at root so URLs are HF-style.
    app.state.git_service = GitSmartService(settings)
    app.mount("/", app.state.git_service.asgi_app())

    try:
        yield
    finally:
        try:
            await scheduler.shutdown(wait=False)
        except Exception:
            _LOGGER.exception("scheduler_shutdown_failed")
        try:
            await engine.dispose()
        except Exception:
            _LOGGER.exception("engine_dispose_failed")
        await dispose_engines()


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build a fresh FastAPI app bound to `settings` (or the global default).

    The factory takes no global state; every dependency reads from the
    `Settings` instance passed here (and propagated via `Depends`).
    """
    if settings is None:
        settings = get_settings()

    @asynccontextmanager
    async def _app_lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with _lifespan(app, settings):
            yield

    app = FastAPI(
        title="outo-models",
        version=version.__version__,
        lifespan=_app_lifespan,
    )
    register_exception_handlers(app)
    _register_routes_and_middleware(app, settings)
    return app


__all__ = ["create_app"]
