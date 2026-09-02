"""Public utilities re-exported for convenient single-import access.

All helpers here are pure functions over the current `Settings`; the rest of
the codebase imports them from this module instead of reaching into the
individual submodules.
"""

from outo_models.utils.git_url import clone_url
from outo_models.utils.hashing import hash_secret, verify_secret
from outo_models.utils.net import detect_lan_ipv4, is_ip_address
from outo_models.utils.paths import (
    audit_dir,
    certs_dir,
    data_dir,
    ensure_dirs,
    lfs_dir,
    repo_path,
    repos_dir,
    spaces_dir,
)
from outo_models.utils.slug import normalize_slug, validate_slug
from outo_models.utils.time import utcnow

__all__ = [
    "audit_dir",
    "certs_dir",
    "clone_url",
    "data_dir",
    "detect_lan_ipv4",
    "ensure_dirs",
    "hash_secret",
    "is_ip_address",
    "lfs_dir",
    "normalize_slug",
    "repo_path",
    "repos_dir",
    "spaces_dir",
    "utcnow",
    "validate_slug",
    "verify_secret",
]
