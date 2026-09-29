"""add requirement dependencies and durable planner checkpoints."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "planner_0007_deps"
down_revision: str | None = "planner_0006_raw_requirements"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "planner_requirement_dependencies",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "source_requirement_id",
            sa.String(36),
            sa.ForeignKey("planner_requirements.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "target_requirement_id",
            sa.String(36),
            sa.ForeignKey("planner_requirements.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("relation", sa.String(32), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "source_requirement_id",
            "target_requirement_id",
            "relation",
            name="uq_planner_requirement_dependencies_source_requirement_id",
        ),
    )
    op.create_index(
        "ix_planner_requirement_dependencies_source_requirement_id",
        "planner_requirement_dependencies",
        ["source_requirement_id"],
    )
    op.create_index(
        "ix_planner_requirement_dependencies_target_requirement_id",
        "planner_requirement_dependencies",
        ["target_requirement_id"],
    )
    op.add_column("planner_workflow_runs", sa.Column("checkpoint", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("planner_workflow_runs", "checkpoint")
    op.drop_table("planner_requirement_dependencies")
