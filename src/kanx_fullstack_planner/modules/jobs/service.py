from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from temporalio.client import (
    Client,
    Schedule,
    ScheduleActionStartWorkflow,
    ScheduleCalendarSpec,
    ScheduleOverlapPolicy,
    SchedulePolicy,
    ScheduleRange,
    ScheduleSpec,
    ScheduleUpdate,
)

from .models import (
    Job,
    JobBatch,
    JobPriority,
    JobSchedule,
    JobStatus,
    OutboxMessage,
    TenantJobQuota,
)
from .settings import get_jobs_settings
from .workflows import ScheduleEnqueueWorkflow


class JobConflictError(ValueError):
    pass


class JobQuotaExceededError(JobConflictError):
    def __init__(self, code: str, retry_after_seconds: int) -> None:
        super().__init__(code)
        self.code = code
        self.retry_after_seconds = retry_after_seconds


class RetryableJobError(RuntimeError):
    def __init__(self, message: str, *, code: str = "retryable_error") -> None:
        super().__init__(message)
        self.code = code


class PermanentJobError(RuntimeError):
    def __init__(self, message: str, *, code: str = "permanent_error") -> None:
        super().__init__(message)
        self.code = code


def _payload_hash(payload: dict[str, Any]) -> str:
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(serialized.encode()).hexdigest()


