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
provisioning tool uses; both surface the same final message.

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
    help="First-run interactive setup wizard",
    no_args_is_help=True,
    add_completion=False,
    rich_markup_mode="rich",
)


@setup_app.callback()
def _setup_callback() -> None:
    """`outo-models setup` — first-install wizard."""


@setup_app.command("run")
def setup_run(
    non_interactive: bool = typer.Option(
        False,
        "--non-interactive",
        help="Disable interactive prompts; use only flags and environment variables.",
    ),
    domain: str | None = typer.Option(None, "--domain", help="Server domain"),
    acme_email: str | None = typer.Option(
        None, "--acme-email", help="ACME (Let's Encrypt) account email"
    ),
    dns_provider: str | None = typer.Option(
        None,
        "--dns-provider",
        help="DNS provider (cloudflare | manual)",
    ),
    public_ipv4: str | None = typer.Option(
        None, "--public-ipv4", help="Server public IPv4 address"
    ),
    admin_username: str | None = typer.Option(
        None, "--admin-username", help="Admin account username"
    ),
    admin_email: str | None = typer.Option(None, "--admin-email", help="Admin account email"),
    admin_password: str | None = typer.Option(
        None,
        "--admin-password",
        help="Admin password (non-interactive mode only; minimum 8 characters)",
    ),
    skip_dns: bool = typer.Option(False, "--skip-dns", help="Skip the DNS record creation step"),
    skip_firewall: bool = typer.Option(
        False, "--skip-firewall", help="Skip the firewall port opening step"
    ),
    skip_ip_detect: bool = typer.Option(
        False,
        "--skip-ip-detect",
        help="Skip automatic IPv4 detection and only accept manual input.",
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        help="Accept defaults automatically (only for safe steps).",
    ),
    ports: str | None = typer.Option(
        None,
        "--ports",
        help="Comma-separated list of ports (default: 80,443).",
    ),
    require_approval: bool | None = typer.Option(
        None,
        "--require-approval/--no-require-approval",
        help="Whether new signups require admin approval.",
    ),
) -> None:
    """Run the setup wizard."""
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

    # (1) Write the YAML config (mode 0o600; secrets included; warn operator).
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

    # (6) Final next-steps message.
    _effect.print_next_steps(config_path, caddyfile)


__all__ = ["SetupAnswers", "setup_app", "setup_run"]


# Re-export so external callers (and tests) can construct a `SetupAnswers`
# without reaching into the private `_collect` module.
SetupAnswers = _collect.SetupAnswers
