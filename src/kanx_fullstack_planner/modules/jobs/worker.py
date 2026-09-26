from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio import activity
from temporalio.exceptions import ApplicationError

from ...core.modules import JobHandlerSpec
from .models import Job, JobAttempt, JobBatch, JobExecutionLease, JobStatus, JobTypeVersion
from .notifications import emit_job_event
from .service import JobService, PermanentJobError, RetryableJobError
from .settings import get_jobs_settings


async def sync_job_types(
    factory: async_sessionmaker[AsyncSession], handlers: tuple[JobHandlerSpec, ...]
) -> None:
    async with factory() as session:
        for item in handlers:
            registered = await session.scalar(
                select(JobTypeVersion).where(
                    JobTypeVersion.name == item.name,
                    JobTypeVersion.version == item.version,
                )
            )
            policy = {
                "timeout_seconds": item.timeout_seconds,
                "maximum_attempts": item.maximum_attempts,
                "initial_backoff_seconds": item.initial_backoff_seconds,
                "maximum_backoff_seconds": item.maximum_backoff_seconds,
                "backoff_coefficient": item.backoff_coefficient,
                "default_priority": item.default_priority,
                "concurrency_limit": item.concurrency_limit,
            }
            if registered is None:
                session.add(JobTypeVersion(name=item.name, version=item.version, policy=policy))
            else:
                registered.enabled = True
                registered.policy = policy
        await session.commit()


async def _update_batch(session: AsyncSession, job: Job) -> None:
    if not job.batch_id:
        return
    batch = await session.get(JobBatch, job.batch_id)
    if batch is None:
        return
    statuses = (
        JobStatus.SUCCEEDED,
        JobStatus.FAILED,
        JobStatus.DEAD_LETTERED,
        JobStatus.CANCELLED,
    )
    counts = {
        status: int(
            await session.scalar(
                select(func.count())
                .select_from(Job)
                .where(Job.batch_id == batch.id, Job.status == status)
            )
            or 0
        )
        for status in statuses
    }
    batch.succeeded = counts[JobStatus.SUCCEEDED]
    batch.failed = counts[JobStatus.FAILED] + counts[JobStatus.DEAD_LETTERED]
    batch.cancelled = counts[JobStatus.CANCELLED]
    batch.updated_at = datetime.now(UTC)
    if batch.succeeded + batch.failed + batch.cancelled >= batch.total:
        batch.status = "completed"
        await emit_job_event(
            session,
            job,
            "batch.completed",
            {
                "batch_id": batch.id,
                "succeeded": batch.succeeded,
                "failed": batch.failed,
                "cancelled": batch.cancelled,
            },
        )


