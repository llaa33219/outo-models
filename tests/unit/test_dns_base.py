"""Tests for `outo_models.dns.base`.

These tests pin the abstract contract every concrete provider must honor:
`DNSProvider` cannot be instantiated directly, and a subclass that fails to
implement one of the abstract async methods is refused at instantiation time.
`DnsRecord` is a frozen dataclass with the exact field shape the WP-14 setup
wizard and WP-6 TLS manager code against.
"""

from __future__ import annotations

import dataclasses

import pytest

from outo_models.dns.base import DNSProvider, DnsRecord


class TestDnsRecord:
    """`DnsRecord` is a frozen dataclass with the documented field shape."""

    def test_construction_with_required_fields_only(self) -> None:
        rec = DnsRecord(name="models.example.com", type="A", value="1.2.3.4")
        assert rec.name == "models.example.com"
        assert rec.type == "A"
        assert rec.value == "1.2.3.4"
        assert rec.ttl == 300  # default

    def test_construction_with_explicit_ttl(self) -> None:
        rec = DnsRecord(name="models.example.com", type="A", value="1.2.3.4", ttl=60)
        assert rec.ttl == 60

    def test_is_frozen(self) -> None:
        rec = DnsRecord(name="x.example.com", type="TXT", value="v=spf1")
        with pytest.raises(dataclasses.FrozenInstanceError):
            rec.ttl = 600  # type: ignore[misc]

    def test_supports_all_documented_record_types(self) -> None:
        for rtype in ("A", "AAAA", "CNAME", "TXT"):
            rec = DnsRecord(name="x", type=rtype, value="y")
            assert rec.type == rtype


class TestDNSProviderABC:
    """`DNSProvider` is abstract and refuses partial implementations."""

    def test_cannot_instantiate_abc_directly(self) -> None:
        with pytest.raises(TypeError):
            DNSProvider()  # type: ignore[abstract]

    def test_subclass_missing_ensure_record_cannot_instantiate(self) -> None:
        class Incomplete(DNSProvider):
            name = "incomplete"

            async def delete_record(self, record: DnsRecord) -> None:
                return None

            async def list_records(self) -> list[DnsRecord]:
                return []

        with pytest.raises(TypeError):
            Incomplete()  # type: ignore[abstract]

    def test_subclass_missing_delete_record_cannot_instantiate(self) -> None:
        class Incomplete(DNSProvider):
            name = "incomplete"

            async def ensure_record(self, record: DnsRecord) -> None:
                return None

            async def list_records(self) -> list[DnsRecord]:
                return []

        with pytest.raises(TypeError):
            Incomplete()  # type: ignore[abstract]

    def test_subclass_missing_list_records_cannot_instantiate(self) -> None:
        class Incomplete(DNSProvider):
            name = "incomplete"

            async def ensure_record(self, record: DnsRecord) -> None:
                return None

            async def delete_record(self, record: DnsRecord) -> None:
                return None

        with pytest.raises(TypeError):
            Incomplete()  # type: ignore[abstract]

    def test_full_subclass_instantiates(self) -> None:
        class FakeProvider(DNSProvider):
            name = "fake"

            async def ensure_record(self, record: DnsRecord) -> None:
                return None

            async def delete_record(self, record: DnsRecord) -> None:
                return None

            async def list_records(self) -> list[DnsRecord]:
                return []

        provider = FakeProvider()
        assert provider.name == "fake"
