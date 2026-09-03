"""TLS layer: Caddyfile rendering + admin-API client + cert health/renewal.

Public API (consumed by WP-11 scheduler jobs and WP-13 server startup):

    from outo_models.tls import (
        TlsConfig,         # typed TLS configuration
        render_caddyfile,  # render assets/caddy/Caddyfile.j2
        CaddyManager,      # async client over Caddy's admin API
        CertHealth,        # cert freshness snapshot
        check_cert_health, # async: TLS handshake, parse notAfter
        renewal_job,       # async: scheduled body — never raises
    )

The Caddy binary and its cloudflare DNS plugin live in the container image
(see `Containerfile`); this module is the Python-side companion that renders
its config and pokes it when certs need to renew.
"""

from outo_models.tls.caddy_manager import CaddyManager, TlsConfig, render_caddyfile
from outo_models.tls.renewal import CertHealth, check_cert_health, renewal_job

__all__ = [
    "CaddyManager",
    "CertHealth",
    "TlsConfig",
    "check_cert_health",
    "render_caddyfile",
    "renewal_job",
]
