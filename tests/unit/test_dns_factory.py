"""Tests for `outo_models.dns.factory`.

The factory is the single point at which concrete providers are wired up.
The setup wizard calls `create_provider(provider, credentials, domain)` after
gathering user input — it must dispatch to the right concrete class and
fail loudly for unknown providers.
"""

from __future__ import annotations

import pytest

from outo_models.dns.base import DNSProvider
from outo_models.dns.cloudflare import CloudflareProvider
from outo_models.dns.factory import create_provider
from outo_models.dns.manual import ManualProvider
from outo_models.exceptions import ConfigError


class TestCreateProviderDispatch:
    """`create_provider` returns the right concrete provider for each name."""

    def test_cloudflare_returns_cloudflare_provider(self) -> None:
        provider = create_provider(
            provider="cloudflare",
            credentials={"api_token": "test-token"},
            domain="models.example.com",
        )
        assert isinstance(provider, CloudflareProvider)
        assert provider.name == "cloudflare"

    def test_manual_returns_manual_provider(self) -> None:
        provider = create_provider(provider="manual", credentials={}, domain="models.example.com")
        assert isinstance(provider, ManualProvider)
        assert provider.name == "manual"

    def test_returned_provider_satisfies_abc(self) -> None:
        provider = create_provider(
            provider="cloudflare",
            credentials={"api_token": "test-token"},
            domain="models.example.com",
        )
        assert isinstance(provider, DNSProvider)


class TestCreateProviderErrors:
    """Unknown provider names must raise `ConfigError`, not `KeyError`."""

    def test_unknown_provider_raises_config_error(self) -> None:
        with pytest.raises(ConfigError) as exc_info:
            create_provider(provider="route53", credentials={}, domain="models.example.com")
        assert exc_info.value.code == "config_error"

    def test_unknown_provider_error_mentions_the_name(self) -> None:
        with pytest.raises(ConfigError) as exc_info:
            create_provider(provider="gandi", credentials={}, domain="models.example.com")
        assert "gandi" in str(exc_info.value)

    def test_empty_provider_raises_config_error(self) -> None:
        with pytest.raises(ConfigError):
            create_provider(provider="", credentials={}, domain="models.example.com")


class TestCloudflareCredentialValidation:
    """Cloudflare requires `api_token`; missing it is a config error."""

    def test_missing_api_token_raises_config_error(self) -> None:
        with pytest.raises(ConfigError):
            create_provider(provider="cloudflare", credentials={}, domain="models.example.com")

    def test_empty_api_token_raises_config_error(self) -> None:
        with pytest.raises(ConfigError):
            create_provider(
                provider="cloudflare",
                credentials={"api_token": ""},
                domain="models.example.com",
            )
