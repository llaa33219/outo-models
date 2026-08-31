"""Cloudflare DNS provider.

Implements `DNSProvider` against the Cloudflare REST API
(`https://api.cloudflare.com/client/v4`). Authenticated with a scoped API
token via `Authorization: Bearer …`.

Error mapping contract (from the API contract):
- Cloudflare 4xx (including validation errors) → `ConfigError` with the API's
  human-readable message, so the setup wizard can surface it to the operator.
- Network errors and Cloudflare 5xx → `OutoError(code="dns_upstream")`. The
  setup wizard treats this as transient and offers to retry.

Secrets hygiene: the API token never appears in `repr()`, in raised
exceptions, or in logged fields. Zone lookups are cached for the lifetime of
the provider — the setup wizard runs in a single CLI invocation, so the
cache never goes stale in practice.
"""

from __future__ import annotations

import re
from typing import Any

import httpx

from outo_models.dns.base import DNSProvider, DnsRecord
from outo_models.exceptions import ConfigError, OutoError

_BASE_URL = "https://api.cloudflare.com/client/v4"
_REDACTION_MARK = "***"


def _sanitize_message(message: str) -> str:
    """Redact any bearer token-shaped substring from a Cloudflare error message.

    Belt-and-braces guard against Cloudflare ever echoing the token back in
    its error payloads. We never expect to need it, but if it ever does fire
    the operator still gets a meaningful error rather than a leaked secret.
    """
    # CF tokens are 40-char hex/alnum blobs; mask anything that looks like one
    # when it appears inside a quoted error message.
    return re.sub(r"[A-Za-z0-9_-]{32,}", _REDACTION_MARK, message)


