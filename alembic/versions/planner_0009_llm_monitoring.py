"""Persist individual LLM attempts for administrator monitoring."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "planner_0009_llm_monitoring"
down_revision: str | None = "auth_0008_sessions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for column in (
        sa.Column("invocation_id", sa.String(36), nullable=True),
        sa.Column("call_index", sa.Integer(), nullable=True),
        sa.Column("subject", sa.JSON(), nullable=True),
        sa.Column("input_payload", sa.JSON(), nullable=True),
        sa.Column("control_payload", sa.JSON(), nullable=True),
        sa.Column("system_prompt", sa.Text(), nullable=True),
        sa.Column("user_prompt", sa.Text(), nullable=True),
        sa.Column("call_type", sa.String(48), nullable=True),
        sa.Column("max_attempts", sa.Integer(), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    ):
        op.add_column("planner_llm_invocations", column)
    op.create_unique_constraint(
        "uq_planner_llm_invocations_run_invocation_attempt",
        "planner_llm_invocations",
        ["run_id", "invocation_id", "attempt"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_planner_llm_invocations_run_invocation_attempt",
        "planner_llm_invocations",
        type_="unique",
    )
    for name in (
        "finished_at",
        "max_attempts",
        "call_type",
        "user_prompt",
        "system_prompt",
        "control_payload",
        "input_payload",
        "subject",
        "call_index",
        "invocation_id",
    ):
        op.drop_column("planner_llm_invocations", name)
