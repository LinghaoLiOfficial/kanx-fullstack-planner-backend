"""independent audit event storage"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "foundation_0002_database"
down_revision: str | None = "foundation_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "audit_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("action", sa.String(160), nullable=False),
        sa.Column("outcome", sa.String(32), nullable=False),
        sa.Column("user_id", sa.String(36), nullable=True),
        sa.Column("organization_id", sa.String(36), nullable=True),
        sa.Column("request_id", sa.String(128), nullable=False),
        sa.Column("trace_id", sa.String(64), nullable=False),
        sa.Column("ip_address", sa.String(64), nullable=True),
        sa.Column("resource_type", sa.String(80), nullable=True),
        sa.Column("resource_id", sa.String(160), nullable=True),
        sa.Column("details", sa.JSON(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
    )
    for column in ("occurred_at", "action", "user_id", "organization_id"):
        op.create_index(f"ix_audit_events_{column}", "audit_events", [column])


def downgrade() -> None:
    op.drop_table("audit_events")
