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
    card       — README + front-matter + safe markdown rendering
    files      — traversal-safe tree listing via dulwich
    social     — likes, follows, comments service helpers
"""

from outo_models.repos.card import CardMetadata, parse_card_metadata, read_card, read_readme
from outo_models.repos.create import create_repo
from outo_models.repos.delete import delete_repo
from outo_models.repos.files import FileEntry, list_files
from outo_models.repos.models import RepoInfo, RepoKind, Visibility
from outo_models.repos.quota import (
    add_usage,
    check_push_allowed,
    ensure_quota_rows,
    reconcile_user,
)
from outo_models.repos.reflog import RevisionInfo, recent_revisions
from outo_models.repos.social import (
    add_comment,
    follow_user,
    follower_count,
    is_following,
    is_liked,
    like_count,
    like_repo,
    list_comments,
    load_repo_or_404,
    load_user_or_404,
    unfollow_user,
    unlike_repo,
)
from outo_models.repos.storage import (
    REPO_LOCKS,
    RepoLockRegistry,
    disk_usage,
    repo_exists,
    repo_fs_path,
)

__all__ = [
    "REPO_LOCKS",
    "CardMetadata",
    "FileEntry",
    "RepoInfo",
    "RepoKind",
    "RepoLockRegistry",
    "RevisionInfo",
    "Visibility",
    "add_comment",
    "add_usage",
    "check_push_allowed",
    "create_repo",
    "delete_repo",
    "disk_usage",
    "ensure_quota_rows",
    "follow_user",
    "follower_count",
    "is_following",
    "is_liked",
    "like_count",
    "like_repo",
    "list_comments",
    "list_files",
    "load_repo_or_404",
    "load_user_or_404",
    "parse_card_metadata",
    "read_card",
    "read_readme",
    "recent_revisions",
    "reconcile_user",
    "repo_exists",
    "repo_fs_path",
    "unfollow_user",
    "unlike_repo",
]
