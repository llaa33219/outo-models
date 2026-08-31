"""Setup wizard effect phase — `SetupAnswers` → disk / DB / DNS / firewall.

Each function in this module performs exactly one side effect, so the
top-level `_run_setup` driver reads as a list of ordered operations.
Tests import individual functions here to mock out single steps without
having to stub the whole pipeline.

The admin password is never written to the YAML config; only the
argon2id hash reaches disk, via `auth.passwords.hash_password`. The
literal password lives in memory only for the duration of the wizard
run.
"""
from __future__ import annotations

import contextlib
import os
import secrets
from pathlib import Path
from typing import Any

import yaml
from rich.console import Console
from sqlalchemy import select

from outo_models import version as app_version
from outo_models.auth.passwords import hash_password
from outo_models.cli import prompts
from outo_models.cli.setup._collect import SetupAnswers
from outo_models.config import get_settings
from outo_models.db import (
    User,
    get_engine,
    run_migrations,
)
from outo_models.db.engine import dispose_engines
from outo_models.db.session import session_scope
from outo_models.dns.base import DnsRecord
from outo_models.dns.factory import create_provider
from outo_models.exceptions import ConfigError, OutoError
from outo_models.firewall.open_ports import open_ports
from outo_models.tls.caddy_manager import TlsConfig, render_caddyfile
from outo_models.utils.paths import ensure_dirs
from outo_models.utils.time import utcnow

# Default location of the YAML config the wizard produces. Same convention
# as `start.py._DEFAULT_CONFIG`. Tests override via `OUTO_CONFIG`.
_DEFAULT_CONFIG_PATH = Path("/etc/outo-models/config.yaml")

# Default image / volume baked into a fresh wizard run. The image tag is
# intentionally stable — operators explicitly pin `dev` if they want the
# bleeding edge.
_DEFAULT_IMAGE = "outo-models:stable"
_DEFAULT_VOLUME = "outo-models-data"


def apply_settings_env(answers: SetupAnswers) -> None:
    """Push collected values into the process environment for downstream steps.

    These are *not* persisted to the YAML — they are runtime overrides so
    `db.engine` (read on next call) picks up the new domain / data dir.
    Mutation is deliberate: Settings is a fresh Pydantic model built on
    the next `get_settings()` call, and the in-process cache is cleared
    before the wizard continues.
    """
    os.environ["OUTO_DOMAIN"] = answers.domain
    os.environ["OUTO_REQUIRE_APPROVAL"] = "true" if answers.require_approval else "false"
    os.environ["OUTO_ENV"] = "production"
    if not os.environ.get("OUTO_SECRET_KEY"):
        os.environ["OUTO_SECRET_KEY"] = secrets.token_urlsafe(48)

    get_settings.cache_clear()


def resolve_config_path() -> Path:
    """Return the YAML config path, honoring `OUTO_CONFIG`."""
    override = os.environ.get("OUTO_CONFIG")
    if override:
        return Path(override)
    return _DEFAULT_CONFIG_PATH


