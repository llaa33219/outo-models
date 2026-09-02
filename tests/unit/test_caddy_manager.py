"""Tests for `outo_models.tls.caddy_manager`.

Covers the Jinja template (every variant of `staging x dns_provider`),
the structural sanity of the rendered Caddyfile, the `CaddyManager`
HTTP client — `reload()` success/4xx/network mapping, `healthy()`, and
`current_config_hash()`. All HTTP interactions are mocked with `respx`
so the unit test does not need a live Caddy.
"""

from __future__ import annotations

import hashlib

import httpx
import pytest
import respx

from outo_models.config import Settings
from outo_models.exceptions import ConfigError, OutoError
from outo_models.tls.caddy_manager import CaddyManager, TlsConfig, render_caddyfile

# `{{` is the Jinja expression opener — if it survives into the rendered
# text, the template silently dropped a variable. Caddyfile itself never
# contains `{{`.
_JINJA_LEFTOVER = "{{"

# A Cloudflare-style token that must NEVER appear in any rendered output.
# The Caddyfile references `{env.CLOUDFLARE_API_TOKEN}` by name; the actual
# value is read from Caddy's environment, never written to disk.
_FAKE_CF_TOKEN = "cf-leaked-token-DN-zzzzzzzzzzzzzzzzzzzz"


def _assert_braces_balanced(rendered: str) -> None:
    """Every `{` must have a matching `}` and vice versa, ignoring `{{`/`}}`.

    Caddyfile is brace-delimited. A balanced-brace sanity check catches the
    most common template bugs (forgotten `{% endif %}`, mismatched `tls {`).
    """
    depth = 0
    i = 0
    while i < len(rendered):
        ch = rendered[i]
        if ch == "{":
            # Skip Jinja expression opener `{{` — these are not Caddy braces.
            if i + 1 < len(rendered) and rendered[i + 1] == "{":
                i += 2
                continue
            depth += 1
        elif ch == "}":
            if i + 1 < len(rendered) and rendered[i + 1] == "}":
                i += 2
                continue
            depth -= 1
            if depth < 0:
                pytest.fail(f"unbalanced closing brace at index {i}: {rendered!r}")
        i += 1
    if depth != 0:
        pytest.fail(f"unbalanced braces (depth={depth}): {rendered!r}")


