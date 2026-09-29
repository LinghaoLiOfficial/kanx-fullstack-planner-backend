"""planner workflow and requirement assets."""

from collections.abc import Sequence

from alembic import op
from kanx_fullstack_planner.modules.planner.models import (
    LLMInvocation,
    RawRequirement,
    RawRequirementRevision,
    Requirement,
    RequirementDependency,
    RequirementRevision,
    ValidationFinding,
    WorkflowRun,
    WorkflowStep,
)

revision: str = "planner_0004"
down_revision: str | None = "foundation_0003_jobs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = (
    RawRequirement,
    RawRequirementRevision,
    WorkflowRun,
    WorkflowStep,
    LLMInvocation,
    ValidationFinding,
    Requirement,
    RequirementRevision,
    RequirementDependency,
)


def upgrade() -> None:
    bind = op.get_bind()
    for model in TABLES:
        model.__table__.create(bind)


def downgrade() -> None:
    bind = op.get_bind()
    for model in reversed(TABLES):
        model.__table__.drop(bind)
