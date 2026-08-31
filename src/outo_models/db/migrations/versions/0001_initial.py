"""initial schema

Revision ID: 0001_initial
Revises:
Create Date: 2026-08-31 00:00:00

Creates every table that ships in WP-1 (outo-models v1):

    users                    — account rows (admin gating on `status`)
    repos                    — git repositories (model / dataset / space)
    revisions                — immutable commits recorded against a repo
    personal_access_tokens   — hashed PAT fingerprints (plaintext never stored)
    approvals                — per-user signup-decision row (one per user)
    user_quotas              — operator-assigned storage cap per user
    user_usages              — current storage consumption per user
    audit_logs               — append-only audit trail
    web_settings             — operator-editable web-admin key/value store
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0001_initial"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("username", sa.String(length=255), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False, server_default="user"),
        sa.Column(
            "status", sa.String(length=16), nullable=False, server_default="pending"
        ),
        sa.Column("display_name", sa.String(length=255), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("approved_by_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_users"),
        sa.UniqueConstraint("username", name="uq_users_username"),
        sa.UniqueConstraint("email", name="uq_users_email"),
        sa.ForeignKeyConstraint(
            ["approved_by_id"],
            ["users.id"],
            name="fk_users_approved_by_id_users",
            use_alter=True,
        ),
    )

    op.create_table(
        "repos",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("owner_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=63), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column(
            "visibility",
            sa.String(length=16),
            nullable=False,
            server_default="private",
        ),
        sa.Column("description", sa.String(length=500), nullable=True),
        sa.Column(
            "default_branch",
            sa.String(length=64),
            nullable=False,
            server_default="main",
        ),
        sa.Column("size_bytes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("path", sa.String(length=500), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_repos"),
        sa.UniqueConstraint("owner_id", "kind", "name", name="uq_repos_owner_id_kind_name"),
        sa.ForeignKeyConstraint(
            ["owner_id"], ["users.id"], name="fk_repos_owner_id_users"
        ),
    )
    op.create_index("ix_repos_owner_id", "repos", ["owner_id"])
    op.create_index("ix_repos_kind", "repos", ["kind"])

    op.create_table(
        "revisions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("repo_id", sa.Integer(), nullable=False),
        sa.Column("commit_sha", sa.String(length=40), nullable=False),
        sa.Column(
            "branch", sa.String(length=64), nullable=False, server_default="main"
        ),
        sa.Column("author_id", sa.Integer(), nullable=True),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_revisions"),
        sa.ForeignKeyConstraint(
            ["repo_id"], ["repos.id"], name="fk_revisions_repo_id_repos"
        ),
        sa.ForeignKeyConstraint(
            ["author_id"], ["users.id"], name="fk_revisions_author_id_users"
        ),
    )
    op.create_index("ix_revisions_repo_id", "revisions", ["repo_id"])
    op.create_index("ix_revisions_commit_sha", "revisions", ["commit_sha"])

    op.create_table(
        "personal_access_tokens",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column(
            "fingerprint_hash", sa.String(length=512), nullable=False
        ),
        sa.Column("prefix", sa.String(length=8), nullable=False),
        sa.Column("scopes", sa.String(length=2000), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_personal_access_tokens"),
        sa.UniqueConstraint("fingerprint_hash", name="uq_personal_access_tokens_fingerprint_hash"),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_personal_access_tokens_user_id_users",
        ),
    )
    op.create_index(
        "ix_personal_access_tokens_user_id", "personal_access_tokens", ["user_id"]
    )

    op.create_table(
        "approvals",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("decided_by_id", sa.Integer(), nullable=True),
        sa.Column(
            "decision", sa.String(length=16), nullable=False, server_default="pending"
        ),
        sa.Column("reason", sa.String(length=500), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_approvals"),
        sa.UniqueConstraint("user_id", name="uq_approvals_user_id"),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_approvals_user_id_users"
        ),
        sa.ForeignKeyConstraint(
            ["decided_by_id"],
            ["users.id"],
            name="fk_approvals_decided_by_id_users",
        ),
    )

    op.create_table(
        "user_quotas",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("max_bytes", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_user_quotas"),
        sa.UniqueConstraint("user_id", name="uq_user_quotas_user_id"),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_user_quotas_user_id_users"
        ),
    )

    op.create_table(
        "user_usages",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column(
            "used_bytes", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_user_usages"),
        sa.UniqueConstraint("user_id", name="uq_user_usages_user_id"),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_user_usages_user_id_users"
        ),
    )

    op.create_table(
        "audit_logs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("actor_id", sa.Integer(), nullable=True),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("target_type", sa.String(length=32), nullable=True),
        sa.Column("target_id", sa.String(length=64), nullable=True),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("ip", sa.String(length=45), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_audit_logs"),
        sa.ForeignKeyConstraint(
            ["actor_id"], ["users.id"], name="fk_audit_logs_actor_id_users"
        ),
    )
    op.create_index("ix_audit_logs_action", "audit_logs", ["action"])
    op.create_index("ix_audit_logs_created_at", "audit_logs", ["created_at"])

    op.create_table(
        "web_settings",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("key", sa.String(length=128), nullable=False),
        sa.Column("value", sa.String(length=2000), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_web_settings"),
        sa.UniqueConstraint("key", name="uq_web_settings_key"),
    )


def downgrade() -> None:
    op.drop_table("web_settings")
    op.drop_index("ix_audit_logs_created_at", table_name="audit_logs")
    op.drop_index("ix_audit_logs_action", table_name="audit_logs")
    op.drop_table("audit_logs")
    op.drop_table("user_usages")
    op.drop_table("user_quotas")
    op.drop_table("approvals")
    op.drop_index(
        "ix_personal_access_tokens_user_id", table_name="personal_access_tokens"
    )
    op.drop_table("personal_access_tokens")
    op.drop_index("ix_revisions_commit_sha", table_name="revisions")
    op.drop_index("ix_revisions_repo_id", table_name="revisions")
    op.drop_table("revisions")
    op.drop_index("ix_repos_kind", table_name="repos")
    op.drop_index("ix_repos_owner_id", table_name="repos")
    op.drop_table("repos")
    op.drop_table("users")