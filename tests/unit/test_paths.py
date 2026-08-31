"""Tests for `outo_models.utils.paths`."""

from __future__ import annotations

from outo_models.utils.paths import (
    audit_dir,
    certs_dir,
    data_dir,
    ensure_dirs,
    repo_path,
    repos_dir,
    spaces_dir,
)


class TestDirHelpers:
    """Each helper returns the right subdirectory of the configured data_dir."""

    def test_data_dir_matches_settings(self, settings) -> None:
        assert data_dir() == settings.data_dir

    def test_repos_dir_is_data_repos(self, settings) -> None:
        assert repos_dir() == settings.data_dir / "repos"

    def test_repo_path_layout_is_owner_dot_git(self, settings) -> None:
        expected = settings.data_dir / "repos" / "alice" / "model.git"
        assert repo_path("alice", "model") == expected

    def test_repo_path_preserves_dots_in_name(self, settings) -> None:
        expected = settings.data_dir / "repos" / "bob" / "weird.name.git"
        assert repo_path("bob", "weird.name") == expected

    def test_spaces_dir_is_data_spaces(self, settings) -> None:
        assert spaces_dir() == settings.data_dir / "spaces"

    def test_certs_dir_is_data_certs(self, settings) -> None:
        assert certs_dir() == settings.data_dir / "certs"

    def test_audit_dir_is_data_audit(self, settings) -> None:
        assert audit_dir() == settings.data_dir / "audit"


class TestEnsureDirs:
    """`ensure_dirs` must create every directory but not fail if they exist."""

    def test_creates_all_top_level_dirs(self, settings) -> None:
        assert not repos_dir().exists()
        assert not spaces_dir().exists()
        assert not certs_dir().exists()
        assert not audit_dir().exists()

        ensure_dirs()

        assert data_dir().is_dir()
        assert repos_dir().is_dir()
        assert spaces_dir().is_dir()
        assert certs_dir().is_dir()
        assert audit_dir().is_dir()

    def test_is_idempotent(self, settings) -> None:
        ensure_dirs()
        ensure_dirs()
        assert data_dir().is_dir()
        assert repos_dir().is_dir()

    def test_returns_none(self, settings) -> None:
        assert ensure_dirs() is None