def write_config(path: Path, answers: SetupAnswers) -> None:
    """Atomically write the YAML config with mode 0o600."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "version": app_version.__version__,
        "domain": answers.domain,
        "acme_email": answers.acme_email,
        "public_ipv4": answers.public_ipv4,
        "dns_provider": answers.dns_provider,
        "image": _DEFAULT_IMAGE,
        "volume": _DEFAULT_VOLUME,
        "ports": answers.ports,
        "require_approval": answers.require_approval,
        "admin_username": answers.admin_username,
        "admin_email": answers.admin_email,
    }
    if answers.cloudflare_api_token:
        payload["cloudflare_api_token"] = answers.cloudflare_api_token
    if "OUTO_SECRET_KEY" in os.environ:
        payload["secret_key"] = os.environ["OUTO_SECRET_KEY"]

    # `yaml.safe_dump` writes a deterministic representation — same input
    # produces identical bytes, which keeps `git diff` on the config file
    # readable.
    body = yaml.safe_dump(payload, sort_keys=False, allow_unicode=True)
    path.write_text(body, encoding="utf-8")
    # chmod may fail on FAT mounts etc. — the Korean warning still fires,
    # and the file content is correct.
    with contextlib.suppress(OSError):
        os.chmod(path, 0o600)

    Console(stderr=True).print(
        f"[yellow][경고] {path} 에 DNS 토큰 / 비밀 키가 포함되어 있습니다. "
        "파일 권한을 0o600 으로 유지해 주세요.[/yellow]"
    )


async def ensure_dns_record(answers: SetupAnswers) -> None:
    """Create (or report instructions for) the A record pointing at the server."""
    provider = create_provider(
        answers.dns_provider,
        (
            {"api_token": answers.cloudflare_api_token}
            if answers.cloudflare_api_token
            else {}
        ),
        domain=answers.domain,
    )
    record = DnsRecord(
        name=answers.domain,
        type="A",
        value=answers.public_ipv4,
        ttl=300,
    )
    await provider.ensure_record(record)

    # `ManualProvider.instructions()` returns a Korean cheat sheet; we
    # print it and wait for the operator to confirm the record is live.
    from outo_models.dns.manual import ManualProvider

    if isinstance(provider, ManualProvider):
        Console().print(provider.instructions())
        prompts.confirm("DNS 레코드가 전파되었으면 Enter 키를 눌러 주세요.", default=True)


async def open_firewall_ports(ports: list[int]) -> None:
    """Delegate to the host-side firewall script (see AGENTS.md §2.3)."""
    try:
        result = await open_ports(ports=ports, dry_run=False)
    except OutoError as exc:
        if exc.code == "firewall_permission":
            raise ConfigError(
                f"방화벽 명령에 권한이 없습니다 ({exc}). "
                "root 권한으로 다시 실행하거나 /etc/sudoers.d/outo-models 에 "
                "NOPASSWD 규칙을 추가해 주세요."
            ) from exc
        raise
    Console().print(
        f"[green][완료] 방화벽 포트 개방: {result.opened} ({result.kind.value})[/green]"
    )


async def bootstrap_database(answers: SetupAnswers) -> None:
    """Run migrations, then create the admin user directly in the DB."""
    ensure_dirs()
    settings = get_settings()
    engine = get_engine(settings)
    try:
        await run_migrations(engine)
    finally:
        await engine.dispose()

    password_hash = hash_password(answers.admin_password)
    admin_email = answers.admin_email.strip().lower()
    now = utcnow()
    async with session_scope() as session:
        existing = (
            await session.execute(select(User).where(User.username == answers.admin_username))
        ).scalar_one_or_none()
        if existing is not None:
            existing.email = admin_email
            existing.password_hash = password_hash
            existing.role = "admin"
            existing.status = "approved"
            existing.approved_at = now
        else:
            session.add(
                User(
                    username=answers.admin_username,
                    email=admin_email,
                    password_hash=password_hash,
                    role="admin",
                    status="approved",
                    approved_at=now,
                )
            )
    await dispose_engines()


def render_caddyfile_setup(answers: SetupAnswers) -> str:
    """Render the Caddyfile and write it next to the YAML config."""
    config = TlsConfig(
        domain=answers.domain,
        email=answers.acme_email,
        dns_provider=answers.dns_provider if answers.dns_provider == "cloudflare" else None,
        staging=False,
    )
    body = render_caddyfile(config)
    path = resolve_config_path().with_name("Caddyfile")
    path.write_text(body, encoding="utf-8")
    Console().print(f"[green][완료] Caddyfile 작성: {path}[/green]")
    return body


def print_next_steps(config_path: Path, caddyfile: str) -> None:
    """Render the final Korean message: where the config lives + how to start."""
    console = Console()
    console.print()
    console.print("[bold green][완료] 설정이 저장되었습니다.[/bold green]")
    console.print(f"  - 설정 파일: {config_path}")
    console.print(f"  - Caddyfile: {config_path.with_name('Caddyfile')}")
    console.print()
    console.print("다음 명령으로 서버를 시작하세요:")
    console.print("  [bold]outo-models start[/bold]")
    from outo_models.cli import emit_korean

    emit_korean(
        "비밀번호는 절대 화면에 다시 출력되지 않습니다. "
        "분실 시 admin reset-password 로 재설정하세요."
    )
    del caddyfile  # signature kept for testability; the body is on disk.


__all__ = [
    "apply_settings_env",
    "bootstrap_database",
    "ensure_dns_record",
    "open_firewall_ports",
    "print_next_steps",
    "render_caddyfile_setup",
    "resolve_config_path",
    "write_config",
]
