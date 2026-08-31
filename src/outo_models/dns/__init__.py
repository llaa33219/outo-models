"""DNS provider abstraction.

The setup wizard (WP-14) and TLS manager (WP-6) talk to upstream DNS through
the `DNSProvider` abstract interface. The factory wires the operator's chosen
provider onto a concrete subclass.
"""

from outo_models.dns.base import DNSProvider, DnsRecord
from outo_models.dns.cloudflare import CloudflareProvider
from outo_models.dns.factory import create_provider
from outo_models.dns.manual import ManualProvider

__all__ = [
    "CloudflareProvider",
    "DNSProvider",
    "DnsRecord",
    "ManualProvider",
    "create_provider",
]
