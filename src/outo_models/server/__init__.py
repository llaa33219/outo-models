"""FastAPI server layer for outo-models.

Public surface:

    `create_app(settings=None)` — the ASGI factory. Builds a fully wired
    FastAPI app (routers, middleware, security headers, exception handlers,
    lifespan-managed DB engine + scheduler + git smart-HTTP service).

Everything else under `outo_models.server.*` is implementation detail
intentionally not re-exported here — clients of this package should
import the specific router / module they need.
"""

from outo_models.server.app import create_app

__all__ = ["create_app"]
