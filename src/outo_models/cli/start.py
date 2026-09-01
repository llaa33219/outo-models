"""`outo-models start` — `podman run` the outo-models container.

The wizard writes `/etc/outo-models/config.yaml` on first run, and `start`
reads it back so it knows which image tag, volume name, and ports to use.
If the config file is missing the command refuses to run — the operator
must `setup` first. The container runs unprivileged (see AGENTS.md §2.3):
firewall / DNS / TLS work is already done by the setup wizard, and this
command does NOT touch any host-privileged tooling.

When `podman` is not on `PATH` (the development machine), the command
prints a clear message telling the operator this command runs on the
server host. This is *not* an error per se — the development machine
has no server to manage — but it exits non-zero so a CI script wrapping
the CLI notices.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml

from outo_models.cli import (
    podman_available,
    print_status,
    render_error,
    stream_subprocess,
    typer_exit,
)
from outo_models.config import get_settings
from outo_models.exceptions import ConfigError

# The default location of the YAML the wizard writes. `OUTO_CONFIG`
# overrides it for testing and for operators who vendor the file under
# `/etc/outo-models/config.yaml` (the production default).
_DEFAULT_CONFIG = Path("/etc/outo-models/config.yaml")

# What fields `start` actually consumes — the wizard writes a few more
# keys it owns (`admin_username`, `dns_provider`, ...) that `start` does
# not need. Being explicit about which keys we read prevents the command
# from accidentally adopting a config field meant for a different surface.
_KEY_IMAGE = "image"
_KEY_VOLUME = "volume"
_KEY_PORTS = "ports"
_REQUIRED_KEYS: tuple[str, ...] = (_KEY_IMAGE, _KEY_VOLUME, _KEY_PORTS)


def _config_path() -> Path:
    """Return the YAML config path, honoring `OUTO_CONFIG`."""
    override = os.environ.get("OUTO_CONFIG")
    if override:
        return Path(override)
    return _DEFAULT_CONFIG


def _load_config() -> dict[str, object]:
    """Read and validate the YAML config the wizard produced.

    Raises:
        ConfigError: when the file is missing (operator needs to run
            `setup` first) or when it is missing required keys.
    """
    path = _config_path()
    if not path.exists():
        raise ConfigError(
            f"config file not found ({path}). Run 'outo-models setup' first."
        )
    with path.open("r", encoding="utf-8") as fh:
        payload = yaml.safe_load(fh) or {}
    if not isinstance(payload, dict):
        raise ConfigError(f"config file has an invalid format ({path}): top-level is not a mapping")
    for key in _REQUIRED_KEYS:
        if key not in payload:
            raise ConfigError(f"config file is missing the '{key}' key ({path})")
    return payload


def start() -> None:
    """`outo-models start` — start the container."""
    if not podman_available():
        render_error(
            ConfigError(
                "this command must be run on the server host (podman not installed). "
                "Re-run it on the host of the deployed container."
            )
        )
        raise typer_exit(1)

    try:
        cfg = _load_config()
    except ConfigError as exc:
        render_error(exc)
        raise typer_exit(1) from exc

    image = str(cfg[_KEY_IMAGE])
    volume = str(cfg[_KEY_VOLUME])
    ports_raw = cfg[_KEY_PORTS]
    if not isinstance(ports_raw, list):
        render_error(
            ConfigError(f"config file 'ports' must be a list (got {type(ports_raw).__name__})")
        )
        raise typer_exit(1)
    ports = [str(int(p)) for p in ports_raw]

    # Mirror the settings env so the in-container process binds to the
    # same data_dir / secret_key the wizard wrote.
    settings = get_settings()
    env_args: list[str] = []
    if settings.data_dir:
        env_args.extend(["-e", f"OUTO_DATA_DIR={settings.data_dir}"])
    if settings.secret_key:
        env_args.extend(["-e", f"OUTO_SECRET_KEY={settings.secret_key}"])
    if settings.domain:
        env_args.extend(["-e", f"OUTO_DOMAIN={settings.domain}"])
    if settings.require_approval is not None:
        env_args.extend(
            ["-e", f"OUTO_REQUIRE_APPROVAL={'true' if settings.require_approval else 'false'}"]
        )
    if settings.db_url:
        env_args.extend(["-e", f"OUTO_DB_URL={settings.db_url}"])

    argv: list[str] = [
        "podman",
        "run",
        "-d",
        "--name",
        "outo-models",
        *env_args,
        "-v",
        f"{volume}:/var/lib/outo-models",
        "--cap-add",
        "NET_BIND_SERVICE",
    ]
    for port in ports:
        argv.extend(["-p", f"{port}:{port}"])
    argv.append(image)

    rc = stream_subprocess(argv)
    if rc != 0:
        print_status(f"[error] container failed to start (exit={rc})")
        raise typer_exit(1)
    print_status(f"container started: {image}")


__all__ = ["start"]
