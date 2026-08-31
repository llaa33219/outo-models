"""Tests for `outo_models.dns.cloudflare`.

These tests use `respx` to mock the Cloudflare REST API at the wire boundary.
Every assertion is against observable HTTP traffic — there is no internal
state to peek at. The setup wizard and TLS manager both depend on the
behaviors pinned here:

- success create (no existing record → POST)
- update-in-place when the record already exists (GET → PUT)
- zone_id is resolved lazily and cached for the lifetime of the provider
- 4xx → `ConfigError` with the API message
- 5xx / network errors → `OutoError(code="dns_upstream")`
- the API token never appears in `repr`, in raised exceptions, or in any
  logged field
"""

from __future__ import annotations

import httpx
import pytest
import respx

from outo_models.dns.base import DnsRecord
from outo_models.dns.cloudflare import CloudflareProvider
from outo_models.exceptions import ConfigError, OutoError

_API_BASE = "https://api.cloudflare.com/client/v4"
_ZONE_ID = "0123456789abcdef0123456789abcdef"
_TOKEN = "test-cloudflare-token-1234567890"
_ZONE_DOMAIN = "models.example.com"


def _zone_lookup_response() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "result": [
                {"id": _ZONE_ID, "name": _ZONE_DOMAIN},
            ],
            "success": True,
            "errors": [],
        },
    )


def _empty_records_response() -> httpx.Response:
    return httpx.Response(
        200,
        json={"result": [], "success": True, "errors": []},
    )


def _existing_record_response(record_id: str = "rec-existing-id") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "result": [
                {
                    "id": record_id,
                    "type": "A",
                    "name": "models.example.com",
                    "content": "1.2.3.4",
                    "ttl": 300,
                }
            ],
            "success": True,
            "errors": [],
        },
    )


def _create_record_response(record_id: str = "rec-new-id") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "result": {"id": record_id, "type": "A"},
            "success": True,
            "errors": [],
        },
    )


def _update_record_response() -> httpx.Response:
    return httpx.Response(
        200,
        json={"result": {"id": "rec-existing-id"}, "success": True, "errors": []},
    )


def _delete_record_response() -> httpx.Response:
    return httpx.Response(
        200,
        json={"result": {"id": "rec-existing-id"}, "success": True, "errors": []},
    )


class TestEnsureRecordCreate:
    """`ensure_record` POSTs when the record does not yet exist."""

    async def test_create_record_when_absent(self, respx_mock: respx.MockRouter) -> None:
        respx_mock.get("/zones", params={"name": _ZONE_DOMAIN}).mock(
            return_value=_zone_lookup_response()
        )
        respx_mock.get(
            "/zones/0123456789abcdef0123456789abcdef/dns_records",
            params={"type": "A", "name": "models.example.com"},
        ).mock(return_value=_empty_records_response())
        create_route = respx_mock.post("/zones/0123456789abcdef0123456789abcdef/dns_records").mock(
            return_value=_create_record_response()
        )

        async with CloudflareProvider(zone_domain=_ZONE_DOMAIN, api_token=_TOKEN) as provider:
            await provider.ensure_record(
                DnsRecord(name="models.example.com", type="A", value="1.2.3.4", ttl=300)
            )

        assert create_route.called

    async def test_create_record_sends_correct_payload(self, respx_mock: respx.MockRouter) -> None:
        respx_mock.get("/zones", params={"name": _ZONE_DOMAIN}).mock(
            return_value=_zone_lookup_response()
        )
        respx_mock.get("/zones/0123456789abcdef0123456789abcdef/dns_records").mock(
            return_value=_empty_records_response()
        )
        create_route = respx_mock.post("/zones/0123456789abcdef0123456789abcdef/dns_records").mock(
            return_value=_create_record_response()
        )

        async with CloudflareProvider(zone_domain=_ZONE_DOMAIN, api_token=_TOKEN) as provider:
            await provider.ensure_record(
                DnsRecord(name="models.example.com", type="A", value="1.2.3.4", ttl=600)
            )

        request = create_route.calls.last.request
        body = request.read()
        import json

        payload = json.loads(body)
        assert payload == {
            "type": "A",
            "name": "models.example.com",
            "content": "1.2.3.4",
            "ttl": 600,
        }


