"""TLS / ACME management: Caddyfile rendering + admin-API client.

Caddy runs INSIDE the outo-models container and owns ACME (HTTP-01 with the
optional DNS-01 plugin baked into the image). This module is the Python side
of that contract:

* `TlsConfig`           — typed configuration the setup wizard hands us.
* `render_caddyfile()`  — Jinja renderer for `container/caddy/Caddyfile.j2`.
* `CaddyManager`        — thin async client over Caddy's admin API (POST
                          `/load`, GET `/config/`).

Secrets hygiene: the Cloudflare API token is read by Caddy from its
environment at runtime (`{env.CLOUDFLARE_API_TOKEN}`) and is never embedded
in the rendered Caddyfile. The `CaddyManager` never touches the token —
secret material lives on the operator side, not on the wire we sign.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import jinja2

from outo_models.exceptions import ConfigError, OutoError

# Env var that overrides the package-relative template lookup. Used by tests
# and operators who vendor the template outside the wheel.
_TEMPLATE_ENV_VAR = "OUTO_CADDYFILE_TEMPLATE"

# `src/outo_models/tls/caddy_manager.py` is 3 parents deep from the repo root;
# `parents[3]` lands on the directory that contains `container/`.
_DEFAULT_TEMPLATE_PATH = Path("container") / "caddy" / "Caddyfile.j2"

# Caddy admin API error mapping.
_RELOAD_BAD_REQUEST = "caddy_rejected"  # 4xx
_RELOAD_UPSTREAM = "caddy_unreachable"  # 5xx / network


@dataclass(frozen=True, slots=True)
class TlsConfig:
    """Typed configuration for the TLS layer.

    Attributes:
        domain: The public hostname Caddy will serve (e.g. `models.example.com`).
        email: ACME account contact — Let's Encrypt sends expiry warnings here.
        dns_provider: `None` → Caddy uses HTTP-01. `"cloudflare"` → Caddy uses
            the caddy-dns/cloudflare plugin against the `CLOUDFLARE_API_TOKEN`
            env var on the Caddy process. The token itself is never passed
            to `render_caddyfile`; it must be present in Caddy's environment.
        staging: If True, point ACME at Let's Encrypt's staging CA. Used for
            first installs so a typo in the domain does not burn rate limits.
        admin_url: Caddy admin API base. Default `http://localhost:2019`
            matches the bundled container's bind address.
    """

    domain: str
    email: str
    dns_provider: str | None = None
    staging: bool = False
    admin_url: str = "http://localhost:2019"


def _resolve_template_path() -> Path:
    """Locate the bundled Caddyfile template, honoring the env override."""
    override = os.environ.get(_TEMPLATE_ENV_VAR)
    if override:
        return Path(override)
    repo_root = Path(__file__).resolve().parents[3]
    return repo_root / _DEFAULT_TEMPLATE_PATH


# Single shared Environment, configured with the two block-trim flags so the
# rendered Caddyfile never picks up stray blank lines from conditional blocks.
# `autoescape=False` because the output target is Caddyfile, not HTML — S701
# is a false positive here.
_JINJA_ENV = jinja2.Environment(
    loader=jinja2.FileSystemLoader(str(_resolve_template_path().parent)),
    autoescape=False,  # noqa: S701  # nosec B701 — Caddyfile output, not HTML
    trim_blocks=True,
    lstrip_blocks=True,
    keep_trailing_newline=True,
)


def render_caddyfile(config: TlsConfig) -> str:
    """Render the Caddyfile template against `config`.

    Returns:
        The full Caddyfile text, ready to POST to Caddy's `/load` admin endpoint.

    Raises:
        ConfigError: When the template cannot be located (the path resolution
            env override points at a missing file, or the wheel was installed
            without the bundled template).
    """
    try:
        template = _JINJA_ENV.get_template(_resolve_template_path().name)
    except jinja2.TemplateNotFound as exc:
        raise ConfigError(
            f"Caddyfile template not found at {_resolve_template_path()!s}"
        ) from exc
    return template.render(
        domain=config.domain,
        email=config.email,
        dns_provider=config.dns_provider,
        staging=config.staging,
    )


class CaddyManager:
    """Async client over Caddy's admin API.

    Constructed with a `TlsConfig` (which carries `admin_url`) and an optional
    pre-built `httpx.AsyncClient`. When the client is not injected, the manager
    owns its own client and `close()` tears it down; an injected client is left
    alone so the caller controls its lifecycle (typical for tests).

    Methods:
        reload(): POST the rendered Caddyfile to `/load`. 4xx → `ConfigError`
            with Caddy's response body; 5xx / network → `OutoError(caddy_unreachable)`.
        current_config_hash(): GET `/config/`, return the sha256 of the body.
        healthy(): GET `/config/`, return True iff the response is 200.
        close(): Release the owned client. Idempotent.
    """

    def __init__(
        self,
        config: TlsConfig,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._config = config
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=config.admin_url,
            timeout=httpx.Timeout(10.0, connect=5.0),
        )

    async def reload(self) -> None:
        """POST the rendered Caddyfile to Caddy's admin API.

        Raises:
            ConfigError: When Caddy rejects the config (4xx). The error
                message includes Caddy's response body so the operator can
                diagnose the syntax problem.
            OutoError: When the admin API is unreachable (network error) or
                Caddy returns 5xx. `code="caddy_unreachable"`.
        """
        body = render_caddyfile(self._config)
        try:
            response = await self._client.post(
                "/load",
                content=body,
                headers={"Content-Type": "text/caddyfile"},
            )
        except httpx.HTTPError as exc:
            raise OutoError(
                f"caddy admin unreachable at {self._config.admin_url}: {exc}",
                code="caddy_unreachable",
            ) from exc
        if 400 <= response.status_code < 500:
            response_text = response.text
            raise ConfigError(
                f"caddy rejected the rendered Caddyfile (HTTP {response.status_code}): "
                f"{response_text}"
            )
        if response.status_code >= 500:
            raise OutoError(
                f"caddy admin returned HTTP {response.status_code}: {response.text}",
                code="caddy_unreachable",
            )

    async def current_config_hash(self) -> str:
        """Return the sha256 of Caddy's current config (the `/config/` body).

        Raises:
            OutoError: When the admin API is unreachable.
        """
        try:
            response = await self._client.get("/config/")
        except httpx.HTTPError as exc:
            raise OutoError(
                f"caddy admin unreachable at {self._config.admin_url}: {exc}",
                code="caddy_unreachable",
            ) from exc
        response.raise_for_status()
        return hashlib.sha256(response.content).hexdigest()

    async def healthy(self) -> bool:
        """Return True iff Caddy's admin API responds 200 to `GET /config/`."""
        try:
            response = await self._client.get("/config/")
        except httpx.HTTPError:
            return False
        return response.status_code == 200

    async def close(self) -> None:
        """Release the owned client; no-op for an injected client. Idempotent."""
        if self._owns_client and not self._client.is_closed:
            await self._client.aclose()


__all__: list[str] = ["CaddyManager", "TlsConfig", "render_caddyfile"]


# Internal alias kept private — no longer used externally but referenced in
# type annotations for future expansion (e.g. retry/backoff). Marked Any to
# avoid `unused-import` noise under ruff's `F401`.
_ = Any