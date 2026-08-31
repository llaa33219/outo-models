"""Filesystem layout for the outo-models data directory.

Every helper reads from `get_settings()` so the layout follows whatever
`OUTO_DATA_DIR` points at — tests get a tmpdir, prod gets `/var/lib/outo-models`.
"""

from __future__ import annotations

from pathlib import Path

from outo_models.config import get_settings


def data_dir() -> Path:
    """Return the root data directory."""
    return get_settings().data_dir


def repos_dir() -> Path:
    """Return the directory that holds bare git repositories."""
    return data_dir() / "repos"


def repo_path(owner: str, name: str) -> Path:
    """Return the on-disk path of a single bare repository.

    Layout: `repos_dir/<owner>/<name>.git`. The owner is a flat segment so
    we can list every repo an owner owns with a single `iterdir()`.
    """
    return repos_dir() / owner / f"{name}.git"


def spaces_dir() -> Path:
    """Return the directory that holds Spaces metadata + static assets."""
    return data_dir() / "spaces"


def certs_dir() -> Path:
    """Return the directory that holds ACME / TLS certificates."""
    return data_dir() / "certs"


def lfs_dir() -> Path:
    """Return the directory that holds Git LFS objects (local backend)."""
    return data_dir() / "lfs"


def audit_dir() -> Path:
    """Return the directory that holds the append-only audit log."""
    return data_dir() / "audit"


def ensure_dirs() -> None:
    """Create every data directory; idempotent."""
    for directory in (data_dir(), repos_dir(), spaces_dir(), certs_dir(), audit_dir(), lfs_dir()):
        directory.mkdir(parents=True, exist_ok=True)
