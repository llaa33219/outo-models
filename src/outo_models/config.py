"""Runtime configuration for outo-models.

All settings flow through `Settings` (a pydantic-settings `BaseSettings`)
which reads environment variables prefixed with `OUTO_`. The process-wide
singleton is `get_settings()` — wrap it in `lru_cache` so callers can read it
from anywhere without paying construction cost, and clear the cache from
tests when they override `OUTO_*` variables.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic_settings import BaseSettings, SettingsConfigDict

from outo_models.exceptions import ConfigError

# Minimum acceptable `secret_key` length in production environments.
# 32 ASCII characters ≈ 256 bits — comfortably above OWASP's minimum.
_MIN_PRODUCTION_SECRET_LEN = 32


class Settings(BaseSettings):
    """Strongly-typed runtime configuration.

    Field defaults are the development defaults; production deployments
    MUST override `data_dir`, `domain`, `secret_key`, and `env` via the
    `OUTO_*` environment variables.
    """

    model_config = SettingsConfigDict(env_prefix="OUTO_")

    data_dir: Path = Path("/var/lib/outo-models")
    domain: str = "localhost"
    db_url: str | None = None
    secret_key: str = ""
    env: Literal["development", "production"] = "development"
    require_approval: bool = True
    default_quota_bytes: int = 10 * 1024**3

    # Git LFS object storage. "local" stores objects under
    # `data_dir/lfs/`; "s3" presigns uploads/downloads against an
    # S3-compatible endpoint (MinIO, AWS S3, ...).
    lfs_backend: Literal["local", "s3"] = "local"
    lfs_max_object_bytes: int = 5 * 1024**3
    s3_endpoint: str = ""  # e.g. "https://s3.ap-northeast-2.amazonaws.com" or MinIO URL
    s3_bucket: str = ""
    s3_region: str = "us-east-1"
    s3_access_key: str = ""
    s3_secret_key: str = ""
    s3_prefix: str = "lfs"
    s3_presign_ttl_seconds: int = 3600

    # Spaces runtime (v2). Disabled by default; requires a reachable
    # Podman API socket (e.g. /run/podman/podman.sock mounted into the
    # container, or the host user socket when running uncontainerized).
    spaces_runtime_enabled: bool = False
    podman_socket: str = "/run/podman/podman.sock"
    spaces_runtime_port_range_start: int = 20000
    spaces_runtime_port_range_end: int = 21000

    @property
    def is_internal(self) -> bool:
        """True when `domain` is empty OR parses as an IP address.

        "Internal mode" means the server is reachable only over a private
        network (LAN / VPN / loopback) on plain HTTP — no ACME, no
        TLS termination, no DNS record. The wizard uses this flag to skip
        the domain / DNS / ACME prompts; the security-headers middleware
        uses it to suppress HSTS; the Caddy manager uses it to drop the
        TLS blocks from the rendered Caddyfile.

        An empty `domain` is also "internal" because the operator may have
        cleared the field during `setup`; in that case the wizard's
        collect phase sets the address from `--public-ipv4` or LAN
        detection before the config ever lands on disk.

        `is_ip_address` is imported lazily to break the
        `utils.__init__` → `utils.git_url` → `config` circular import a
        top-level import would create.
        """
        domain = (self.domain or "").strip()
        if not domain:
            return True
        from outo_models.utils.net import is_ip_address

        return is_ip_address(domain)

    @property
    def resolved_db_url(self) -> str:
        """Explicit `db_url` if set, otherwise a derived SQLite URL under `data_dir`."""
        if self.db_url is not None:
            return self.db_url
        return f"sqlite+aiosqlite:///{self.data_dir.as_posix()}/db.sqlite3"

    @property
    def base_url(self) -> str:
        """`http://` for internal mode (any IP), `https://` for real hostnames."""
        scheme = "http" if self.is_internal else "https"
        return f"{scheme}://{self.domain}"

    def validate_for_production(self) -> None:
        """Enforce settings that are mandatory in production.

        Raises:
            ConfigError: when `env == "production"` but `secret_key` is empty
                or shorter than the minimum acceptable length.
        """
        if self.env != "production":
            return
        if not self.secret_key or len(self.secret_key) < _MIN_PRODUCTION_SECRET_LEN:
            raise ConfigError(
                f"OUTO_SECRET_KEY must be set and at least "
                f"{_MIN_PRODUCTION_SECRET_LEN} characters in production"
            )


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide `Settings`, constructed on first access.

    Sources, highest priority first: `OUTO_*` environment variables, then
    the YAML config file at `OUTO_CONFIG` (default
    `/etc/outo-models/config.yaml`, written by the setup wizard), then field
    defaults. YAML keys that are not `Settings` fields (e.g. `image`,
    `volume`, `ports` — consumed by `start` directly) are ignored here.
    """
    data: dict[str, Any] = {}
    config_path = Path(os.environ.get("OUTO_CONFIG", "/etc/outo-models/config.yaml"))
    if config_path.is_file():
        try:
            raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise ConfigError(f"failed to parse {config_path}: {exc}") from exc
        if raw is None:
            raw = {}
        if not isinstance(raw, dict):
            raise ConfigError(
                f"{config_path} must contain a YAML mapping, got {type(raw).__name__}"
            )
        data = {k: v for k, v in raw.items() if k in Settings.model_fields}
        for key in list(data):
            if f"OUTO_{key.upper()}" in os.environ:
                del data[key]
    return Settings(**data)
