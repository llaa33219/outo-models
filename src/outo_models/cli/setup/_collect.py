"""Setup wizard collection phase — prompts → `SetupAnswers`.

Each `_collect_*` function takes the flag value (or `None` if the operator
didn't pass the flag) and the `non_interactive` / `yes` modes, and returns
either the validated answer or a typed `ConfigError`. The wizard's
*effect* phase consumes the resulting `SetupAnswers`; this module is
pure collection (no file I/O, no DB writes, no DNS calls).

The interactive path goes through `cli.prompts` (rich-backed, swappable
in tests). The non-interactive path raises a `ConfigError` when a
required flag is missing, so the operator gets a clean error rather
than a hung prompt.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from rich.console import Console

from outo_models.cli import prompts
from outo_models.config import get_settings
from outo_models.exceptions import ConfigError, ValidationFailedError
from outo_models.firewall.open_ports import REQUIRED_PORTS as _FIREWALL_REQUIRED_PORTS
from outo_models.utils.slug import validate_slug

_MIN_PASSWORD_LENGTH = 8


@dataclass(frozen=True, slots=True)
class SetupAnswers:
    """The operator's collected answers; downstream steps consume this.

    The dataclass is `frozen=True, slots=True` so an accidental mutation
    in a downstream step (e.g. an async race) cannot silently corrupt
    the value the DB write used.
    """

    domain: str
    acme_email: str
    public_ipv4: str
    dns_provider: str  # "cloudflare" | "manual"
    cloudflare_api_token: str | None  # only when dns_provider == "cloudflare"
    admin_username: str
    admin_email: str
    admin_password: str
    ports: list[int]
    require_approval: bool


def collect_answers(
    *,
    non_interactive: bool,
    domain: str | None,
    acme_email: str | None,
    dns_provider: str | None,
    public_ipv4: str | None,
    admin_username: str | None,
    admin_email: str | None,
    admin_password: str | None,
    skip_ip_detect: bool,
    yes: bool,
    ports: str | None,
    require_approval: bool | None,
) -> SetupAnswers:
    """Collect the operator's answers, prompting interactively when needed.

    Each prompt is wrapped in a typed `ConfigError` so a missing flag in
    non-interactive mode surfaces as a clean error rather than a stack
    trace — same UX as the interactive path.
    """
    if non_interactive:
        required = {
            "domain": domain,
            "acme_email": acme_email,
            "dns_provider": dns_provider,
            "admin_username": admin_username,
            "admin_email": admin_email,
            "admin_password": admin_password,
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise ConfigError(
                f"--non-interactive mode requires the following values: {', '.join(missing)}"
            )

    domain_value = _collect_domain(domain, non_interactive, yes)
    acme_email_value = _collect_acme_email(acme_email, non_interactive, yes)
    dns_provider_value = _collect_dns_provider(dns_provider, non_interactive, yes)
    cloudflare_token: str | None = None
    if dns_provider_value == "cloudflare":
        cloudflare_token = _collect_cloudflare_token(non_interactive, yes)

    public_ipv4_value = _collect_public_ipv4(public_ipv4, non_interactive, yes, skip_ip_detect)
    admin_username_value = _collect_admin_username(admin_username, non_interactive, yes)
    admin_email_value = _collect_admin_email(admin_email, non_interactive, yes)
    admin_password_value = _collect_admin_password(admin_password, non_interactive, yes)

    ports_value = _collect_ports(ports, non_interactive, yes)
    require_approval_value = _collect_require_approval(require_approval, non_interactive, yes)

    return SetupAnswers(
        domain=domain_value,
        acme_email=acme_email_value,
        public_ipv4=public_ipv4_value,
        dns_provider=dns_provider_value,
        cloudflare_api_token=cloudflare_token,
        admin_username=admin_username_value,
        admin_email=admin_email_value,
        admin_password=admin_password_value,
        ports=ports_value,
        require_approval=require_approval_value,
    )


def _collect_domain(flag_value: str | None, non_interactive: bool, yes: bool) -> str:
    if flag_value:
        return _validate_domain(flag_value)
    if non_interactive:
        raise ConfigError("--domain is required (--non-interactive mode).")
    default = "models.example.com" if yes else ""
    while True:
        value = prompts.text("Enter the server domain (e.g. models.example.com):", default=default)
        try:
            return _validate_domain(value)
        except ValidationFailedError as exc:
            Console(stderr=True).print(f"[red]{exc}[/red]")


def _validate_domain(value: str) -> str:
    stripped = value.strip().lower()
    if not stripped:
        raise ValidationFailedError("A domain is required.")
    if " " in stripped or "/" in stripped:
        raise ValidationFailedError("The domain must not contain spaces or slashes.")
    return stripped


def _collect_acme_email(flag_value: str | None, non_interactive: bool, yes: bool) -> str:
    if flag_value:
        return flag_value.strip()
    if non_interactive:
        raise ConfigError("--acme-email is required (--non-interactive mode).")
    default = f"admin@{(get_settings().domain or 'example.com')}" if yes else ""
    return prompts.text("Enter the ACME (Let's Encrypt) account email:", default=default).strip()


def _collect_dns_provider(flag_value: str | None, non_interactive: bool, yes: bool) -> str:
    valid = ("cloudflare", "manual")
    if flag_value:
        if flag_value not in valid:
            raise ConfigError(f"--dns-provider must be one of {valid}.")
        return flag_value
    if non_interactive:
        raise ConfigError("--dns-provider is required (--non-interactive mode).")
    while True:
        value = prompts.text(
            "Choose a DNS provider (cloudflare / manual):", default="cloudflare" if yes else ""
        )
        if value in valid:
            return value
        Console(stderr=True).print(f"[red]Please enter one of {valid}.[/red]")


def _collect_cloudflare_token(non_interactive: bool, yes: bool) -> str:
    env_token = os.environ.get("OUTO_CLOUDFLARE_API_TOKEN", "").strip()
    if env_token:
        return env_token
    if non_interactive:
        raise ConfigError(
            "Using the cloudflare provider requires a token, just like --admin-password. "
            "Set the OUTO_CLOUDFLARE_API_TOKEN environment variable."
        )
    return prompts.password("Enter the Cloudflare API token (Zone.DNS:Edit scope):")


def _collect_public_ipv4(
    flag_value: str | None, non_interactive: bool, yes: bool, skip_detect: bool
) -> str:
    if flag_value:
        return flag_value.strip()
    if non_interactive:
        raise ConfigError("--public-ipv4 is required (--non-interactive mode).")

    detected: str | None = None
    if not skip_detect:
        try:
            import httpx

            response = httpx.get("https://api.ipify.org", timeout=5.0)
            if response.status_code == 200:
                detected = response.text.strip()
        except Exception:
            detected = None

    default = detected or ""
    value = prompts.text("Server public IPv4 address (DNS A record):", default=default)
    return value.strip()


def _collect_admin_username(flag_value: str | None, non_interactive: bool, yes: bool) -> str:
    if flag_value:
        return validate_slug(flag_value)
    if non_interactive:
        raise ConfigError("--admin-username is required (--non-interactive mode).")
    default = "admin" if yes else ""
    while True:
        value = prompts.text("Admin account username (slug, e.g. admin):", default=default)
        try:
            return validate_slug(value)
        except ValidationFailedError as exc:
            Console(stderr=True).print(f"[red]{exc}[/red]")


def _collect_admin_email(flag_value: str | None, non_interactive: bool, yes: bool) -> str:
    if flag_value:
        return flag_value.strip().lower()
    if non_interactive:
        raise ConfigError("--admin-email is required (--non-interactive mode).")
    default = ""
    while True:
        value = prompts.text("Enter the admin account email:", default=default).strip().lower()
        if "@" in value:
            return value
        Console(stderr=True).print("[red]Please enter a valid email address.[/red]")


def _collect_admin_password(flag_value: str | None, non_interactive: bool, yes: bool) -> str:
    if flag_value:
        return _validate_password_strength(flag_value)
    if non_interactive:
        raise ConfigError(
            f"--admin-password is required "
            f"(--non-interactive mode, minimum {_MIN_PASSWORD_LENGTH} characters)."
        )
    while True:
        first = prompts.password("Enter the admin password (minimum 8 characters):")
        try:
            _validate_password_strength(first)
        except ValidationFailedError as exc:
            Console(stderr=True).print(f"[red]{exc}[/red]")
            continue
        second = prompts.password("Re-enter the admin password:")
        if first != second:
            Console(stderr=True).print("[red]Passwords do not match. Please try again.[/red]")
            continue
        return first


def _validate_password_strength(value: str) -> str:
    if len(value) < _MIN_PASSWORD_LENGTH:
        raise ValidationFailedError(
            f"Password must be at least {_MIN_PASSWORD_LENGTH} characters long."
        )
    return value


def _collect_ports(flag_value: str | None, non_interactive: bool, yes: bool) -> list[int]:
    if flag_value:
        return _parse_ports(flag_value)
    if non_interactive:
        return list(_FIREWALL_REQUIRED_PORTS)
    default = "80,443" if yes else ""
    raw = prompts.text(
        "Ports to expose externally (comma-separated, default 80,443):", default=default
    )
    if not raw.strip():
        return list(_FIREWALL_REQUIRED_PORTS)
    return _parse_ports(raw)


def _parse_ports(raw: str) -> list[int]:
    out: list[int] = []
    for piece in raw.split(","):
        piece = piece.strip()
        if not piece:
            continue
        try:
            port = int(piece)
        except ValueError as exc:
            raise ValidationFailedError(f"Port '{piece}' is not an integer.") from exc
        if not (1 <= port <= 65535):
            raise ValidationFailedError(f"Port {port} is outside the valid range (1-65535).")
        out.append(port)
    if not out:
        raise ValidationFailedError("At least one port is required.")
    return out


def _collect_require_approval(flag_value: bool | None, non_interactive: bool, yes: bool) -> bool:
    if flag_value is not None:
        return flag_value
    if non_interactive:
        return True
    return prompts.confirm("Require admin approval for new signups?", default=yes or True)


__all__ = ["SetupAnswers", "collect_answers"]