class TestRenderCaddyfile:
    """Every (staging, dns_provider) combination renders a sane Caddyfile."""

    def test_http01_production(self) -> None:
        rendered = render_caddyfile(
            TlsConfig(domain="models.example.com", email="admin@example.com")
        )
        assert "models.example.com" in rendered
        assert "admin@example.com" in rendered
        assert "127.0.0.1:8000" in rendered
        assert "reverse_proxy" in rendered
        assert "/healthz" in rendered
        assert "acme-staging" not in rendered
        assert "tls {" not in rendered
        assert _JINJA_LEFTOVER not in rendered
        _assert_braces_balanced(rendered)

    def test_http01_staging(self) -> None:
        rendered = render_caddyfile(
            TlsConfig(
                domain="models.example.com",
                email="admin@example.com",
                staging=True,
            )
        )
        assert "acme-staging-v02.api.letsencrypt.org/directory" in rendered
        assert "tls {" not in rendered
        assert _JINJA_LEFTOVER not in rendered
        _assert_braces_balanced(rendered)

    def test_dns01_production(self) -> None:
        rendered = render_caddyfile(
            TlsConfig(
                domain="models.example.com",
                email="admin@example.com",
                dns_provider="cloudflare",
            )
        )
        assert "tls {" in rendered
        assert "dns cloudflare" in rendered
        assert "{env.CLOUDFLARE_API_TOKEN}" in rendered
        assert "acme-staging" not in rendered
        assert _JINJA_LEFTOVER not in rendered
        _assert_braces_balanced(rendered)

    def test_dns01_staging(self) -> None:
        rendered = render_caddyfile(
            TlsConfig(
                domain="models.example.com",
                email="admin@example.com",
                staging=True,
                dns_provider="cloudflare",
            )
        )
        assert "acme-staging-v02.api.letsencrypt.org/directory" in rendered
        assert "tls {" in rendered
        assert "dns cloudflare" in rendered
        assert "{env.CLOUDFLARE_API_TOKEN}" in rendered
        assert _JINJA_LEFTOVER not in rendered
        _assert_braces_balanced(rendered)

    def test_rendering_never_embeds_an_api_token(self) -> None:
        """Belt-and-braces: a token leaked into config must not surface in the Caddyfile."""
        rendered = render_caddyfile(
            TlsConfig(
                domain="models.example.com",
                email="admin@example.com",
                dns_provider="cloudflare",
            )
        )
        assert _FAKE_CF_TOKEN not in rendered
        # Generic catch for token-shaped substrings (40+ alnum/_/-).
        import re

        assert not re.search(r"[A-Za-z0-9_-]{32,}", rendered), (
            "rendered Caddyfile contains a token-shaped string; "
            "the Caddyfile must only reference {env.VAR_NAME} placeholders"
        )

    def test_email_special_characters_are_preserved(self) -> None:
        """Domains with hyphens and emails with `+` aliases must not be HTML-escaped.

        Jinja2 autoescape defaults to False for non-HTML templates — verify
        the rendered output stays literal so Caddy can parse it.
        """
        rendered = render_caddyfile(
            TlsConfig(
                domain="models-with-dashes.example.com",
                email="admin+acme@example.com",
            )
        )
        assert "models-with-dashes.example.com" in rendered
        assert "admin+acme@example.com" in rendered
        assert "&#64;" not in rendered  # would indicate accidental HTML escaping


class TestRenderCaddyfileAllVariants:
    """Structural sanity sweep across the full 2x2 input grid."""

    @pytest.mark.parametrize(
        ("staging", "dns_provider"),
        [
            (False, None),
            (False, "cloudflare"),
            (True, None),
            (True, "cloudflare"),
        ],
    )
    def test_all_variants_have_balanced_braces_and_no_jinja_leftovers(
        self, staging: bool, dns_provider: str | None
    ) -> None:
        rendered = render_caddyfile(
            TlsConfig(
                domain="models.example.com",
                email="admin@example.com",
                dns_provider=dns_provider,
                staging=staging,
            )
        )
        assert _JINJA_LEFTOVER not in rendered
        _assert_braces_balanced(rendered)
        assert "models.example.com" in rendered
        assert "admin@example.com" in rendered
        if staging:
            assert "acme-staging" in rendered
        else:
            assert "acme-staging" not in rendered
        if dns_provider == "cloudflare":
            assert "tls {" in rendered
        else:
            assert "tls {" not in rendered


class TestTlsConfigReplace:
    """`dataclasses.replace` is the idiomatic way to vary one field per test."""

    def test_replace_returns_new_instance(self) -> None:
        import dataclasses

        original = TlsConfig(domain="x.example.com", email="a@example.com")
        mutated = dataclasses.replace(original, staging=True)
        assert original.staging is False
        assert mutated.staging is True
        assert mutated.domain == original.domain


