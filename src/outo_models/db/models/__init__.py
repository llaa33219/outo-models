"""ORM models for outo-models.

Every model in this package inherits from `Base` (declared in
`outo_models.db.models.base`) and lives in its own module so that Alembic's
autogenerator can match table ↔ class 1:1. The `__all__` list below is the
canonical public surface — `outo_models.db` re-exports it.
"""

from outo_models.db.models.approval import Approval
from outo_models.db.models.audit import AuditLog
from outo_models.db.models.base import Base, IntIdMixin, TimestampMixin, TimestampWithUpdateMixin
from outo_models.db.models.posts import (
    POST_BODY_LONG_MAX,
    POST_BODY_SHORT_MAX,
    POST_COMMENT_BODY_MAX,
    POST_KIND_LONG,
    POST_KIND_SHORT,
    POST_TITLE_MAX,
    REACTION_PALETTE,
    Post,
    PostComment,
    PostReaction,
)
from outo_models.db.models.quota import UserQuota, UserUsage
from outo_models.db.models.repo import Repo
from outo_models.db.models.revision import Revision
from outo_models.db.models.social import RepoComment, RepoLike, UserFollow
from outo_models.db.models.token import PersonalAccessToken
from outo_models.db.models.user import User
from outo_models.db.models.web_settings import WebSetting

__all__ = [
    "POST_BODY_LONG_MAX",
    "POST_BODY_SHORT_MAX",
    "POST_COMMENT_BODY_MAX",
    "POST_KIND_LONG",
    "POST_KIND_SHORT",
    "POST_TITLE_MAX",
    "REACTION_PALETTE",
    "Approval",
    "AuditLog",
    "Base",
    "IntIdMixin",
    "PersonalAccessToken",
    "Post",
    "PostComment",
    "PostReaction",
    "Repo",
    "RepoComment",
    "RepoLike",
    "Revision",
    "TimestampMixin",
    "TimestampWithUpdateMixin",
    "User",
    "UserFollow",
    "UserQuota",
    "UserUsage",
    "WebSetting",
]
