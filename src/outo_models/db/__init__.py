"""Database layer for outo-models.

Public surface:

    Engine / lifecycle:
        - `get_engine(settings=None)` — cached async engine per `db_url`
        - `dispose_engines()` — test helper to dispose every cached engine
        - `run_migrations(engine)` — programmatic `alembic upgrade head`

    Sessions:
        - `get_session_factory(engine=None)` — `async_sessionmaker` factory
        - `session_scope()` — async context manager with commit / rollback

    ORM models:
        - `User`, `Repo`, `Revision`, `PersonalAccessToken`, `Approval`,
          `UserQuota`, `UserUsage`, `AuditLog`, `WebSetting`,
          `RepoLike`, `UserFollow`, `RepoComment`,
          `Post`, `PostReaction`, `PostComment`
        - `Base` and shared mixins
"""

from outo_models.db.engine import dispose_engines, get_engine, run_migrations
from outo_models.db.models import (
    POST_BODY_LONG_MAX,
    POST_BODY_SHORT_MAX,
    POST_COMMENT_BODY_MAX,
    POST_KIND_LONG,
    POST_KIND_SHORT,
    POST_TITLE_MAX,
    REACTION_PALETTE,
    Approval,
    AuditLog,
    Base,
    IntIdMixin,
    PersonalAccessToken,
    Post,
    PostComment,
    PostReaction,
    Repo,
    RepoComment,
    RepoLike,
    Revision,
    TimestampMixin,
    TimestampWithUpdateMixin,
    User,
    UserFollow,
    UserQuota,
    UserUsage,
    WebSetting,
)
from outo_models.db.session import get_session_factory, session_scope

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
    "dispose_engines",
    "get_engine",
    "get_session_factory",
    "run_migrations",
    "session_scope",
]