class TestTlsConfigFromSettings:
    """`TlsConfig.from_settings` derives `tls_enabled` from `Settings.is_internal`."""

    def test_tls_enabled_for_hostname_settings(self) -> None:
        s = Settings(domain="models.example.com", _env_file=None)  # type: ignore[call-arg]
        config = TlsConfig.from_settings(s, email="ops@example.com")
        assert config.tls_enabled is True
        assert config.domain == "models.example.com"

    def test_tls_disabled_for_ip_settings(self) -> None:
        s = Settings(domain="192.168.1.10", _env_file=None)  # type: ignore[call-arg]
        config = TlsConfig.from_settings(s, email="ops@example.com")
        assert config.tls_enabled is False

    def test_tls_disabled_for_empty_settings(self) -> None:
        s = Settings(domain="", _env_file=None)  # type: ignore[call-arg]
        config = TlsConfig.from_settings(s, email="ops@example.com")
        assert config.tls_enabled is False

    def test_overrides_are_honored(self) -> None:
        s = Settings(domain="models.example.com", _env_file=None)  # type: ignore[call-arg]
        config = TlsConfig.from_settings(
            s,
            email="custom@example.com",
            dns_provider="cloudflare",
            staging=True,
            admin_url="http://example:9999",
        )
        assert config.email == "custom@example.com"
        assert config.dns_provider == "cloudflare"
        assert config.staging is True
        assert config.admin_url == "http://example:9999"


class TestRenderCaddyfileInternal:
    """`tls_enabled=False` produces the plain-HTTP site block."""

    def test_internal_renders_port_80_block(self) -> None:
        rendered = render_caddyfile(
            TlsConfig(
                domain="192.168.1.10",
                email="admin@example.com",
                tls_enabled=False,
            )
        )
        assert ":80 {" in rendered
        assert "reverse_proxy 127.0.0.1:8000" in rendered
        assert "acme-staging" not in rendered
        assert "email admin@example.com" not in rendered
        assert "tls {" not in rendered
        assert "/healthz" in rendered
        assert _JINJA_LEFTOVER not in rendered
        _assert_braces_balanced(rendered)

    def test_internal_ignores_dns_provider_and_staging(self) -> None:
        rendered = render_caddyfile(
            TlsConfig(
                domain="192.168.1.10",
                email="admin@example.com",
                dns_provider="cloudflare",
                staging=True,
                tls_enabled=False,
            )
        )
        assert ":80 {" in rendered
        assert "acme-staging" not in rendered
        assert "tls {" not in rendered
        assert "dns cloudflare" not in rendered
        assert _JINJA_LEFTOVER not in rendered
        _assert_braces_balanced(rendered)

    def test_internal_does_not_leak_token(self) -> None:
        rendered = render_caddyfile(
            TlsConfig(
                domain="192.168.1.10",
                email="admin@example.com",
                tls_enabled=False,
            )
        )
        assert _FAKE_CF_TOKEN not in rendered

    def test_hostname_flow_still_renders_acme_blocks(self) -> None:
        # The hostname branch must keep emitting the email / per-site
        # block — the internal-mode refactor must not break it.
        rendered = render_caddyfile(
            TlsConfig(
                domain="models.example.com",
                email="admin@example.com",
                tls_enabled=True,
            )
        )
        assert "email admin@example.com" in rendered
        assert "acme-staging" not in rendered
        assert "models.example.com {" in rendered
        assert "tls {" not in rendered
        _assert_braces_balanced(rendered)


