from ...core.modules import JobTypeSpec, MigrationDescriptor, ModuleSpec
from .admin_api import router as admin_router
from .api import router
from .models import (
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
from .service import execute_job
from .settings import get_planner_settings

module = ModuleSpec(
    name="planner",
    requires=("database", "temporal", "ai", "jobs"),
    routers=(router, admin_router),
    models=(
        RawRequirement,
        RawRequirementRevision,
        WorkflowRun,
        WorkflowStep,
        LLMInvocation,
        ValidationFinding,
        Requirement,
        RequirementRevision,
        RequirementDependency,
    ),
    migrations=(MigrationDescriptor("planner"),),
    permissions=("planner:read", "planner:write", "planner:cancel"),
    job_handlers=(
        JobTypeSpec(
            name="planner.raw_to_agile",
            handler=execute_job,
            timeout_seconds=900,
            maximum_attempts=get_planner_settings().activity_max_attempts,
        ),
    ),
    settings_factory=get_planner_settings,
    optional_env=("TEMPORAL_ACTIVITY_MAX_RETRIES",),
)
