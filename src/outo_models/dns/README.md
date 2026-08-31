# DNS provider abstraction

`outo_models.dns` defines the async interface the setup wizard and TLS
manager talk to when they need to create, update, or delete a DNS record.
Concrete providers (Cloudflare, manual, …) implement that interface; the
factory wires them up from the operator's configuration.

## Public API

| Symbol | Where | Purpose |
| --- | --- | --- |
| `DnsRecord` | `outo_models.dns.base` | Frozen dataclass describing one record (`name`, `type`, `value`, `ttl`). |
| `DNSProvider` | `outo_models.dns.base` | Async ABC with `ensure_record`, `delete_record`, `list_records`. |
| `CloudflareProvider` | `outo_models.dns.cloudflare` | Implementation backed by the Cloudflare REST API. |
| `ManualProvider` | `outo_models.dns.manual` | In-memory tracker that prints operator instructions. |
| `create_provider(provider, credentials, domain)` | `outo_models.dns.factory` | Dispatch table — returns a concrete `DNSProvider`. |

## Error contract

| Situation | Exception |
| --- | --- |
| Unknown provider name | `ConfigError` |
| Provider-specific credential missing | `ConfigError` |
| Provider API 4xx (validation, auth, not-found) | `ConfigError` carrying the API message |
| Provider API 5xx or network failure | `OutoError(code="dns_upstream")` |

`ConfigError.status_code == 500` (operator-fixable); `dns_upstream` is treated
as transient by the setup wizard and offers a retry.

## Adding a new provider (4 steps)

1. **Implement `DNSProvider`.** Drop a new file under `src/outo_models/dns/`,
   subclass `DNSProvider`, and implement the three abstract methods.
   Set the `name` class attribute to the identifier callers will use in
   configuration (`"route53"`, `"gandi"`, …). Use `__aenter__` / `__aexit__`
   if you need a long-lived client (httpx, boto3, …).

   ```python
   from outo_models.dns.base import DNSProvider, DnsRecord


   class Route53Provider(DNSProvider):
       name = "route53"

       async def ensure_record(self, record: DnsRecord) -> None: ...
       async def delete_record(self, record: DnsRecord) -> None: ...
       async def list_records(self) -> list[DnsRecord]: ...
   ```

2. **Map error responses onto the contract.** 4xx → `ConfigError` with the
   upstream message. Network errors and 5xx → `OutoError(code="dns_upstream")`.
   Never include credentials in exception messages; never log them.

3. **Wire it into the factory.** Add a branch in
   `src/outo_models/dns/factory.create_provider`. Validate that
   `credentials` contains everything the provider needs before construction;
   missing credentials are a `ConfigError`.

4. **Re-export and test.** Export the new class from
   `src/outo_models/dns/__init__.py`. Add a `tests/unit/test_dns_<name>.py`
   covering: success path, update-in-place, error mapping, secrets hygiene
   (no token in `repr` / exceptions), and — for HTTP-backed providers — zone
   resolution caching. The factory test in `test_dns_factory.py` should be
   extended with a dispatch case for the new name.

The setup wizard (WP-14) and TLS manager (WP-6) code against `DNSProvider`
only; once the four steps land, no other module in the codebase needs to
change to support a new upstream.