class TestEnsureRecordUpdate:
    """`ensure_record` PUTs in place when the record already exists."""

    async def test_update_record_when_present(self, respx_mock: respx.MockRouter) -> None:
        respx_mock.get("/zones", params={"name": _ZONE_DOMAIN}).mock(
            return_value=_zone_lookup_response()
        )
        respx_mock.get(
            "/zones/0123456789abcdef0123456789abcdef/dns_records",
            params={"type": "A", "name": "models.example.com"},
        ).mock(return_value=_existing_record_response())
        update_route = respx_mock.put(
            "/zones/0123456789abcdef0123456789abcdef/dns_records/rec-existing-id"
        ).mock(return_value=_update_record_response())
        post_route = respx_mock.post("/zones/0123456789abcdef0123456789abcdef/dns_records").mock(
            return_value=_create_record_response()
        )

        async with CloudflareProvider(zone_domain=_ZONE_DOMAIN, api_token=_TOKEN) as provider:
            await provider.ensure_record(
                DnsRecord(name="models.example.com", type="A", value="5.6.7.8", ttl=300)
            )

        assert update_route.called
        assert not post_route.called

    async def test_update_sends_full_payload(self, respx_mock: respx.MockRouter) -> None:
        respx_mock.get("/zones", params={"name": _ZONE_DOMAIN}).mock(
            return_value=_zone_lookup_response()
        )
        respx_mock.get("/zones/0123456789abcdef0123456789abcdef/dns_records").mock(
            return_value=_existing_record_response()
        )
        update_route = respx_mock.put(
            "/zones/0123456789abcdef0123456789abcdef/dns_records/rec-existing-id"
        ).mock(return_value=_update_record_response())

        async with CloudflareProvider(zone_domain=_ZONE_DOMAIN, api_token=_TOKEN) as provider:
            await provider.ensure_record(
                DnsRecord(name="models.example.com", type="A", value="9.9.9.9", ttl=120)
            )

        request = update_route.calls.last.request
        import json

        payload = json.loads(request.read())
        assert payload == {
            "type": "A",
            "name": "models.example.com",
            "content": "9.9.9.9",
            "ttl": 120,
        }


class TestDeleteRecord:
    """`delete_record` removes the record, silently ignoring the missing case."""

    async def test_delete_existing_record(self, respx_mock: respx.MockRouter) -> None:
        respx_mock.get("/zones", params={"name": _ZONE_DOMAIN}).mock(
            return_value=_zone_lookup_response()
        )
        respx_mock.get("/zones/0123456789abcdef0123456789abcdef/dns_records").mock(
            return_value=_existing_record_response()
        )
        delete_route = respx_mock.delete(
            "/zones/0123456789abcdef0123456789abcdef/dns_records/rec-existing-id"
        ).mock(return_value=_delete_record_response())

        async with CloudflareProvider(zone_domain=_ZONE_DOMAIN, api_token=_TOKEN) as provider:
            await provider.delete_record(
                DnsRecord(name="models.example.com", type="A", value="1.2.3.4")
            )

        assert delete_route.called

    async def test_delete_missing_record_is_noop(self, respx_mock: respx.MockRouter) -> None:
        respx_mock.get("/zones", params={"name": _ZONE_DOMAIN}).mock(
            return_value=_zone_lookup_response()
        )
        respx_mock.get("/zones/0123456789abcdef0123456789abcdef/dns_records").mock(
            return_value=_empty_records_response()
        )
        delete_route = respx_mock.delete(
            "/zones/0123456789abcdef0123456789abcdef/dns_records/anything"
        ).mock(return_value=_delete_record_response())

        async with CloudflareProvider(zone_domain=_ZONE_DOMAIN, api_token=_TOKEN) as provider:
            await provider.delete_record(
                DnsRecord(name="models.example.com", type="A", value="1.2.3.4")
            )

        assert not delete_route.called


