from ...core.modules import MigrationDescriptor, ModuleSpec
from .models import (
    Job,
    JobAttempt,
    JobBatch,
    JobEvent,
    JobExecutionLease,
    JobSchedule,
    JobTypeVersion,
    JobWebhook,
    OutboxMessage,
    PlatformRoleAssignment,
    TenantJobQuota,
    WebhookDelivery,
)
from .settings import get_jobs_settings
from .workflows import JobWorkflow, ScheduleEnqueueWorkflow

module = ModuleSpec(
    name="jobs",
    requires=("database", "temporal"),
    models=(
        Job,
        JobAttempt,
        OutboxMessage,
        JobSchedule,
        JobTypeVersion,
        TenantJobQuota,
        JobExecutionLease,
        JobBatch,
        JobEvent,
        JobWebhook,
        WebhookDelivery,
        PlatformRoleAssignment,
    ),
    migrations=(MigrationDescriptor("jobs"),),
    workflows=(JobWorkflow, ScheduleEnqueueWorkflow),
    permissions=(
        "jobs:read",
        "jobs:cancel",
        "jobs:retry",
        "jobs:manage",
        "jobs:schedules:manage",
        "jobs:webhooks:manage",
        "platform:jobs:*",
    ),
    settings_factory=get_jobs_settings,
    optional_env=(
        "JOBS_POLL_INTERVAL_SECONDS",
        "JOBS_BATCH_SIZE",
        "JOBS_ACTIVITY_TIMEOUT_SECONDS",
        "JOBS_MAXIMUM_ATTEMPTS",
    ),
    optional_dependencies=("temporalio>=1.18,<2",),
)
