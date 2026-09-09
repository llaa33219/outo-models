"""Multi-server credential store at `~/.config/omc/config.json`.

The store is a single JSON file holding the PAT for every server the user
has authenticated against. The server URL (normalized — see `normalize_server_url`)
is the key, so a user can run `omc auth login --server https://a.example` and
later `omc auth login --server https://b.example` without overwriting either.

File mode is 0600 — only the owner may read or write. The store is created
with that mode on first write; an existing file with looser permissions is
tightened on every save so accidental `chmod 644` does not silently leak
the token.

Two environment variables override the file for CI / scripted use:

    * `OMC_SERVER` — picks a server URL without touching the config.
    * `OMC_TOKEN`  — supplies the bearer credential directly.

Both are read-only overrides; `omc auth login` never persists to the file
when they are set, so a CI job can never accidentally clobber a personal
credential with a one-shot bot token.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Self

from outo_models_cli.errors import AuthRequiredError, ConfigError

# File mode for the config file and its parent directory.
_DIR_MODE = 0o700
_FILE_MODE = 0o600

# Environment variables consulted before the config file. The values are
# treated as ephemeral overrides: `auth login` does NOT persist them.
_ENV_SERVER = "OMC_SERVER"
_ENV_TOKEN = "OMC_TOKEN"  # noqa: S105 — env var name, not a credential


def normalize_server_url(raw: str) -> str:
    """Canonicalize a server URL for storage and HTTP use.

    Rules:
        * Strip whitespace and a single trailing slash.
        * If the URL carries no scheme, prepend `http://`. Plain hostnames
          and bare IPs are common in self-hosted setups; defaulting to
          `http://` (instead of guessing `https`) keeps a local LAN server
          working out of the box — `https://` would reject the request
          outright because the bundled Caddy is the only TLS terminator.
        * Lowercase the scheme + host portion so `HTTPS://Example.com` and
          `https://example.com/` collapse onto the same key.

    Raises `ConfigError` for empty input — silently storing `""` as a key
    would mean every subsequent `omc auth whoami` looks up the wrong entry.
    """
    cleaned = raw.strip().rstrip("/")
    if not cleaned:
        raise ConfigError("Server URL must not be empty.")
    if "://" not in cleaned:
        cleaned = "http://" + cleaned
    scheme, _, rest = cleaned.partition("://")
    host_part, _, _ = rest.partition("/")
    return f"{scheme.lower()}://{host_part.lower()}"


def config_dir(home_dir: str | os.PathLike[str] | None = None) -> Path:
    """Return the directory that holds `config.json`.

    Honors `OMC_CONFIG_DIR` (full override), `XDG_CONFIG_HOME` (with
    `~/.config` as the fallback); the dev environment and CI tests can
    point this at a temp dir by passing `home_dir` explicitly. Passing
    `home_dir` takes priority over the env var so `pytest -q` can run in
    parallel without leaking into the host config.
    """
    if home_dir is not None:
        base = Path(home_dir).expanduser().resolve()
        return base / ".config" / "omc"
    override = os.environ.get("OMC_CONFIG_DIR")
    if override:
        return Path(override).expanduser().resolve()
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg).expanduser().resolve() / "omc"
    home = Path(os.environ.get("HOME", "~")).expanduser().resolve()
    return home / ".config" / "omc"


def config_path(home_dir: str | os.PathLike[str] | None = None) -> Path:
    """Return the full path to the config JSON file."""
    return config_dir(home_dir) / "config.json"


@dataclass(frozen=True, slots=True)
class ServerEntry:
    """One server's stored credential.

    `username` is captured at login time (the same `me()` round-trip
    verifies the token and learns the username) so the CLI can build
    `Authorization: Basic <b64(user:token)>` headers the LFS endpoints
    require. Older config files may have `None` here — `Store.with_username`
    fills it on demand.
    """

    url: str
    token: str
    username: str | None = None


@dataclass(frozen=True, slots=True)
class Store:
    """In-memory snapshot of the credential store.

    `default_server` is the URL the CLI uses when no `--server` flag is
    passed; the constructor enforces that it is a member of `servers`
    so a stale default cannot outlive its entry.
    """

    default_server: str | None
    servers: dict[str, ServerEntry]

    def __post_init__(self) -> None:
        if self.default_server is not None and self.default_server not in self.servers:
            raise ConfigError(
                f"Default server {self.default_server!r} is not in the credential store.",
            )
        for url in self.servers:
            if not url:
                raise ConfigError("Server URL must not be empty.")

    # ------------------------------------------------------------------
    # Construction / persistence
    # ------------------------------------------------------------------

    @classmethod
    def empty(cls) -> Self:
        """Return an in-memory store with no servers."""
        return cls(default_server=None, servers={})

    @classmethod
    def load(cls, path: Path) -> Self:
        """Load the store from `path`; missing file → `empty()`."""
        if not path.exists():
            return cls.empty()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ConfigError(
                f"Config file is not valid JSON: {path} ({exc.msg} at line {exc.lineno}).",
            ) from exc
        if not isinstance(raw, dict):
            raise ConfigError(f"Config file root must be an object: {path}.")
        default_server = raw.get("default_server")
        if default_server is not None and not isinstance(default_server, str):
            raise ConfigError("`default_server` must be a string when present.")
        servers_raw = raw.get("servers", {})
        if not isinstance(servers_raw, dict):
            raise ConfigError("`servers` must be an object.")
        servers: dict[str, ServerEntry] = {}
        for url, entry in servers_raw.items():
            if not isinstance(url, str) or not url:
                raise ConfigError("Every server key must be a non-empty string.")
            if not isinstance(entry, dict):
                raise ConfigError(f"Server entry for {url!r} must be an object.")
            token = entry.get("token")
            if not isinstance(token, str) or not token:
                raise ConfigError(f"Server entry for {url!r} is missing a token.")
            raw_username = entry.get("username")
            username = raw_username if isinstance(raw_username, str) and raw_username else None
            servers[url] = ServerEntry(url=url, token=token, username=username)
        return cls(default_server=default_server, servers=servers)

    def save(self, path: Path) -> None:
        """Persist the store to `path` (atomic via sibling tmp; mode 0600)."""
        path.parent.mkdir(parents=True, exist_ok=True, mode=_DIR_MODE)
        servers_payload: dict[str, dict[str, str]] = {}
        for url, entry in self.servers.items():
            row: dict[str, str] = {"token": entry.token}
            if entry.username:
                row["username"] = entry.username
            servers_payload[url] = row
        payload: dict[str, Any] = {
            "default_server": self.default_server,
            "servers": servers_payload,
        }
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.chmod(tmp, _FILE_MODE)
        os.replace(tmp, path)
        os.chmod(path, _FILE_MODE)

    # ------------------------------------------------------------------
    # Mutations (return new `Store` so the dataclass stays frozen)
    # ------------------------------------------------------------------

    def with_login(self, url: str, token: str, *, username: str | None = None) -> Self:
        """Add or replace `url → token`, leaving the default server alone.

        `username` is captured by `auth login` (same `me()` round-trip
        that verifies the token). When omitted, any existing username
        for the server is preserved so a bare re-login does not blank
        the Basic-auth identity the LFS path needs.
        """
        new_servers = dict(self.servers)
        prior = new_servers.get(url)
        effective_username = username if username else (prior.username if prior else None)
        new_servers[url] = ServerEntry(url=url, token=token, username=effective_username)
        default = self.default_server if self.default_server in new_servers else None
        if default is None and new_servers:
            # First-ever login: pin the default to the new server so a
            # subsequent `omc auth whoami` works without `--server`.
            default = url
        return replace(self, default_server=default, servers=new_servers)

    def without(self, url: str) -> Self:
        """Remove the entry for `url`. `default_server` is repointed if it
        pointed at the removed server."""
        if url not in self.servers:
            return self
        new_servers = {k: v for k, v in self.servers.items() if k != url}
        default = self.default_server
        if default == url:
            default = next(iter(new_servers), None)
        return replace(self, default_server=default, servers=new_servers)

    def with_default(self, url: str) -> Self:
        """Pin `url` as the default; raises if the server is unknown."""
        if url not in self.servers:
            raise ConfigError(
                f"Unknown server: {url!r}. Run `omc auth login --server <url>` first.",
            )
        return replace(self, default_server=url)

    # ------------------------------------------------------------------
    # Lookup
    # ------------------------------------------------------------------

    def resolve(
        self,
        requested: str | None,
        *,
        env: dict[str, str] | None = None,
    ) -> tuple[str, str]:
        """Pick `(server_url, token)` honoring env overrides.

        Resolution order: `requested` argument (from `--server`), then
        `OMC_SERVER` env var, then `default_server` in the config file.
        The token is sourced from `OMC_TOKEN` first, then from the
        resolved server's stored entry, so a CI job can run
        `OMC_SERVER=… OMC_TOKEN=… omc …` without ever touching the on-disk
        store.
        """
        env_map = env if env is not None else os.environ
        url = requested or env_map.get(_ENV_SERVER) or self.default_server
        if not url:
            raise AuthRequiredError(
                "No server configured. Run `omc auth login --server <url>`.",
            )
        url = normalize_server_url(url)
        token = env_map.get(_ENV_TOKEN)
        if token is None:
            entry = self.servers.get(url)
            if entry is None:
                raise AuthRequiredError(
                    f"No token stored for {url}. Run `omc auth login --server {url}`.",
                )
            token = entry.token
        return url, token

    # ------------------------------------------------------------------
    # Introspection (used by `omc auth status`)
    # ------------------------------------------------------------------

    def list_servers(self) -> list[str]:
        """Return the sorted list of configured server URLs."""
        return sorted(self.servers.keys())

    def get(self, url: str) -> ServerEntry | None:
        """Return the stored entry for `url` (already-normalized) or `None`."""
        return self.servers.get(url)

    def with_username(self, url: str, username: str) -> Self:
        """Return a new store with `username` set on the entry for `url`."""
        if not username:
            raise ConfigError("Username must not be empty.")
        entry = self.servers.get(url)
        if entry is None:
            msg = f"Unknown server: {url!r}. Run `omc auth login --server <url>` first."
            raise ConfigError(msg)
        new_servers = dict(self.servers)
        new_servers[url] = ServerEntry(url=entry.url, token=entry.token, username=username)
        return replace(self, servers=new_servers)


# ---------------------------------------------------------------------------
# Convenience facade: open + save the file in one call.
# ---------------------------------------------------------------------------


def load_store(path: Path | None = None) -> Store:
    """Load the store from `path`, defaulting to the user's config dir."""
    return Store.load(path if path is not None else config_path())


def save_store(store: Store, path: Path | None = None) -> None:
    """Save `store` to `path`, defaulting to the user's config dir."""
    store.save(path if path is not None else config_path())


__all__ = [
    "ServerEntry",
    "Store",
    "config_dir",
    "config_path",
    "load_store",
    "normalize_server_url",
    "save_store",
]
