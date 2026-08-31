"""Dispatch `provider` strings onto concrete `DNSProvider` instances.

The setup wizard collects the operator's DNS-provider choice during interactive
setup and then calls `create_provider(provider, credentials, domain)` with the
collected values. The factory owns the dispatch table; adding a new provider
means registering it here and exporting it from `outo_models.dns`.

Unknown provider names are mapped to `ConfigError` so the wizard can surface a
human-readable prompt without catching a generic `KeyError`/`ValueError`.
"""

from __future__ import annotations

from outo_models.dns.base import DNSProvider
from outo_models.dns.cloudflare import CloudflareProvider
from outo_models.dns.manual import ManualProvider
from outo_models.exceptions import ConfigError


def create_provider(
    provider: str,
    credentials: dict[str, str],
    domain: str,
) -> DNSProvider:
    """Return a concrete `DNSProvider` for `provider`.

    Args:
        provider: One of `"cloudflare"`, `"manual"`.
        credentials: Provider-specific secrets. `"cloudflare"` requires
            `"api_token"`; `"manual"` requires nothing.
        domain: The zone domain this provider should manage.

    Raises:
        ConfigError: When `provider` is unknown, or when `credentials` are
            missing for the requested provider.
    """
    if provider == "cloudflare":
        token = credentials.get("api_token", "").strip()
        if not token:
            raise ConfigError("cloudflare provider requires a non-empty 'api_token'")
        return CloudflareProvider(zone_domain=domain, api_token=token)
    if provider == "manual":
        return ManualProvider(zone_domain=domain)
    raise ConfigError(f"unknown dns provider '{provider}'; expected one of: cloudflare, manual")