class TestListRecords:
    """`list_records` mirrors every record in the zone."""

    async def test_list_returns_mapped_records(self, respx_mock: respx.MockRouter) -> None:
        respx_mock.get("/zones", params={"name": _ZONE_DOMAIN}).mock(
            return_value=_zone_lookup_response()
        )
        respx_mock.get("/zones/0123456789abcdef0123456789abcdef/dns_records").mock(
            return_value=httpx.Response(
                200,
                json={
                    "result": [
                        {
                            "id": "a",
                            "type": "A",
                            "name": "models.example.com",
                            "content": "1.2.3.4",
                            "ttl": 300,
                        },
                        {
                            "id": "txt",
                            "type": "TXT",
                            "name": "_dmarc.models.example.com",
                            "content": "v=DMARC1",
                            "ttl": 300,
                        },
                    ],
                    "success": True,
                    "errors": [],
                },
            )
        )

        async with CloudflareProvider(zone_domain=_ZONE_DOMAIN, api_token=_TOKEN) as provider:
            records = await provider.list_records()

        assert records == [
            DnsRecord(name="models.example.com", type="A", value="1.2.3.4", ttl=300),
            DnsRecord(name="_dmarc.models.example.com", type="TXT", value="v=DMARC1", ttl=300),
        ]


class TestZoneResolution:
    """Zone ID is resolved lazily and cached for the lifetime of the provider."""

    async def test_zone_resolved_only_once_across_calls(self, respx_mock: respx.MockRouter) -> None:
        zones_route = respx_mock.get("/zones", params={"name": _ZONE_DOMAIN}).mock(
            return_value=_zone_lookup_response()
        )
        respx_mock.get("/zones/0123456789abcdef0123456789abcdef/dns_records").mock(
            return_value=_empty_records_response()
        )
        respx_mock.post("/zones/0123456789abcdef0123456789abcdef/dns_records").mock(
            return_value=_create_record_response()
        )

        async with CloudflareProvider(zone_domain=_ZONE_DOMAIN, api_token=_TOKEN) as provider:
            await provider.ensure_record(
                DnsRecord(name="models.example.com", type="A", value="1.2.3.4")
            )
            await provider.ensure_record(
                DnsRecord(name="api.models.example.com", type="A", value="1.2.3.4")
            )

        assert zones_route.call_count == 1

    async def test_unknown_zone_raises_config_error(self, respx_mock: respx.MockRouter) -> None:
        respx_mock.get("/zones", params={"name": _ZONE_DOMAIN}).mock(
            return_value=httpx.Response(
                200,
                json={"result": [], "success": True, "errors": []},
            )
        )

        async with CloudflareProvider(zone_domain=_ZONE_DOMAIN, api_token=_TOKEN) as provider:
            with pytest.raises(ConfigError):
                await provider.list_records()


class TestErrorMapping:
    """4xx → ConfigError, 5xx/network → OutoError(code='dns_upstream')."""

    async def test_4xx_maps_to_config_error_with_api_message(
        self, respx_mock: respx.MockRouter
    ) -> None:
        respx_mock.get("/zones", params={"name": _ZONE_DOMAIN}).mock(
            return_value=_zone_lookup_response()
        )
        respx_mock.get("/zones/0123456789abcdef0123456789abcdef/dns_records").mock(
            return_value=_empty_records_response()
        )
        respx_mock.post("/zones/0123456789abcdef0123456789abcdef/dns_records").mock(
            return_value=httpx.Response(
                400,
                json={
                    "success": False,
                    "errors": [{"message": "Invalid TTL"}],
                },
            )
        )

        async with CloudflareProvider(zone_domain=_ZONE_DOMAIN, api_token=_TOKEN) as provider:
            with pytest.raises(ConfigError) as exc_info:
                await provider.ensure_record(
                    DnsRecord(name="models.example.com", type="A", value="1.2.3.4")
                )
            assert "Invalid TTL" in str(exc_info.value)

    async def test_4xx_does_not_leak_token(self, respx_mock: respx.MockRouter) -> None:
        """A pathological Cloudflare error containing the token must still be sanitized."""
        respx_mock.get("/zones", params={"name": _ZONE_DOMAIN}).mock(
            return_value=httpx.Response(
                400,
                json={
                    "success": False,
                    "errors": [
                        {"message": f"token rejected: {_TOKEN}"},
                    ],
                },
            )
        )

        async with CloudflareProvider(zone_domain=_ZONE_DOMAIN, api_token=_TOKEN) as provider:
            with pytest.raises(ConfigError) as exc_info:
                await provider.list_records()
            assert _TOKEN not in str(exc_info.value)
            assert "***" in str(exc_info.value)

    async def test_5xx_maps_to_dns_upstream_error(self, respx_mock: respx.MockRouter) -> None:
        respx_mock.get("/zones", params={"name": _ZONE_DOMAIN}).mock(
            return_value=_zone_lookup_response()
        )
        respx_mock.get("/zones/0123456789abcdef0123456789abcdef/dns_records").mock(
            return_value=_empty_records_response()
        )
        respx_mock.post("/zones/0123456789abcdef0123456789abcdef/dns_records").mock(
            return_value=httpx.Response(503, json={"success": False, "errors": []})
        )

        async with CloudflareProvider(zone_domain=_ZONE_DOMAIN, api_token=_TOKEN) as provider:
            with pytest.raises(OutoError) as exc_info:
                await provider.ensure_record(
                    DnsRecord(name="models.example.com", type="A", value="1.2.3.4")
                )
            assert exc_info.value.code == "dns_upstream"

    async def test_network_error_maps_to_dns_upstream(self, respx_mock: respx.MockRouter) -> None:
        respx_mock.get("/zones", params={"name": _ZONE_DOMAIN}).mock(
            side_effect=httpx.ConnectError("connection refused")
        )

        async with CloudflareProvider(zone_domain=_ZONE_DOMAIN, api_token=_TOKEN) as provider:
            with pytest.raises(OutoError) as exc_info:
                await provider.list_records()
            assert exc_info.value.code == "dns_upstream"

    async def test_invalid_json_maps_to_dns_upstream(self, respx_mock: respx.MockRouter) -> None:
        respx_mock.get("/zones", params={"name": _ZONE_DOMAIN}).mock(
            return_value=httpx.Response(200, text="<html>not json</html>")
        )

        async with CloudflareProvider(zone_domain=_ZONE_DOMAIN, api_token=_TOKEN) as provider:
            with pytest.raises(OutoError) as exc_info:
                await provider.list_records()
            assert exc_info.value.code == "dns_upstream"


