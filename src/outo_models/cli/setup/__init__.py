"""`outo-models setup` — interactive (and non-interactive) first-run wizard.

The wizard is the single entry point for a brand-new install. It writes
`/etc/outo-models/config.yaml`, opens host firewall ports via the
host-side script, creates the DNS A record through the operator's chosen
provider, writes the admin user directly into the DB, and prints the
rendered Caddyfile location. Every step is idempotent so the operator can
re-run the wizard to fix a typo or rotate credentials.

Two surfaces:
    * Default → fully interactive via `cli.prompts` (rich-backed).
    * `--non-interactive` → all values come from flags / `OUTO_*` env
      vars; the wizard refuses to prompt.

The interactive path is what a human operator gets on the server host.
The non-interactive path is what `container/scripts/*.sh` and any
provisioning tool uses; both surface the same Korean final message.

The admin password is never logged, never echoed, never written to the
config YAML in plaintext — only the argon2id hash reaches disk, via
`outo_models.auth.passwords.hash_password`. The literal password lives in
memory only for the duration of the wizard run.

This `__init__.py` is the *only* file in the package that the parent
`outo_models.cli.main` imports. It builds the Typer `setup_app`,
declares the `run` command, and wires `setup._collect` /
`setup._effect` together.
"""
from __future__ import annotations

import asyncio

import typer

from outo_models.cli import render_error, typer_exit
from outo_models.cli.setup import _collect, _effect
from outo_models.exceptions import OutoError

setup_app = typer.Typer(
    name="setup",
    help="최초 대화형 설정 마법사",
    no_args_is_help=True,
    add_completion=False,
    rich_markup_mode="rich",
)


@setup_app.callback()
def _setup_callback() -> None:
    """`outo-models setup` — 최초 설치 마법사."""


@setup_app.command("run")
def setup_run(
    non_interactive: bool = typer.Option(
        False,
        "--non-interactive",
        help="대화형 프롬프트를 비활성화하고 플래그 / 환경변수만 사용합니다.",
    ),
    domain: str | None = typer.Option(None, "--domain", help="서버 도메인"),
    acme_email: str | None = typer.Option(
        None, "--acme-email", help="ACME (Let's Encrypt) 계정 이메일"
    ),
    dns_provider: str | None = typer.Option(
        None,
        "--dns-provider",
        help="DNS 제공자 (cloudflare | manual)",
    ),
    public_ipv4: str | None = typer.Option(None, "--public-ipv4", help="서버 공개 IPv4 주소"),
    admin_username: str | None = typer.Option(None, "--admin-username", help="관리자 계정 이름"),
    admin_email: str | None = typer.Option(None, "--admin-email", help="관리자 계정 이메일"),
    admin_password: str | None = typer.Option(
        None,
        "--admin-password",
        help="관리자 비밀번호 (비대화형 모드 전용; 8자 이상)",
    ),
    skip_dns: bool = typer.Option(False, "--skip-dns", help="DNS 레코드 생성 단계 건너뜀"),
    skip_firewall: bool = typer.Option(
        False, "--skip-firewall", help="방화벽 포트 개방 단계 건너뜀"
    ),
    skip_ip_detect: bool = typer.Option(
        False,
        "--skip-ip-detect",
        help="자동 IPv4 감지를 건너뛰고 수동 입력만 받습니다.",
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        help="기본값을 자동으로 수락 (안전한 단계에 한함).",
    ),
    ports: str | None = typer.Option(
        None,
        "--ports",
        help="쉼표로 구분된 포트 목록 (기본: 80,443).",
    ),
    require_approval: bool | None = typer.Option(
        None,
        "--require-approval/--no-require-approval",
        help="신규 가입 시 관리자 승인 필요 여부.",
    ),
) -> None:
    """설정 마법사를 실행합니다."""
    try:
        _run_setup(
            non_interactive=non_interactive,
            domain=domain,
            acme_email=acme_email,
            dns_provider=dns_provider,
            public_ipv4=public_ipv4,
            admin_username=admin_username,
            admin_email=admin_email,
            admin_password=admin_password,
            skip_dns=skip_dns,
            skip_firewall=skip_firewall,
            skip_ip_detect=skip_ip_detect,
            yes=yes,
            ports=ports,
            require_approval=require_approval,
        )
    except OutoError as exc:
        render_error(exc)
        raise typer_exit(1) from exc


def _run_setup(
    *,
    non_interactive: bool,
    domain: str | None,
    acme_email: str | None,
    dns_provider: str | None,
    public_ipv4: str | None,
    admin_username: str | None,
    admin_email: str | None,
    admin_password: str | None,
    skip_dns: bool,
    skip_firewall: bool,
    skip_ip_detect: bool,
    yes: bool,
    ports: str | None,
    require_approval: bool | None,
) -> None:
    """Top-level wizard — split out so the integration tests can stub phases."""
    answers = _collect.collect_answers(
        non_interactive=non_interactive,
        domain=domain,
        acme_email=acme_email,
        dns_provider=dns_provider,
        public_ipv4=public_ipv4,
        admin_username=admin_username,
        admin_email=admin_email,
        admin_password=admin_password,
        skip_ip_detect=skip_ip_detect,
        yes=yes,
        ports=ports,
        require_approval=require_approval,
    )
    _effect.apply_settings_env(answers)

    # (1) Write the YAML config (mode 0o600; secrets included; warn Korean).
    config_path = _effect.resolve_config_path()
    _effect.write_config(config_path, answers)

    # (2) DNS A record (unless skipped).
    if not skip_dns:
        asyncio.run(_effect.ensure_dns_record(answers))

    # (3) Firewall ports (unless skipped).
    if not skip_firewall:
        asyncio.run(_effect.open_firewall_ports(answers.ports))

    # (4) Data dirs + migrations + admin user.
    asyncio.run(_effect.bootstrap_database(answers))

    # (5) Caddyfile rendering.
    caddyfile = _effect.render_caddyfile_setup(answers)

    # (6) Korean next-steps.
    _effect.print_next_steps(config_path, caddyfile)


__all__ = ["SetupAnswers", "setup_app", "setup_run"]


# Re-export so external callers (and tests) can construct a `SetupAnswers`
# without reaching into the private `_collect` module.
SetupAnswers = _collect.SetupAnswers
