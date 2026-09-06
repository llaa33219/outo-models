"""Tests for the multi-server credential store."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from outo_models_cli import config
from outo_models_cli.errors import AuthRequiredError, ConfigError


def test_normalize_strips_trailing_slash() -> None:
    assert config.normalize_server_url("http://example.com/") == "http://example.com"
    assert config.normalize_server_url("http://example.com///") == "http://example.com"


def test_normalize_adds_http_scheme_when_missing() -> None:
    assert config.normalize_server_url("192.168.0.10") == "http://192.168.0.10"
    assert config.normalize_server_url("example.com:8080") == "http://example.com:8080"


def test_normalize_lowercases_scheme_and_host() -> None:
    assert config.normalize_server_url("HTTPS://Example.COM") == "https://example.com"


def test_normalize_keeps_explicit_https() -> None:
    assert config.normalize_server_url("https://hub.example") == "https://hub.example"


def test_normalize_rejects_empty() -> None:
    with pytest.raises(ConfigError):
        config.normalize_server_url("")
    with pytest.raises(ConfigError):
        config.normalize_server_url("   ")


def test_store_with_login_pins_default_on_first_login() -> None:
    store = config.Store.empty()
    store = store.with_login("http://a", "tok-a")
    assert store.default_server == "http://a"
    assert store.servers["http://a"].token == "tok-a"


def test_store_with_login_replaces_token_for_same_url() -> None:
    store = config.Store.empty().with_login("http://a", "tok-a")
    store = store.with_login("http://a", "tok-a-2")
    assert store.servers["http://a"].token == "tok-a-2"


def test_store_with_login_keeps_existing_default() -> None:
    """A second login must NOT silently retarget the default server."""
    store = (
        config.Store.empty()
        .with_login("http://a", "tok-a")
        .with_default("http://a")
        .with_login("http://b", "tok-b")
    )
    assert store.default_server == "http://a"
    assert set(store.servers) == {"http://a", "http://b"}


def test_store_without_removes_one_entry_and_repoints_default() -> None:
    store = (
        config.Store.empty()
        .with_login("http://a", "tok-a")
        .with_login("http://b", "tok-b")
        .with_default("http://a")
    )
    store = store.without("http://a")
    assert "http://a" not in store.servers
    assert store.default_server == "http://b"


def test_store_without_returns_self_when_missing() -> None:
    store = config.Store.empty().with_login("http://a", "tok-a")
    assert store.without("http://nope") == store


def test_store_with_default_raises_for_unknown_server() -> None:
    store = config.Store.empty()
    with pytest.raises(ConfigError):
        store.with_default("http://nope")


def test_store_save_creates_parent_dir_with_0700(tmp_path: Path) -> None:
    """First save: parent dir is 0700, file is 0600."""
    target = tmp_path / "deep" / "config.json"
    store = config.Store.empty().with_login("http://a", "tok-a")
    store.save(target)
    assert stat.S_IMODE(target.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_store_save_tightens_existing_file_mode(tmp_path: Path) -> None:
    """A pre-existing file with looser perms must be re-tightened on save."""
    target = tmp_path / "config.json"
    target.write_text("{}", encoding="utf-8")
    target.chmod(0o644)
    store = config.Store.empty().with_login("http://a", "tok-a")
    store.save(target)
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_store_load_round_trips(tmp_path: Path) -> None:
    target = tmp_path / "config.json"
    original = (
        config.Store.empty()
        .with_login("http://a", "tok-a")
        .with_login("http://b", "tok-b")
        .with_default("http://b")
    )
    original.save(target)
    loaded = config.Store.load(target)
    assert loaded == original


def test_store_load_missing_returns_empty(tmp_path: Path) -> None:
    assert config.Store.load(tmp_path / "nope.json") == config.Store.empty()


def test_store_load_rejects_garbage(tmp_path: Path) -> None:
    target = tmp_path / "config.json"
    target.write_text("not json", encoding="utf-8")
    with pytest.raises(ConfigError):
        config.Store.load(target)


def test_store_load_rejects_root_not_object(tmp_path: Path) -> None:
    target = tmp_path / "config.json"
    target.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    with pytest.raises(ConfigError):
        config.Store.load(target)


def test_store_load_rejects_empty_token(tmp_path: Path) -> None:
    target = tmp_path / "config.json"
    target.write_text(json.dumps({"servers": {"http://a": {"token": ""}}}), encoding="utf-8")
    with pytest.raises(ConfigError):
        config.Store.load(target)


def test_resolve_prefers_requested_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = config.Store.empty().with_login("http://a", "tok-a").with_login("http://b", "tok-b")
    url, tok = store.resolve("http://b", env={})
    assert url == "http://b"
    assert tok == "tok-b"


def test_resolve_uses_env_server(tmp_path: Path) -> None:
    store = config.Store.empty().with_login("http://a", "tok-a")
    url, tok = store.resolve(None, env={"OMC_SERVER": "http://a", "OMC_TOKEN": "tok-a"})
    assert url == "http://a"
    assert tok == "tok-a"


def test_resolve_prefers_env_token_over_stored(tmp_path: Path) -> None:
    store = config.Store.empty().with_login("http://a", "tok-a")
    _url, tok = store.resolve(None, env={"OMC_SERVER": "http://a", "OMC_TOKEN": "override"})
    assert tok == "override"


def test_resolve_raises_auth_required_when_nothing_configured(tmp_path: Path) -> None:
    store = config.Store.empty()
    with pytest.raises(AuthRequiredError):
        store.resolve(None, env={})


def test_resolve_raises_auth_required_when_url_unknown(tmp_path: Path) -> None:
    store = config.Store.empty().with_login("http://a", "tok-a")
    with pytest.raises(AuthRequiredError):
        store.resolve("http://b", env={})


def test_config_dir_honors_omc_config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMC_CONFIG_DIR", str(tmp_path / "override"))
    assert config.config_dir() == (tmp_path / "override").resolve()


def test_config_dir_honors_xdg_config_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OMC_CONFIG_DIR", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert config.config_dir() == (tmp_path / "xdg" / "omc").resolve()


def test_config_dir_falls_back_to_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OMC_CONFIG_DIR", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert config.config_dir() == (tmp_path / ".config" / "omc").resolve()


def test_config_dir_explicit_home_overrides_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An explicit `home_dir` arg beats env vars — needed for test isolation."""
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.setenv("OMC_CONFIG_DIR", str(other))
    explicit = tmp_path / "explicit"
    explicit.mkdir()
    assert config.config_dir(explicit) == (explicit / ".config" / "omc").resolve()


def test_save_store_writes_atomically(tmp_path: Path, config_path: Path) -> None:
    """`save_store` is the public facade — round-trip via it works."""
    store = config.Store.empty().with_login("http://a", "tok-a")
    config.save_store(store, config_path)
    assert config_path.exists()
    assert config.Store.load(config_path) == store


def test_no_token_in_serialized_default_server_metadata(tmp_path: Path) -> None:
    """The serialized JSON must not store default_server metadata beyond a URL."""
    store = config.Store.empty().with_login("http://a", "tok-a")
    target = tmp_path / "config.json"
    store.save(target)
    data = json.loads(target.read_text(encoding="utf-8"))
    assert set(data.keys()) == {"default_server", "servers"}
    assert data["default_server"] == "http://a"


def test_resolve_does_not_mutate_store(tmp_path: Path) -> None:
    store = config.Store.empty().with_login("http://a", "tok-a")
    before = store
    store.resolve(None, env={"OMC_TOKEN": "x"})
    assert store == before
