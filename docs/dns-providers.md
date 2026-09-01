# DNS providers

The `outo_models.dns` package defines the asynchronous interface
(`DNSProvider` ABC) shared by the setup wizard and Caddy. Concrete
implementations (Cloudflare, manual) implement this interface and are
dispatched from [factory.create_provider](../src/outo_models/dns/factory.py).
The four-step procedure for adding a new provider is documented in
[src/outo_models/dns/README.md](../src/outo_models/dns/README.md).

This page covers the operator-facing decisions: which provider to pick, how
to mint a token, and how the manual mode works.

## Interface

```python
@dataclass(frozen=True, slots=True)
class DnsRecord:
    name: str         # e.g. models.example.com
    type: str         # "A" | "AAAA" | "CNAME" | "TXT"
    value: str        # IPv4 / IPv6 / target / text
    ttl: int = 300    # 5-minute default — keeps the ACME verification round-trip short

class DNSProvider(ABC):
    name: str         # factory dispatch key

    async def ensure_record(self, record: DnsRecord) -> None
    async def delete_record(self, record: DnsRecord) -> None
    async def list_records(self) -> list[DnsRecord]
```

`ensure_record` is idempotent — it updates the existing record if one
already exists, otherwise creates it. The wizard is safe to re-run.

## Error mapping

| Situation | Exception |
| --- | --- |
| Unknown provider name | `ConfigError` (`config_error`) |
| Provider-specific credentials missing | `ConfigError` |
| Provider 4xx response (auth / validation / not-found) | `ConfigError` with the provider's message |
| Provider 5xx / network error | `OutoError(code="dns_upstream")` |

`dns_upstream` is treated as transient — the wizard surfaces a retry
hint.

## Cloudflare mode

This is the recommended mode for most operators. For domains delegated to
Cloudflare DNS, it creates the `A` record automatically and uses the same
token for DNS-01 ACME challenges.

### API token issuance

1. Visit <https://dash.cloudflare.com/profile/api-tokens> and click
   **Create Token**
2. Choose the template **Edit zone DNS** (or a **Custom token**)
3. Permissions:
   - `Zone` → `DNS` → `Edit`
   - `Zone Resources` → `Include` → `Specific zone` → `<your-zone>`
     (e.g. `example.com`)
4. **Continue to summary** → **Create Token**
5. Paste the generated token into the wizard (or inject it via
   `OUTO_CLOUDFLARE_API_TOKEN`)

> The token only needs the `Zone.DNS:Edit` permission. Do not grant
> broader permissions (Zone Read, Account Read, etc.).

### Wizard behavior

Running the wizard with `--dns-provider cloudflare` performs the
following automatically:

1. Calls Cloudflare API `GET /zones?name=<domain>` to cache the
   `zone_id`
2. Calls `POST /zones/{zone_id}/dns_records` (or `PUT` if it already
   exists) to ensure the record
   `{ name: <domain>, type: A, content: <ipv4>, ttl: 300 }`
3. The wizard continues → Caddy uses the same token for the DNS-01 ACME
   challenge

If the API response body ever contains the token, the wizard applies
`re.sub(r"[A-Za-z0-9_-]{32,}", "***", ...)` to mask it before composing
the message.

### Masking

`CloudflareProvider.__repr__` shows only the zone domain (the `api_token`
is never included). Plaintext never leaks into logs, exception messages,
or the DB.

## Manual mode

For DNS hosts other than Cloudflare (Route53, GoDaddy, etc.), or for
environments where the wizard must not touch DNS at all.

### Wizard behavior

Running with `--dns-provider manual` performs the following:

1. `ManualProvider._pending` (in-memory dict) stores
   `{name, type, value, ttl}`
2. `ManualProvider.instructions()` prints operator instructions to stdout
3. The operator follows the instructions to create the record on their
   DNS host
4. The wizard waits for confirmation via
   `prompts.confirm("DNS record has propagated — press Enter to continue.", default=True)`

Example instruction output:

```
Add the following DNS record to the DNS host for example.com:

1. name: models.example.com  type: A  value: 203.0.113.10  TTL: 300s

After confirming the record has propagated, press 'confirm' in the setup wizard.
```

After propagation (e.g. `dig +short models.example.com @1.1.1.1`) the
wizard can proceed. The in-memory dict goes away with the wizard
instance, so re-running prints the instructions again.

### Handling DNS-01 manually

In manual mode the Cloudflare plugin is unavailable, so Caddy uses the
HTTP-01 challenge. That means port 80 must be reachable from the public
internet at ACME issuance time (typical for production deployments). If
you need wildcard certificates or private-domain certificates, see the
ACME section of [troubleshooting.md](troubleshooting.md).

## Adding a new provider (4 steps)

The formal procedure is documented in
[src/outo_models/dns/README.md](../src/outo_models/dns/README.md). Summary:

1. **Implement the ABC**: add a `DNSProvider` subclass under
   `src/outo_models/dns/<name>.py`. Manage httpx / boto3 etc. via
   `__aenter__` / `__aexit__`.
2. **Error mapping**: 4xx → `ConfigError`, 5xx / network →
   `OutoError(code="dns_upstream")`. Never embed credentials in exception
   messages.
3. **Register in the factory**: add a new branch in
   `dns/factory.create_provider`. Missing credentials must immediately
   raise `ConfigError`.
4. **Export / test**: export from `dns/__init__.py`. Add success /
   update / error / secret-hygiene tests to
   `tests/unit/test_dns_<name>.py`, plus a dispatch case in
   `tests/unit/test_dns_factory.py`.

The wizard and TLS manager only depend on the `DNSProvider` interface, so
adding a new provider requires no other module changes.

## Operational checklist

To change the DNS mode:

1. Update the `dns_provider` key in `/etc/outo-models/config.yaml`
2. (When switching to Cloudflare) add the Cloudflare API token under
   `cloudflare_api_token`
3. Re-run `outo-models setup` (idempotent) — the DNS / Caddyfile steps
   regenerate with the new settings
4. Run `outo-models restart` — the new Caddyfile takes effect

## Next steps

- [setup-wizard.md](setup-wizard.md) — prompt order of the DNS step
- [security.md](security.md) — secret hygiene for DNS tokens
- [troubleshooting.md](troubleshooting.md) — debugging DNS propagation /
  certificate issuance
