from __future__ import annotations

from datetime import timedelta
from typing import Any, cast

from temporalio import workflow
from temporalio.common import RetryPolicy


@workflow.defn
class JobWorkflow:
    @workflow.run
    async def run(self, input: dict[str, Any]) -> dict[str, Any] | None:
        result = await workflow.execute_activity(
            "jobs.execute",
            input,
            start_to_close_timeout=timedelta(seconds=int(input.get("timeout_seconds", 300))),
            heartbeat_timeout=timedelta(seconds=10),
            retry_policy=RetryPolicy(
                initial_interval=timedelta(seconds=int(input.get("initial_backoff_seconds", 1))),
                maximum_interval=timedelta(seconds=int(input.get("maximum_backoff_seconds", 300))),
                backoff_coefficient=float(input.get("backoff_coefficient", 2)),
                maximum_attempts=int(input.get("maximum_attempts", 5)),
            ),
        )
        return cast(dict[str, Any] | None, result)


@workflow.defn
class ScheduleEnqueueWorkflow:
    @workflow.run
    async def run(self, input: dict[str, Any]) -> str:
        result = await workflow.execute_activity(
            "jobs.enqueue_scheduled",
            input,
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy(maximum_attempts=5),
        )
        return cast(str, result)