class CloudflareProvider(DNSProvider):
    """`DNSProvider` backed by the Cloudflare REST API.

    Args:
        zone_domain: The apex zone the operator delegates to Cloudflare.
            Sub-records are addressed relative to this zone.
        api_token: A scoped Cloudflare API token with `Zone.DNS:Edit`
            permission on `zone_domain`.
    """

    name = "cloudflare"

    def __init__(self, zone_domain: str, api_token: str) -> None:
        self._zone_domain = zone_domain
        self._api_token = api_token
        self._client: httpx.AsyncClient | None = None
        self._zone_id: str | None = None

    # --- lifecycle ----------------------------------------------------------

    async def __aenter__(self) -> CloudflareProvider:
        self._client = httpx.AsyncClient(
            base_url=_BASE_URL,
            headers={"Authorization": f"Bearer {self._api_token}"},
            timeout=httpx.Timeout(10.0, connect=5.0),
        )
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Release the underlying `httpx.AsyncClient`. Idempotent."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # --- representation -----------------------------------------------------

    def __repr__(self) -> str:
        return f"CloudflareProvider(zone_domain={self._zone_domain!r})"

    # --- helpers ------------------------------------------------------------

    def _require_client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise ConfigError(
                "CloudflareProvider must be used as an async context manager "
                "(`async with CloudflareProvider(...)`) or have aclose() awaited"
            )
        return self._client

    async def _resolve_zone_id(self) -> str:
        """Resolve `self._zone_domain` to its Cloudflare zone ID, with caching."""
        if self._zone_id is not None:
            return self._zone_id
        client = self._require_client()
        try:
            response = await client.get(
                "/zones",
                params={"name": self._zone_domain},
            )
        except httpx.HTTPError as exc:
            raise OutoError(
                f"network error while resolving Cloudflare zone {self._zone_domain!r}: {exc}",
                code="dns_upstream",
            ) from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise OutoError(
                f"invalid JSON from Cloudflare while resolving zone: {exc}",
                code="dns_upstream",
            ) from exc
        if response.status_code >= 400:
            raise self._map_response_error(response.status_code, payload)
        zones = payload.get("result", [])
        if not zones:
            raise ConfigError(f"no Cloudflare zone found for domain {self._zone_domain!r}")
        zone_id = zones[0].get("id")
        if not isinstance(zone_id, str) or not zone_id:
            raise ConfigError(
                f"Cloudflare zone lookup returned a malformed payload for {self._zone_domain!r}"
            )
        self._zone_id = zone_id
        return zone_id

    @staticmethod
    def _map_response_error(status_code: int, payload: Any) -> ConfigError | OutoError:
        """Map a Cloudflare response onto our error hierarchy."""
        if 400 <= status_code < 500:
            return ConfigError(_format_cloudflare_message(payload, status_code))
        return OutoError(
            _format_cloudflare_message(payload, status_code),
            code="dns_upstream",
        )

    async def _request(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        """Issue a request and return its JSON body.

        Raises:
            ConfigError: For 4xx Cloudflare responses (auth, validation,
                not-found). Surfaces the API message to the operator.
            OutoError: For network errors and 5xx Cloudflare responses. Marked
                `code="dns_upstream"` so the wizard can retry.
        """
        client = self._require_client()
        try:
            response = await client.request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            raise OutoError(
                f"network error calling Cloudflare {method} {url}: {exc}",
                code="dns_upstream",
            ) from exc
        try:
            payload: dict[str, Any] = response.json()
        except ValueError as exc:
            raise OutoError(
                f"invalid JSON from Cloudflare {method} {url}: {exc}",
                code="dns_upstream",
            ) from exc
        if response.status_code >= 400:
            raise self._map_response_error(response.status_code, payload)
        return payload

    # --- DNSProvider API ----------------------------------------------------

    async def ensure_record(self, record: DnsRecord) -> None:
        """Create the record, or update it in place if it already exists."""
        zone_id = await self._resolve_zone_id()
        existing = await self._find_existing_record(zone_id, record)
        if existing is None:
            await self._create_record(zone_id, record)
            return
        record_id = existing["id"]
        await self._update_record(zone_id, record_id, record)

    async def delete_record(self, record: DnsRecord) -> None:
        """Delete the record, silently ignoring the not-found case."""
        zone_id = await self._resolve_zone_id()
        existing = await self._find_existing_record(zone_id, record)
        if existing is None:
            return
        record_id = existing["id"]
        await self._delete_record_by_id(zone_id, record_id)

    async def list_records(self) -> list[DnsRecord]:
        """Return every record currently in the zone."""
        zone_id = await self._resolve_zone_id()
        payload = await self._request("GET", f"/zones/{zone_id}/dns_records")
        result = payload.get("result", [])
        return [
            DnsRecord(
                name=item["name"],
                type=item["type"],
                value=item["content"],
                ttl=int(item.get("ttl", 300)),
            )
            for item in result
            if isinstance(item, dict)
        ]

    # --- Cloudflare-specific helpers ----------------------------------------

    async def _find_existing_record(self, zone_id: str, record: DnsRecord) -> dict[str, Any] | None:
        payload = await self._request(
            "GET",
            f"/zones/{zone_id}/dns_records",
            params={
                "type": record.type,
                "name": record.name,
            },
        )
        results = payload.get("result", [])
        if not results:
            return None
        first = results[0]
        if not isinstance(first, dict):
            return None
        return first

    async def _create_record(self, zone_id: str, record: DnsRecord) -> None:
        body = {
            "type": record.type,
            "name": record.name,
            "content": record.value,
            "ttl": record.ttl,
        }
        await self._request("POST", f"/zones/{zone_id}/dns_records", json=body)

    async def _update_record(self, zone_id: str, record_id: str, record: DnsRecord) -> None:
        body = {
            "type": record.type,
            "name": record.name,
            "content": record.value,
            "ttl": record.ttl,
        }
        await self._request(
            "PUT",
            f"/zones/{zone_id}/dns_records/{record_id}",
            json=body,
        )

    async def _delete_record_by_id(self, zone_id: str, record_id: str) -> None:
        await self._request(
            "DELETE",
            f"/zones/{zone_id}/dns_records/{record_id}",
        )


def _format_cloudflare_message(payload: Any, status_code: int) -> str:
    """Extract the first human-readable message from a Cloudflare error payload."""
    if not isinstance(payload, dict):
        return f"Cloudflare API error (HTTP {status_code})"
    errors = payload.get("errors")
    if isinstance(errors, list) and errors:
        first = errors[0]
        if isinstance(first, dict):
            message = first.get("message")
            if isinstance(message, str) and message:
                return _sanitize_message(f"Cloudflare API error: {message}")
    message = payload.get("message")
    if isinstance(message, str) and message:
        return _sanitize_message(f"Cloudflare API error: {message}")
    return f"Cloudflare API error (HTTP {status_code})"
