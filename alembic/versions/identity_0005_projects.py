"""users and projects for the authenticated planner workspace."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "identity_0005_projects"
down_revision: str | None = "planner_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("username", sa.String(80), nullable=False),
        sa.Column("display_name", sa.String(160)),
        sa.Column("password_hash", sa.String(256), nullable=False),
        sa.Column("role", sa.String(32), nullable=False, server_default="user"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("is_email_verified", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("preferred_locale", sa.String(16), nullable=False, server_default="zh-CN"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_login_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_users_email", "users", ["email"], unique=True)
    op.create_index("ix_users_username", "users", ["username"], unique=True)
    op.create_table(
        "projects",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "owner_user_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column("status", sa.String(32), nullable=False, server_default="active"),
        sa.Column("llm_prompt_language", sa.String(16), nullable=False, server_default="zh-CN"),
        sa.Column("target_users", sa.JSON(), nullable=False),
        sa.Column("business_domain", sa.String(160)),
        sa.Column("frontend_tech_stack", sa.JSON(), nullable=False),
        sa.Column("backend_tech_stack", sa.JSON(), nullable=False),
        sa.Column("database_tech_stack", sa.JSON(), nullable=False),
        sa.Column("global_constraints", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_opened_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_projects_owner_user_id", "projects", ["owner_user_id"])


def downgrade() -> None:
    op.drop_table("projects")
    op.drop_index("ix_users_username", table_name="users")
    op.drop_index("ix_users_email", table_name="users")
    op.drop_table("users")
