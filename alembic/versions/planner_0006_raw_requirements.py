"""add raw requirement persistence and planner revision links."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "planner_0006_raw_requirements"
down_revision: str | None = "identity_0005_projects"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "planner_raw_requirements",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(128), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_planner_raw_requirements_project_id", "planner_raw_requirements", ["project_id"]
    )
    op.create_index("ix_planner_raw_requirements_status", "planner_raw_requirements", ["status"])
    op.create_index(
        "ix_planner_raw_requirements_project_created",
        "planner_raw_requirements",
        ["project_id", "created_at"],
    )
    op.create_table(
        "planner_raw_requirement_revisions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "raw_requirement_id",
            sa.String(36),
            sa.ForeignKey("planner_raw_requirements.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("project_context", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "raw_requirement_id",
            "revision",
            name="uq_planner_raw_requirement_revisions_raw_requirement_id",
        ),
    )
    op.create_index(
        "ix_planner_raw_requirement_revisions_raw_requirement_id",
        "planner_raw_requirement_revisions",
        ["raw_requirement_id"],
    )
    op.add_column(
        "planner_workflow_runs",
        sa.Column("raw_requirement_revision_id", sa.String(36), nullable=True),
    )
    op.create_index(
        "ix_planner_workflow_runs_raw_requirement_revision_id",
        "planner_workflow_runs",
        ["raw_requirement_revision_id"],
    )
    op.create_foreign_key(
        "fk_planner_runs_raw_revision",
        "planner_workflow_runs",
        "planner_raw_requirement_revisions",
        ["raw_requirement_revision_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.add_column(
        "planner_requirement_revisions",
        sa.Column("raw_requirement_revision_id", sa.String(36), nullable=True),
    )
    op.create_index(
        "ix_planner_requirement_revisions_raw_requirement_revision_id",
        "planner_requirement_revisions",
        ["raw_requirement_revision_id"],
    )
    op.create_foreign_key(
        "fk_planner_req_revisions_raw_revision",
        "planner_requirement_revisions",
        "planner_raw_requirement_revisions",
        ["raw_requirement_revision_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_planner_req_revisions_raw_revision", "planner_requirement_revisions", type_="foreignkey"
    )
    op.drop_index(
        "ix_planner_requirement_revisions_raw_requirement_revision_id",
        table_name="planner_requirement_revisions",
    )
    op.drop_column("planner_requirement_revisions", "raw_requirement_revision_id")
    op.drop_constraint("fk_planner_runs_raw_revision", "planner_workflow_runs", type_="foreignkey")
    op.drop_index(
        "ix_planner_workflow_runs_raw_requirement_revision_id", table_name="planner_workflow_runs"
    )
    op.drop_column("planner_workflow_runs", "raw_requirement_revision_id")
    op.drop_table("planner_raw_requirement_revisions")
    op.drop_table("planner_raw_requirements")