class TestCaddyManagerReload:
    """`reload()` POSTs the rendered Caddyfile to the admin API and maps errors."""

    async def test_reload_success(self, respx_mock: respx.MockRouter) -> None:
        route = respx_mock.post("/load").mock(return_value=httpx.Response(200, text=""))
        manager = CaddyManager(TlsConfig(domain="models.example.com", email="admin@example.com"))
        try:
            await manager.reload()
        finally:
            await manager.close()
        assert route.called
        request = route.calls.last.request
        assert request.headers["Content-Type"] == "text/caddyfile"
        body = request.read().decode()
        assert "models.example.com" in body
        assert "admin@example.com" in body

    async def test_reload_4xx_raises_config_error_with_caddy_body(
        self, respx_mock: respx.MockRouter
    ) -> None:
        respx_mock.post("/load").mock(
            return_value=httpx.Response(400, text="unrecognized directive: wibble")
        )
        manager = CaddyManager(TlsConfig(domain="models.example.com", email="admin@example.com"))
        try:
            with pytest.raises(ConfigError) as exc_info:
                await manager.reload()
            assert "unrecognized directive: wibble" in str(exc_info.value)
        finally:
            await manager.close()

    async def test_reload_network_error_raises_caddy_unreachable(
        self, respx_mock: respx.MockRouter
    ) -> None:
        respx_mock.post("/load").mock(side_effect=httpx.ConnectError("connection refused"))
        manager = CaddyManager(TlsConfig(domain="models.example.com", email="admin@example.com"))
        try:
            with pytest.raises(OutoError) as exc_info:
                await manager.reload()
            assert exc_info.value.code == "caddy_unreachable"
        finally:
            await manager.close()

    async def test_reload_5xx_maps_to_caddy_unreachable(self, respx_mock: respx.MockRouter) -> None:
        respx_mock.post("/load").mock(return_value=httpx.Response(503, text="boom"))
        manager = CaddyManager(TlsConfig(domain="models.example.com", email="admin@example.com"))
        try:
            with pytest.raises(OutoError) as exc_info:
                await manager.reload()
            assert exc_info.value.code == "caddy_unreachable"
        finally:
            await manager.close()


class TestCaddyManagerHealth:
    """`healthy()` and `current_config_hash()` hit `/config/`."""

    async def test_healthy_returns_true_on_200(self, respx_mock: respx.MockRouter) -> None:
        respx_mock.get("/config/").mock(return_value=httpx.Response(200, text="{}"))
        manager = CaddyManager(TlsConfig(domain="models.example.com", email="admin@example.com"))
        try:
            assert await manager.healthy() is True
        finally:
            await manager.close()

    async def test_healthy_returns_false_on_non_200(self, respx_mock: respx.MockRouter) -> None:
        respx_mock.get("/config/").mock(return_value=httpx.Response(500, text="boom"))
        manager = CaddyManager(TlsConfig(domain="models.example.com", email="admin@example.com"))
        try:
            assert await manager.healthy() is False
        finally:
            await manager.close()

    async def test_healthy_returns_false_on_network_error(
        self, respx_mock: respx.MockRouter
    ) -> None:
        respx_mock.get("/config/").mock(side_effect=httpx.ConnectError("refused"))
        manager = CaddyManager(TlsConfig(domain="models.example.com", email="admin@example.com"))
        try:
            assert await manager.healthy() is False
        finally:
            await manager.close()

    async def test_current_config_hash_is_sha256_of_body(
        self, respx_mock: respx.MockRouter
    ) -> None:
        body = b'{"apps":{"http":{}}}'
        respx_mock.get("/config/").mock(return_value=httpx.Response(200, content=body))
        manager = CaddyManager(TlsConfig(domain="models.example.com", email="admin@example.com"))
        try:
            digest = await manager.current_config_hash()
        finally:
            await manager.close()
        assert digest == hashlib.sha256(body).hexdigest()


class TestCaddyManagerLifecycle:
    """Lifecycle: explicit `close()` and injected client."""

    async def test_close_is_idempotent(self) -> None:
        manager = CaddyManager(TlsConfig(domain="models.example.com", email="admin@example.com"))
        await manager.close()
        await manager.close()  # must not raise

    async def test_injected_client_is_not_closed_on_close(self) -> None:
        async with httpx.AsyncClient(base_url="http://localhost:2019") as owned:
            manager = CaddyManager(
                TlsConfig(domain="models.example.com", email="admin@example.com"),
                client=owned,
            )
            await manager.close()
            # The injected client must remain usable after the manager closes.
            assert not owned.is_closed


@pytest.fixture
def respx_mock() -> respx.MockRouter:
    """Yield a respx MockRouter scoped to the local Caddy admin port."""
    with respx.mock(
        assert_all_called=False,
        assert_all_mocked=False,
        base_url="http://localhost:2019",
    ) as mock:
        yield mock
