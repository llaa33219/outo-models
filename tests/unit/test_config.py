"""Tests for `outo_models.config`."""

from __future__ import annotations

from pathlib import Path

import pytest

from outo_models.config import Settings, get_settings
from outo_models.exceptions import ConfigError


class TestSettingsDefaults:
    """Defaults must work with no environment variables at all."""

    def test_data_dir_default_is_var_lib_outo_models(self) -> None:
        # Use the raw constructor without env overrides.
        get_settings.cache_clear()
        s = Settings(_env_file=None)  # type: ignore[call-arg]
        assert s.data_dir == Path("/var/lib/outo-models")

    def test_domain_default_is_localhost(self) -> None:
        get_settings.cache_clear()
        s = Settings(_env_file=None)  # type: ignore[call-arg]
        assert s.domain == "localhost"

    def test_db_url_default_is_none(self) -> None:
        get_settings.cache_clear()
        s = Settings(_env_file=None)  # type: ignore[call-arg]
        assert s.db_url is None

    def test_secret_key_default_is_empty(self) -> None:
        get_settings.cache_clear()
        s = Settings(_env_file=None)  # type: ignore[call-arg]
        assert s.secret_key == ""

    def test_env_default_is_development(self) -> None:
        get_settings.cache_clear()
        s = Settings(_env_file=None)  # type: ignore[call-arg]
        assert s.env == "development"

    def test_require_approval_default_true(self) -> None:
        get_settings.cache_clear()
        s = Settings(_env_file=None)  # type: ignore[call-arg]
        assert s.require_approval is True

    def test_default_quota_bytes_is_10_gib(self) -> None:
        get_settings.cache_clear()
        s = Settings(_env_file=None)  # type: ignore[call-arg]
        assert s.default_quota_bytes == 10 * 1024**3


class TestResolvedDbUrl:
    """`resolved_db_url` returns the explicit `db_url` or derives a SQLite URL."""

    def test_returns_explicit_db_url(self) -> None:
        s = Settings(db_url="postgresql+asyncpg://u:p@h:5432/d", _env_file=None)  # type: ignore[call-arg]
        assert s.resolved_db_url == "postgresql+asyncpg://u:p@h:5432/d"

    def test_derives_sqlite_url_from_data_dir(self) -> None:
        s = Settings(data_dir=Path("/srv/data"), _env_file=None)  # type: ignore[call-arg]
        assert s.resolved_db_url == f"sqlite+aiosqlite:///{s.data_dir.as_posix()}/db.sqlite3"

    def test_derived_db_url_uses_forward_slashes(self, settings: Settings) -> None:
        # Even if the host were Windows, the SQLite URL must be POSIX form.
        s = Settings(data_dir=Path("/srv/data with spaces"), _env_file=None)  # type: ignore[call-arg]
        assert "\\" not in s.resolved_db_url
        assert s.resolved_db_url.startswith("sqlite+aiosqlite:///")


class TestBaseUrl:
    """`base_url` chooses http for localhost, https otherwise."""

    def test_https_for_real_domain(self) -> None:
        s = Settings(domain="models.example.com", _env_file=None)  # type: ignore[call-arg]
        assert s.base_url == "https://models.example.com"

    def test_http_for_localhost(self) -> None:
        s = Settings(domain="localhost", _env_file=None)  # type: ignore[call-arg]
        assert s.base_url == "http://localhost"

    def test_http_for_loopback_ip(self) -> None:
        s = Settings(domain="127.0.0.1", _env_file=None)  # type: ignore[call-arg]
        assert s.base_url == "http://127.0.0.1"


class TestEnvLoading:
    """OUTO_-prefixed environment variables feed the Settings."""

    def test_outo_data_dir_env_is_picked_up(self, tmp_data_dir: Path) -> None:
        s = get_settings()
        assert s.data_dir == tmp_data_dir

    def test_outo_domain_env_is_picked_up(
        self, tmp_data_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OUTO_DOMAIN", "hub.example.org")
        get_settings.cache_clear()
        assert get_settings().domain == "hub.example.org"


class TestGetSettings:
    """`get_settings()` is cached and the cache can be cleared."""

    def test_returns_same_instance_on_repeat_call(self) -> None:
        get_settings.cache_clear()
        first = get_settings()
        second = get_settings()
        assert first is second

    def test_cache_clear_returns_new_instance(self) -> None:
        first = get_settings()
        get_settings.cache_clear()
        second = get_settings()
        assert first is not second


class TestValidateForProduction:
    """`validate_for_production` enforces secret strength only in production."""

    def test_development_does_not_check_secret(self) -> None:
        s = Settings(env="development", secret_key="", _env_file=None)  # type: ignore[call-arg]
        s.validate_for_production()  # must not raise

    def test_production_with_empty_secret_raises(self) -> None:
        s = Settings(env="production", secret_key="", _env_file=None)  # type: ignore[call-arg]
        with pytest.raises(ConfigError):
            s.validate_for_production()

    def test_production_with_short_secret_raises(self) -> None:
        s = Settings(env="production", secret_key="short-but-not-empty", _env_file=None)  # type: ignore[call-arg]
        with pytest.raises(ConfigError):
            s.validate_for_production()

    def test_production_with_strong_secret_passes(self) -> None:
        strong = "x" * 48
        s = Settings(env="production", secret_key=strong, _env_file=None)  # type: ignore[call-arg]
        s.validate_for_production()  # must not raise


class TestConfigErrorIsOutoError:
    """ConfigError must carry the standard `code` / `status_code` fields."""

    def test_config_error_default_status_code(self) -> None:
        s = Settings(env="production", secret_key="", _env_file=None)  # type: ignore[call-arg]
        try:
            s.validate_for_production()
        except ConfigError as exc:
            assert exc.status_code == 500
            assert isinstance(exc.code, str)
            assert exc.code
        else:
            pytest.fail("expected ConfigError")