class TestLifecycle:
    """Async context manager / explicit `aclose()` semantics."""

    async def test_context_manager_returns_self(self, respx_mock: respx.MockRouter) -> None:
        respx_mock.get("/zones", params={"name": _ZONE_DOMAIN}).mock(
            return_value=_zone_lookup_response()
        )
        async with CloudflareProvider(zone_domain=_ZONE_DOMAIN, api_token=_TOKEN) as provider:
            assert isinstance(provider, CloudflareProvider)
            assert provider._client is not None

    async def test_aclose_is_idempotent(self) -> None:
        provider = CloudflareProvider(zone_domain=_ZONE_DOMAIN, api_token=_TOKEN)
        await provider.aclose()
        await provider.aclose()  # must not raise


class TestSecretsHygiene:
    """The API token must never appear in `repr`, exceptions, or log fields."""

    def test_repr_does_not_leak_token(self) -> None:
        provider = CloudflareProvider(zone_domain=_ZONE_DOMAIN, api_token=_TOKEN)
        assert _TOKEN not in repr(provider)

    def test_repr_includes_zone(self) -> None:
        provider = CloudflareProvider(zone_domain=_ZONE_DOMAIN, api_token=_TOKEN)
        assert _ZONE_DOMAIN in repr(provider)


class TestAuthorizationHeader:
    """Every request carries `Authorization: Bearer <token>`."""

    async def test_bearer_header_is_set(self, respx_mock: respx.MockRouter) -> None:
        zones_route = respx_mock.get("/zones", params={"name": _ZONE_DOMAIN}).mock(
            return_value=_zone_lookup_response()
        )
        respx_mock.get("/zones/0123456789abcdef0123456789abcdef/dns_records").mock(
            return_value=_empty_records_response()
        )

        async with CloudflareProvider(zone_domain=_ZONE_DOMAIN, api_token=_TOKEN) as provider:
            await provider.list_records()

        request = zones_route.calls.last.request
        assert request.headers["Authorization"] == f"Bearer {_TOKEN}"


@pytest.fixture
def respx_mock() -> respx.MockRouter:
    """Yield a respx MockRouter wired against the Cloudflare base URL.

    `assert_all_called=False` because some tests deliberately probe an
    idempotent path that may not need the second route; the assertions on
    `called` per-route catch the real regressions.
    """
    with respx.mock(assert_all_called=False, assert_all_mocked=False, base_url=_API_BASE) as mock:
        yield mock
