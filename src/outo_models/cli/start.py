"""`outo-models start` — `podman run` the outo-models container.

The wizard writes `/etc/outo-models/config.yaml` on first run, and `start`
reads it back so it knows which image tag, volume name, and ports to use.
If the config file is missing the command refuses to run — the operator
must `setup` first. The container runs unprivileged (see AGENTS.md §2.3):
firewall / DNS / TLS work is already done by the setup wizard, and this
command does NOT touch any host-privileged tooling.

When `podman` is not on `PATH` (the development machine), the command
prints a clear Korean message telling the operator this command runs on
the server host. This is *not* an error per se — the development machine
has no server to manage — but it exits non-zero so a CI script wrapping
the CLI notices.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml

from outo_models.cli import (
    emit_korean,
    podman_available,
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
            f"설정 파일이 없습니다 ({path}). 먼저 'outo-models setup'을 실행해 주세요."
        )
    with path.open("r", encoding="utf-8") as fh:
        payload = yaml.safe_load(fh) or {}
    if not isinstance(payload, dict):
        raise ConfigError(f"설정 파일 형식이 잘못되었습니다 ({path}): 최상위가 매핑이 아님")
    for key in _REQUIRED_KEYS:
        if key not in payload:
            raise ConfigError(f"설정 파일에 '{key}' 키가 없습니다 ({path})")
    return payload


def start() -> None:
    """`outo-models start` — 컨테이너를 시작합니다."""
    if not podman_available():
        render_error(
            ConfigError(
                "이 명령은 서버 호스트에서 실행되어야 합니다 (podman 미설치). "
                "컨테이너 배포 환경의 호스트에서 다시 실행해 주세요."
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
            ConfigError(f"설정 파일의 'ports'는 리스트여야 합니다 (got {type(ports_raw).__name__})")
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
        emit_korean(f"[오류] 컨테이너 시작 실패 (exit={rc})")
        raise typer_exit(1)
    emit_korean(f"컨테이너 시작 완료: {image}")


__all__ = ["start"]
