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
                f"--non-interactive 모드에서 다음 값이 필요합니다: {', '.join(missing)}"
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
        raise ConfigError("--domain 이 필요합니다 (--non-interactive 모드).")
    default = "models.example.com" if yes else ""
    while True:
        value = prompts.text("서버 도메인을 입력하세요 (예: models.example.com):", default=default)
        try:
            return _validate_domain(value)
        except ValidationFailedError as exc:
            Console(stderr=True).print(f"[red]{exc}[/red]")


def _validate_domain(value: str) -> str:
    stripped = value.strip().lower()
    if not stripped:
        raise ValidationFailedError("도메인을 입력해야 합니다.")
    if " " in stripped or "/" in stripped:
        raise ValidationFailedError("도메인에 공백이나 슬래시를 포함할 수 없습니다.")
    return stripped


def _collect_acme_email(flag_value: str | None, non_interactive: bool, yes: bool) -> str:
    if flag_value:
        return flag_value.strip()
    if non_interactive:
        raise ConfigError("--acme-email 이 필요합니다 (--non-interactive 모드).")
    default = f"admin@{(get_settings().domain or 'example.com')}" if yes else ""
    return prompts.text("ACME (Let's Encrypt) 계정 이메일을 입력하세요:", default=default).strip()


def _collect_dns_provider(flag_value: str | None, non_interactive: bool, yes: bool) -> str:
    valid = ("cloudflare", "manual")
    if flag_value:
        if flag_value not in valid:
            raise ConfigError(f"--dns-provider 는 {valid} 중 하나여야 합니다.")
        return flag_value
    if non_interactive:
        raise ConfigError("--dns-provider 가 필요합니다 (--non-interactive 모드).")
    while True:
        value = prompts.text(
            "DNS 제공자를 선택하세요 (cloudflare / manual):", default="cloudflare" if yes else ""
        )
        if value in valid:
            return value
        Console(stderr=True).print(f"[red]{valid} 중 하나를 입력해 주세요.[/red]")


def _collect_cloudflare_token(non_interactive: bool, yes: bool) -> str:
    env_token = os.environ.get("OUTO_CLOUDFLARE_API_TOKEN", "").strip()
    if env_token:
        return env_token
    if non_interactive:
        raise ConfigError(
            "cloudflare 제공자를 사용할 때 --admin-password 처럼 토큰이 필요합니다. "
            "OUTO_CLOUDFLARE_API_TOKEN 환경변수를 설정해 주세요."
        )
    return prompts.password("Cloudflare API 토큰을 입력하세요 (Zone.DNS:Edit 권한):")


def _collect_public_ipv4(
    flag_value: str | None, non_interactive: bool, yes: bool, skip_detect: bool
) -> str:
    if flag_value:
        return flag_value.strip()
    if non_interactive:
        raise ConfigError("--public-ipv4 가 필요합니다 (--non-interactive 모드).")

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
    value = prompts.text("서버의 공개 IPv4 주소 (DNS A 레코드):", default=default)
    return value.strip()


def _collect_admin_username(flag_value: str | None, non_interactive: bool, yes: bool) -> str:
    if flag_value:
        return validate_slug(flag_value)
    if non_interactive:
        raise ConfigError("--admin-username 이 필요합니다 (--non-interactive 모드).")
    default = "admin" if yes else ""
    while True:
        value = prompts.text("관리자 계정 이름 (slug, 예: admin):", default=default)
        try:
            return validate_slug(value)
        except ValidationFailedError as exc:
            Console(stderr=True).print(f"[red]{exc}[/red]")


def _collect_admin_email(flag_value: str | None, non_interactive: bool, yes: bool) -> str:
    if flag_value:
        return flag_value.strip().lower()
    if non_interactive:
        raise ConfigError("--admin-email 이 필요합니다 (--non-interactive 모드).")
    default = ""
    while True:
        value = prompts.text("관리자 계정 이메일을 입력하세요:", default=default).strip().lower()
        if "@" in value:
            return value
        Console(stderr=True).print("[red]유효한 이메일 주소를 입력해 주세요.[/red]")


def _collect_admin_password(flag_value: str | None, non_interactive: bool, yes: bool) -> str:
    if flag_value:
        return _validate_password_strength(flag_value)
    if non_interactive:
        raise ConfigError(
            f"--admin-password 가 필요합니다 "
            f"(--non-interactive 모드, 최소 {_MIN_PASSWORD_LENGTH}자)."
        )
    while True:
        first = prompts.password("관리자 비밀번호를 입력하세요 (8자 이상):")
        try:
            _validate_password_strength(first)
        except ValidationFailedError as exc:
            Console(stderr=True).print(f"[red]{exc}[/red]")
            continue
        second = prompts.password("관리자 비밀번호를 다시 입력하세요:")
        if first != second:
            Console(stderr=True).print("[red]비밀번호가 일치하지 않습니다. 다시 시도하세요.[/red]")
            continue
        return first


def _validate_password_strength(value: str) -> str:
    if len(value) < _MIN_PASSWORD_LENGTH:
        raise ValidationFailedError(
            f"비밀번호는 최소 {_MIN_PASSWORD_LENGTH}자 이상이어야 합니다."
        )
    return value


def _collect_ports(flag_value: str | None, non_interactive: bool, yes: bool) -> list[int]:
    if flag_value:
        return _parse_ports(flag_value)
    if non_interactive:
        return list(_FIREWALL_REQUIRED_PORTS)
    default = "80,443" if yes else ""
    raw = prompts.text("외부에서 열 포트 (쉼표 구분, 기본 80,443):", default=default)
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
            raise ValidationFailedError(f"포트 '{piece}' 는 정수가 아닙니다.") from exc
        if not (1 <= port <= 65535):
            raise ValidationFailedError(f"포트 {port} 는 유효 범위 (1-65535)가 아닙니다.")
        out.append(port)
    if not out:
        raise ValidationFailedError("최소 한 개의 포트가 필요합니다.")
    return out


def _collect_require_approval(flag_value: bool | None, non_interactive: bool, yes: bool) -> bool:
    if flag_value is not None:
        return flag_value
    if non_interactive:
        return True
    return prompts.confirm("신규 가입 시 관리자 승인을 요구하시겠습니까?", default=yes or True)


__all__ = ["SetupAnswers", "collect_answers"]
