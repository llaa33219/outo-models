"""Tests for `outo_models.dns.manual`.

`ManualProvider` is the "no DNS API" fallback. It tracks desired records in
memory and surfaces operator instructions. The setup wizard depends on every
one of these behaviors — they are the operator's only feedback loop.
"""

from __future__ import annotations

import pytest

from outo_models.dns.base import DnsRecord
from outo_models.dns.manual import ManualProvider


class TestEnsureRecord:
    """`ensure_record` is a desired-state recorder, not an upstream call."""

    async def test_first_ensure_adds_to_pending(self) -> None:
        provider = ManualProvider(zone_domain="models.example.com")
        record = DnsRecord(name="models.example.com", type="A", value="1.2.3.4")
        await provider.ensure_record(record)
        assert await provider.list_records() == [record]

    async def test_re_ensure_same_record_is_idempotent(self) -> None:
        provider = ManualProvider(zone_domain="models.example.com")
        record = DnsRecord(name="models.example.com", type="A", value="1.2.3.4")
        await provider.ensure_record(record)
        await provider.ensure_record(record)
        assert len(await provider.list_records()) == 1

    async def test_re_ensure_same_key_replaces_value(self) -> None:
        provider = ManualProvider(zone_domain="models.example.com")
        await provider.ensure_record(
            DnsRecord(name="models.example.com", type="A", value="1.2.3.4")
        )
        await provider.ensure_record(
            DnsRecord(name="models.example.com", type="A", value="5.6.7.8")
        )
        records = await provider.list_records()
        assert len(records) == 1
        assert records[0].value == "5.6.7.8"

    async def test_different_names_coexist(self) -> None:
        provider = ManualProvider(zone_domain="models.example.com")
        await provider.ensure_record(
            DnsRecord(name="models.example.com", type="A", value="1.2.3.4")
        )
        await provider.ensure_record(
            DnsRecord(name="api.models.example.com", type="A", value="1.2.3.4")
        )
        assert len(await provider.list_records()) == 2

    async def test_different_types_for_same_name_coexist(self) -> None:
        provider = ManualProvider(zone_domain="models.example.com")
        await provider.ensure_record(DnsRecord(name="_acme-challenge", type="TXT", value="abc"))
        await provider.ensure_record(
            DnsRecord(name="_acme-challenge", type="CNAME", value="x.example.com")
        )
        assert len(await provider.list_records()) == 2


class TestDeleteRecord:
    """`delete_record` clears the pending entry, missing entries are silent."""

    async def test_delete_removes_record(self) -> None:
        provider = ManualProvider(zone_domain="models.example.com")
        record = DnsRecord(name="models.example.com", type="A", value="1.2.3.4")
        await provider.ensure_record(record)
        await provider.delete_record(record)
        assert await provider.list_records() == []

    async def test_delete_missing_record_is_noop(self) -> None:
        provider = ManualProvider(zone_domain="models.example.com")
        await provider.delete_record(
            DnsRecord(name="models.example.com", type="A", value="1.2.3.4")
        )
        assert await provider.list_records() == []


class TestListRecords:
    """`list_records` is the operator-visible desired state."""

    async def test_starts_empty(self) -> None:
        provider = ManualProvider(zone_domain="models.example.com")
        assert await provider.list_records() == []


class TestMarkConfirmed:
    """`mark_confirmed` clears the pending set after operator confirmation."""

    async def test_mark_confirmed_clears_pending(self) -> None:
        provider = ManualProvider(zone_domain="models.example.com")
        await provider.ensure_record(
            DnsRecord(name="models.example.com", type="A", value="1.2.3.4")
        )
        provider.mark_confirmed()
        assert await provider.list_records() == []

    async def test_mark_confirmed_is_safe_when_empty(self) -> None:
        provider = ManualProvider(zone_domain="models.example.com")
        provider.mark_confirmed()
        assert await provider.list_records() == []


class TestInstructions:
    """`instructions()` is English text the wizard prints to the operator."""

    def test_empty_when_no_pending(self) -> None:
        provider = ManualProvider(zone_domain="models.example.com")
        assert provider.instructions() == ""

    def test_contains_zone_domain(self) -> None:
        provider = ManualProvider(zone_domain="models.example.com")
        provider._pending[("a", "A")] = DnsRecord(name="a", type="A", value="1.2.3.4")
        text = provider.instructions()
        assert "models.example.com" in text

    def test_contains_record_fields(self) -> None:
        provider = ManualProvider(zone_domain="models.example.com")
        provider._pending[("models.example.com", "A")] = DnsRecord(
            name="models.example.com", type="A", value="1.2.3.4", ttl=300
        )
        text = provider.instructions()
        assert "models.example.com" in text
        assert "A" in text
        assert "1.2.3.4" in text
        assert "300" in text

    def test_contains_multiple_record_lines(self) -> None:
        provider = ManualProvider(zone_domain="models.example.com")
        provider._pending[("models.example.com", "A")] = DnsRecord(
            name="models.example.com", type="A", value="1.2.3.4"
        )
        provider._pending[("api.models.example.com", "A")] = DnsRecord(
            name="api.models.example.com", type="A", value="1.2.3.4"
        )
        text = provider.instructions()
        assert "1." in text
        assert "2." in text


class TestRepresentation:
    """`repr` does not leak anything beyond the zone domain."""

    def test_repr_includes_zone(self) -> None:
        provider = ManualProvider(zone_domain="models.example.com")
        assert "models.example.com" in repr(provider)

    def test_provider_name_is_manual(self) -> None:
        assert ManualProvider(zone_domain="x").name == "manual"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