class JobService:
    async def enqueue(
        self,
        session: AsyncSession,
        *,
        job_type: str,
        payload: dict[str, Any],
        idempotency_key: str,
        organization_id: str | None = None,
        created_by: str | None = None,
        available_at: datetime | None = None,
        job_version: str = "1.0.0",
        priority: JobPriority | str = JobPriority.NORMAL,
        batch_id: str | None = None,
        maximum_attempts: int = 5,
        timeout_seconds: int = 300,
        initial_backoff_seconds: int = 1,
        maximum_backoff_seconds: int = 300,
        backoff_coefficient: int = 2,
        concurrency_limit: int | None = None,
    ) -> Job:
        if organization_id is not None:
            await self._check_enqueue_quota(session, organization_id)
        scope = organization_id or "global"
        digest = _payload_hash(payload)
        criteria = (
            Job.idempotency_scope == scope,
            Job.job_type == job_type,
            Job.idempotency_key == idempotency_key,
        )
        existing: Job | None = await session.scalar(select(Job).where(*criteria))
        if existing is not None:
            if existing.payload_hash != digest:
                raise JobConflictError("Idempotency key was already used with a different payload")
            return existing
        job = Job(
            organization_id=organization_id,
            created_by=created_by,
            job_type=job_type,
            job_version=job_version,
            payload=payload,
            payload_hash=digest,
            idempotency_scope=scope,
            idempotency_key=idempotency_key,
            available_at=available_at or datetime.now(UTC),
            priority=str(priority),
            batch_id=batch_id,
            maximum_attempts=maximum_attempts,
            timeout_seconds=timeout_seconds,
            initial_backoff_seconds=initial_backoff_seconds,
            maximum_backoff_seconds=maximum_backoff_seconds,
            backoff_coefficient=backoff_coefficient,
            concurrency_limit=concurrency_limit,
        )
        try:
            async with session.begin_nested():
                session.add(job)
                await session.flush()
        except IntegrityError as error:
            existing = await session.scalar(select(Job).where(*criteria))
            if existing is None:
                raise
            if existing.payload_hash != digest:
                raise JobConflictError(
                    "Idempotency key was already used with a different payload"
                ) from error
            return existing
        session.add(
            OutboxMessage(
                aggregate_id=job.id,
                payload={"job_id": job.id, "attempt": job.attempt},
                available_at=job.available_at,
            )
        )
        return job

    async def _check_enqueue_quota(self, session: AsyncSession, organization_id: str) -> None:
        quota = await session.get(TenantJobQuota, organization_id)
        max_queued = quota.max_queued if quota else 1000
        hourly_limit = quota.max_submissions_per_hour if quota else 1000
        queued = await session.scalar(
            select(func.count())
            .select_from(Job)
            .where(
                Job.organization_id == organization_id,
                Job.status.in_((JobStatus.PENDING, JobStatus.DISPATCHED)),
            )
        )
        if int(queued or 0) >= max_queued:
            raise JobQuotaExceededError("job_queue_quota_exceeded", 60)
        recent = await session.scalar(
            select(func.count())
            .select_from(Job)
            .where(
                Job.organization_id == organization_id,
                Job.created_at >= datetime.now(UTC) - timedelta(hours=1),
            )
        )
        if int(recent or 0) >= hourly_limit:
            raise JobQuotaExceededError("job_submission_rate_exceeded", 3600)

    async def retry(self, session: AsyncSession, job: Job) -> Job:
        if job.status not in (
            JobStatus.FAILED,
            JobStatus.CANCELLED,
            JobStatus.DEAD_LETTERED,
        ):
            raise JobConflictError("Only failed or cancelled jobs can be retried")
        job.attempt += 1
        job.generation = (job.generation or 1) + 1
        job.status = JobStatus.PENDING
        job.error = None
        job.result = None
        job.error_code = None
        job.error_class = None
        job.dead_lettered_at = None
        job.updated_at = datetime.now(UTC)
        session.add(
            OutboxMessage(
                aggregate_id=job.id,
                payload={"job_id": job.id, "attempt": job.attempt},
            )
        )
        return job

    async def redrive(self, session: AsyncSession, job: Job) -> Job:
        if job.status != JobStatus.DEAD_LETTERED:
            raise JobConflictError("Only dead-lettered jobs can be redriven")
        return await self.retry(session, job)

    async def create_batch(
        self,
        session: AsyncSession,
        *,
        organization_id: str,
        created_by: str,
        items: list[dict[str, Any]],
    ) -> tuple[JobBatch, list[Job]]:
        if not items or len(items) > get_jobs_settings().max_batch_size:
            raise ValueError("Batch size is outside the allowed range")
        batch = JobBatch(organization_id=organization_id, created_by=created_by, total=len(items))
        session.add(batch)
        await session.flush()
        jobs = []
        for index, item in enumerate(items):
            jobs.append(
                await self.enqueue(
                    session,
                    job_type=str(item["job_type"]),
                    job_version=str(item.get("job_version", "1.0.0")),
                    payload=dict(item.get("payload", {})),
                    idempotency_key=str(item.get("idempotency_key", f"{batch.id}:{index}")),
                    organization_id=organization_id,
                    created_by=created_by,
                    priority=str(item.get("priority", JobPriority.NORMAL)),
                    batch_id=batch.id,
                )
            )
        return batch, jobs

    async def cancel(self, job: Job) -> Job:
        if job.status in (JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED):
            return job
        job.status = (
            JobStatus.CANCELLED if job.status == JobStatus.PENDING else JobStatus.CANCELLING
        )
        job.updated_at = datetime.now(UTC)
        return job

    async def create_schedule(
        self,
        session: AsyncSession,
        client: Client,
        *,
        name: str,
        cron: str = "",
        job_type: str,
        payload: dict[str, Any],
        task_queue: str,
        organization_id: str | None = None,
        timezone: str = "UTC",
        calendar: dict[str, Any] | None = None,
        exclusions: list[dict[str, Any]] | None = None,
        overlap_policy: str = "skip",
        catchup_window_seconds: int = 900,
        jitter_seconds: int = 0,
    ) -> JobSchedule:
        try:
            ZoneInfo(timezone)
        except ZoneInfoNotFoundError as error:
            raise ValueError("Unknown IANA timezone") from error
        if bool(cron) == bool(calendar):
            raise ValueError("Exactly one of cron or calendar is required")
        record = JobSchedule(
            temporal_schedule_id="pending",
            organization_id=organization_id,
            name=name,
            cron=cron,
            job_type=job_type,
            payload=payload,
            timezone=timezone,
            calendar=calendar,
            exclusions=exclusions or [],
            overlap_policy=overlap_policy,
            catchup_window_seconds=catchup_window_seconds,
            jitter_seconds=jitter_seconds,
        )
        session.add(record)
        await session.flush()
        record.temporal_schedule_id = f"job-schedule/{record.id}"
        await client.create_schedule(
            record.temporal_schedule_id,
            self._schedule(record, task_queue),
        )
        return record

    async def update_schedule(
        self,
        record: JobSchedule,
        client: Client,
        *,
        task_queue: str,
        cron: str | None = None,
        payload: dict[str, Any] | None = None,
        timezone: str | None = None,
        calendar: dict[str, Any] | None = None,
        exclusions: list[dict[str, Any]] | None = None,
        overlap_policy: str | None = None,
        catchup_window_seconds: int | None = None,
        jitter_seconds: int | None = None,
    ) -> JobSchedule:
        if cron is not None:
            record.cron = cron
        if payload is not None:
            record.payload = payload
        if timezone is not None:
            try:
                ZoneInfo(timezone)
            except ZoneInfoNotFoundError as error:
                raise ValueError("Unknown IANA timezone") from error
            record.timezone = timezone
        if calendar is not None:
            record.calendar = calendar
            record.cron = ""
        if exclusions is not None:
            record.exclusions = exclusions
        if overlap_policy is not None:
            record.overlap_policy = overlap_policy
        if catchup_window_seconds is not None:
            record.catchup_window_seconds = catchup_window_seconds
        if jitter_seconds is not None:
            record.jitter_seconds = jitter_seconds
        record.updated_at = datetime.now(UTC)
        schedule = self._schedule(record, task_queue)
        await client.get_schedule_handle(record.temporal_schedule_id).update(
            lambda _input: ScheduleUpdate(schedule)
        )
        return record

    async def pause_schedule(
        self, record: JobSchedule, client: Client, note: str = ""
    ) -> JobSchedule:
        await client.get_schedule_handle(record.temporal_schedule_id).pause(note=note)
        record.paused = True
        record.updated_at = datetime.now(UTC)
        return record

    async def resume_schedule(
        self, record: JobSchedule, client: Client, note: str = ""
    ) -> JobSchedule:
        await client.get_schedule_handle(record.temporal_schedule_id).unpause(note=note)
        record.paused = False
        record.updated_at = datetime.now(UTC)
        return record

    async def delete_schedule(
        self, session: AsyncSession, record: JobSchedule, client: Client
    ) -> None:
        await client.get_schedule_handle(record.temporal_schedule_id).delete()
        await session.delete(record)

    @staticmethod
    def _schedule(record: JobSchedule, task_queue: str) -> Schedule:
        overlap = {
            "skip": ScheduleOverlapPolicy.SKIP,
            "buffer_one": ScheduleOverlapPolicy.BUFFER_ONE,
            "buffer_all": ScheduleOverlapPolicy.BUFFER_ALL,
            "cancel_other": ScheduleOverlapPolicy.CANCEL_OTHER,
            "terminate_other": ScheduleOverlapPolicy.TERMINATE_OTHER,
            "allow_all": ScheduleOverlapPolicy.ALLOW_ALL,
        }.get(record.overlap_policy)
        if overlap is None:
            raise ValueError("Unknown schedule overlap policy")
        return Schedule(
            action=ScheduleActionStartWorkflow(
                ScheduleEnqueueWorkflow.run,
                {
                    "schedule_id": record.id,
                    "job_type": record.job_type,
                    "payload": record.payload,
                    "organization_id": record.organization_id,
                },
                id=f"schedule-trigger/{record.id}",
                task_queue=task_queue,
            ),
            spec=ScheduleSpec(
                cron_expressions=[record.cron] if record.cron else [],
                calendars=([JobService._calendar(record.calendar)] if record.calendar else []),
                skip=[JobService._calendar(item) for item in record.exclusions],
                jitter=timedelta(seconds=record.jitter_seconds),
                time_zone_name=record.timezone,
            ),
            policy=SchedulePolicy(
                overlap=overlap,
                catchup_window=timedelta(seconds=record.catchup_window_seconds),
            ),
        )

    @staticmethod
    def _calendar(value: dict[str, Any]) -> ScheduleCalendarSpec:
        fields: dict[str, Any] = {}
        for key in ("second", "minute", "hour", "day_of_month", "month", "year", "day_of_week"):
            if key in value:
                fields[key] = [
                    ScheduleRange(**item) if isinstance(item, dict) else ScheduleRange(int(item))
                    for item in value[key]
                ]
        fields["comment"] = value.get("comment")
        return ScheduleCalendarSpec(**fields)
