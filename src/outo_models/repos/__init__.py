"""Repository domain layer.

Re-exports the public surface consumed by WP-10 (git smart-HTTP) and
WP-13 (FastAPI routers): enums, value objects, the create / delete /
quota / reflog services, and the storage primitives they share.

Every module under this package is responsible for ONE thing:
    models     — pure enums + dataclasses (no DB, no disk)
    storage    — paths, disk_usage, per-repo locks
    quota      — UserQuota / UserUsage helpers
    create     — `create_repo` (on-disk + DB, compensating cleanup)
    delete     — `delete_repo` (revisions + row + dir + usage decrement)
    reflog     — walk recent commits from a bare repo
"""

from outo_models.repos.create import create_repo
from outo_models.repos.delete import delete_repo
from outo_models.repos.models import RepoInfo, RepoKind, Visibility
from outo_models.repos.quota import (
    add_usage,
    check_push_allowed,
    ensure_quota_rows,
    reconcile_user,
)
from outo_models.repos.reflog import RevisionInfo, recent_revisions
from outo_models.repos.storage import (
    REPO_LOCKS,
    RepoLockRegistry,
    disk_usage,
    repo_exists,
    repo_fs_path,
)

__all__ = [
    "REPO_LOCKS",
    "RepoInfo",
    "RepoKind",
    "RepoLockRegistry",
    "RevisionInfo",
    "Visibility",
    "add_usage",
    "check_push_allowed",
    "create_repo",
    "delete_repo",
    "disk_usage",
    "ensure_quota_rows",
    "recent_revisions",
    "reconcile_user",
    "repo_exists",
    "repo_fs_path",
]
