"""CLI → server admin REST client.

The `outo-models admin --remote ...` commands use this package to talk
to a running server's `/api/admin/*` endpoints.

Public surface:
    * `AdminApiClient` — synchronous httpx client with bearer PAT auth.
    * `AdminApiError` — typed exception that unifies every transport
      failure.

See `api.py` for the implementation details.
"""

from outo_models.cli_remote.api import AdminApiClient, AdminApiError

__all__ = ["AdminApiClient", "AdminApiError"]