def build_job_activity(
    factory: async_sessionmaker[AsyncSession], handlers: tuple[JobHandlerSpec, ...]
) -> Any:
    registry = {(item.name, item.version): item.handler for item in handlers}

    @activity.defn(name="jobs.execute")
    async def execute(input: dict[str, Any]) -> dict[str, Any] | None:
        job_id = str(input["job_id"])
        generation_attempt = max(1, int(input["attempt"]))
        try:
            temporal_attempt = activity.info().attempt
            in_activity_context = True
        except RuntimeError:
            temporal_attempt = int(input.get("temporal_attempt", 1))
            in_activity_context = False
        async with factory() as session:
            job = await session.scalar(select(Job).where(Job.id == job_id).with_for_update())
            if job is None:
                raise RuntimeError(f"Job not found: {job_id}")
            if job.status == JobStatus.CANCELLING:
                job.status = JobStatus.CANCELLED
                await session.commit()
                return None
            handler = registry.get((job.job_type, job.job_version or "1.0.0"))
            if handler is None:
                message = f"No handler registered for {job.job_type!r}@{job.job_version}"
                if not in_activity_context:
                    raise RuntimeError(message)
                raise ApplicationError(
                    message,
                    type="handler_not_registered",
                    non_retryable=True,
                )
            job.status = JobStatus.RUNNING
            job.updated_at = datetime.now(UTC)
            attempt = await session.scalar(
                select(JobAttempt).where(
                    JobAttempt.job_id == job.id,
                    JobAttempt.attempt
                    == (generation_attempt - 1) * 1000 + temporal_attempt,
                )
            )
            if attempt is None:
                attempt = JobAttempt(
                    job_id=job.id,
                    attempt=(generation_attempt - 1) * 1000 + temporal_attempt,
                    workflow_id=(
                        f"job/{job.id}/{generation_attempt}/attempt/{temporal_attempt}"
                    ),
                )
                session.add(attempt)
            attempt.started_at = attempt.started_at or datetime.now(UTC)
            await session.commit()

        async def heartbeat() -> None:
            while True:
                activity.heartbeat(job_id)
                async with factory() as heartbeat_session:
                    lease = await heartbeat_session.get(JobExecutionLease, job_id)
                    if lease is not None:
                        now = datetime.now(UTC)
                        lease.heartbeat_at = now
                        lease.expires_at = now + timedelta(
                            seconds=get_jobs_settings().lease_seconds
                        )
                        await heartbeat_session.commit()
                await asyncio.sleep(5)

        heartbeat_task = asyncio.create_task(heartbeat()) if in_activity_context else None
        try:
            result = await handler(job.id, dict(job.payload))
        except asyncio.CancelledError:
            async with factory() as session:
                current = await session.get(Job, job_id)
                attempt = await session.scalar(
                    select(JobAttempt).where(
                        JobAttempt.job_id == job_id,
                        JobAttempt.attempt
                        == (generation_attempt - 1) * 1000 + temporal_attempt,
                    )
                )
                if current is not None:
                    current.status = JobStatus.CANCELLED
                    current.updated_at = datetime.now(UTC)
                    if in_activity_context:
                        await emit_job_event(session, current, "job.cancelled", {})
                        await _update_batch(session, current)
                lease = await session.get(JobExecutionLease, job_id)
                if isinstance(lease, JobExecutionLease):
                    await session.delete(lease)
                if attempt is not None:
                    attempt.finished_at = datetime.now(UTC)
                    attempt.error = "cancelled"
                await session.commit()
            raise
        except Exception as error:
            permanent = isinstance(error, PermanentJobError)
            retryable = isinstance(error, RetryableJobError) or not permanent
            exhausted = temporal_attempt >= (job.maximum_attempts or 5)
            error_code = getattr(error, "code", type(error).__name__)
            async with factory() as session:
                current = await session.get(Job, job_id)
                attempt = await session.scalar(
                    select(JobAttempt).where(
                        JobAttempt.job_id == job_id,
                        JobAttempt.attempt
                        == (generation_attempt - 1) * 1000 + temporal_attempt,
                    )
                )
                if current is not None:
                    current.status = (
                        JobStatus.DEAD_LETTERED
                        if in_activity_context and (permanent or exhausted)
                        else JobStatus.FAILED
                        if not in_activity_context
                        else JobStatus.DISPATCHED
                    )
                    current.error = str(error)[:2000]
                    current.error_code = str(error_code)[:128]
                    current.error_class = "permanent" if permanent else "retryable"
                    if in_activity_context and (permanent or exhausted):
                        current.dead_lettered_at = datetime.now(UTC)
                        await emit_job_event(
                            session,
                            current,
                            "job.dead_lettered",
                            {"code": current.error_code},
                        )
                        lease = await session.get(JobExecutionLease, job_id)
                        if isinstance(lease, JobExecutionLease):
                            await session.delete(lease)
                        await _update_batch(session, current)
                    elif in_activity_context:
                        await emit_job_event(
                            session,
                            current,
                            "job.retrying",
                            {"code": current.error_code, "attempt": temporal_attempt},
                        )
                    current.updated_at = datetime.now(UTC)
                if attempt is not None:
                    attempt.finished_at = datetime.now(UTC)
                    attempt.error = str(error)[:2000]
                    attempt.error_code = str(error_code)[:128]
                    attempt.error_class = "permanent" if permanent else "retryable"
                await session.commit()
            if not in_activity_context:
                raise
            raise ApplicationError(
                str(error),
                type=str(error_code),
                non_retryable=permanent or not retryable,
            ) from error
        finally:
            if heartbeat_task is not None:
                heartbeat_task.cancel()
                with suppress(asyncio.CancelledError):
                    await heartbeat_task
        async with factory() as session:
            current = await session.get(Job, job_id)
            attempt = await session.scalar(
                select(JobAttempt).where(
                    JobAttempt.job_id == job_id,
                    JobAttempt.attempt
                    == (generation_attempt - 1) * 1000 + temporal_attempt,
                )
            )
            if current is not None:
                current.status = JobStatus.SUCCEEDED
                current.result = result
                current.error = None
                current.error_code = None
                current.error_class = None
                current.updated_at = datetime.now(UTC)
                if in_activity_context:
                    await emit_job_event(session, current, "job.succeeded", {"result": result})
                    await _update_batch(session, current)
                lease = await session.get(JobExecutionLease, job_id)
                if isinstance(lease, JobExecutionLease):
                    await session.delete(lease)
            if attempt is not None:
                attempt.finished_at = datetime.now(UTC)
                attempt.error = None
            await session.commit()
        return result

    return execute


def build_schedule_activity(factory: async_sessionmaker[AsyncSession]) -> Any:
    @activity.defn(name="jobs.enqueue_scheduled")
    async def enqueue(input: dict[str, Any]) -> str:
        info = activity.info()
        key = f"{input['schedule_id']}:{info.workflow_run_id}"
        async with factory() as session:
            job = await JobService().enqueue(
                session,
                job_type=str(input["job_type"]),
                payload=dict(input["payload"]),
                idempotency_key=key,
                organization_id=input.get("organization_id"),
            )
            await session.commit()
            return job.id

    return enqueue
