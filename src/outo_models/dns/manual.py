"""Manual DNS provider for operators who manage DNS outside the application.

`ensure_record` is a no-op against the upstream — the operator must create the
records themselves at their DNS host. The provider remembers every record the
setup wizard asked for, and `instructions()` returns an English-language cheat
sheet telling the operator exactly which records to create. `mark_confirmed()`
clears the pending list once the operator has confirmed propagation.

The provider deliberately does NOT perform any DNS resolution / lookup — it
has no upstream. Its job is to be a faithful "desired state" recorder that
the setup wizard can poll for completion.
"""

from __future__ import annotations

from outo_models.dns.base import DNSProvider, DnsRecord


class ManualProvider(DNSProvider):
    """No-op DNS provider that tracks desired records in memory.

    Args:
        zone_domain: The zone domain the operator will edit by hand.
    """

    name = "manual"

    def __init__(self, zone_domain: str) -> None:
        self._zone_domain = zone_domain
        self._pending: dict[tuple[str, str], DnsRecord] = {}

    # --- representation -----------------------------------------------------

    def __repr__(self) -> str:
        return f"ManualProvider(zone_domain={self._zone_domain!r})"

    # --- DNSProvider API ----------------------------------------------------

    async def ensure_record(self, record: DnsRecord) -> None:
        """Record `record` as desired. Re-calling with the same key is idempotent."""
        self._pending[(record.name, record.type)] = record

    async def delete_record(self, record: DnsRecord) -> None:
        """Drop `record` from the pending set if present; otherwise a no-op."""
        self._pending.pop((record.name, record.type), None)

    async def list_records(self) -> list[DnsRecord]:
        """Return every record the operator still needs to create."""
        return list(self._pending.values())

    # --- Manual-only API ----------------------------------------------------

    def mark_confirmed(self) -> None:
        """Clear the pending set after the operator confirms propagation."""
        self._pending.clear()

    def instructions(self) -> str:
        """English-language instructions telling the operator which records to add.

        The wizard prints this verbatim when the operator picks the `manual`
        DNS provider. Returns an empty string when no records are pending.
        """
        if not self._pending:
            return ""
        lines: list[str] = [
            f"Please add the following DNS records at your DNS host for {self._zone_domain}:",
            "",
        ]
        for idx, record in enumerate(self._pending.values(), start=1):
            lines.append(
                f"{idx}. Name: {record.name}  "
                f"Type: {record.type}  "
                f"Value: {record.value}  "
                f"TTL: {record.ttl}s"
            )
        lines.append("")
        lines.append(
            "Once you have confirmed the records have propagated, "
            "please press 'Confirm' in the setup wizard."
        )
        return "\n".join(lines)
