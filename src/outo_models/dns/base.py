"""DNS provider abstraction shared by every concrete provider.

`DnsRecord` is the wire-level description of a single resource record. It is
frozen so callers cannot mutate it after a provider has inspected it, and it
holds no provider-specific fields — Cloudflare record IDs, Route53 hosted
zone IDs, etc. live on the concrete provider.

`DNSProvider` is the async interface the setup wizard (WP-14) and the TLS
manager (WP-6) program against. Adding a new provider means subclassing it
and implementing the three async methods; the factory then wires it in.

Both the abstract methods and the `name` class attribute are non-negotiable —
the setup wizard renders `dns_provider` configuration by matching on `name`,
and the factory uses the same identifier as the dispatch key.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class DnsRecord:
    """A single DNS resource record.

    Attributes:
        name: Fully-qualified domain name or relative name (e.g. `_acme-challenge`).
            The concrete provider is responsible for resolving it against its
            zone model.
        type: One of `"A"`, `"AAAA"`, `"CNAME"`, `"TXT"`.
        value: The record value — an IPv4 / IPv6 literal, a target hostname, or
            a free-form text payload for TXT records.
        ttl: Time-to-live in seconds. Defaults to 300 to keep DNS validation
            round-trips snappy during ACME issuance.
    """

    name: str
    type: str
    value: str
    ttl: int = 300


class DNSProvider(ABC):
    """Abstract async DNS provider.

    Every concrete provider MUST set the `name` class attribute (used by the
    factory and by `settings.dns_provider`); MUST be safe to drop into the
    factory without further configuration beyond `(zone_domain, credentials)`;
    and MUST treat `ensure_record` / `delete_record` as idempotent — calling
    them with a record that already matches desired state is a no-op, not an
    error. The setup wizard relies on idempotency to retry safely.
    """

    name: str

    @abstractmethod
    async def ensure_record(self, record: DnsRecord) -> None:
        """Create `record` if absent, update it in place if it already exists."""
        raise NotImplementedError

    @abstractmethod
    async def delete_record(self, record: DnsRecord) -> None:
        """Remove `record`. Missing records are silently ignored."""
        raise NotImplementedError

    @abstractmethod
    async def list_records(self) -> list[DnsRecord]:
        """Return every record the provider currently manages."""
        raise NotImplementedError
