"""Git smart-HTTP service for outo-models.

`GitSmartService.asgi_app()` returns an ASGI app WP-13 mounts under `/git`,
turning `git clone/push/pull http(s)://<host>/<owner>/<name>.git` into a
real round-trip against the local bare repo, with auth, quota, and
audit. The package is small on purpose — every member is part of the
public API the rest of the system depends on.
"""

from outo_models.git_smart.auth import GitAction, authorize, resolve_git_identity
from outo_models.git_smart.lfs import lfs_not_supported
from outo_models.git_smart.service import DEFAULT_MAX_PUSH_BYTES, GitSmartService

__all__ = [
    "DEFAULT_MAX_PUSH_BYTES",
    "GitAction",
    "GitSmartService",
    "authorize",
    "lfs_not_supported",
    "resolve_git_identity",
]
