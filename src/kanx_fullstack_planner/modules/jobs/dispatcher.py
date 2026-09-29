from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from temporalio.client import Client
from temporalio.exceptions import WorkflowAlreadyStartedError

from ...core.config import Settings, get_settings
from .models import Job, JobExecutionLease, JobStatus, OutboxMessage, TenantJobQuota
from .notifications import dispatch_webhooks_once
from .settings import get_jobs_settings
from .workflows import JobWorkflow


async def dispatch_once(settings: Settings, client: Client) -> int:
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    dispatched = 0
    try:
        async with factory() as session:
            now = datetime.now(UTC)
            supports_admission = hasattr(session, "execute")
            if supports_admission:
                await session.execute(
                    delete(JobExecutionLease).where(JobExecutionLease.expires_at <= now)
                )
            messages = list(
                (
                    await session.scalars(
                        select(OutboxMessage)
                        .join(Job, Job.id == OutboxMessage.aggregate_id)
                        .where(
                            OutboxMessage.dispatched_at.is_(None),
                            OutboxMessage.available_at <= datetime.now(UTC),
                        )
                        .order_by(
                            (Job.priority == "critical").desc(),
                            (Job.priority == "high").desc(),
                            (Job.priority == "normal").desc(),
                            OutboxMessage.created_at,
                        )
                        .limit(get_jobs_settings().batch_size)
                        .with_for_update(skip_locked=True)
                    )
                ).all()
            )
            for message in messages:
                job = await session.get(Job, message.aggregate_id)
                if job is None:
                    message.dispatched_at = datetime.now(UTC)
                    continue
                global_running = (
                    await session.scalar(select(func.count()).select_from(JobExecutionLease))
                    if supports_admission
                    else 0
                )
                if int(global_running or 0) >= get_jobs_settings().global_concurrency:
                    continue
                if supports_admission and job.organization_id:
                    quota = await session.get(TenantJobQuota, job.organization_id)
                    tenant_limit = (
                        quota.max_running if quota else get_jobs_settings().tenant_concurrency
                    )
                    tenant_running = await session.scalar(
                        select(func.count())
                        .select_from(JobExecutionLease)
                        .where(JobExecutionLease.organization_id == job.organization_id)
                    )
                    if int(tenant_running or 0) >= tenant_limit:
                        continue
                if supports_admission and job.concurrency_limit:
                    type_running = await session.scalar(
                        select(func.count())
                        .select_from(JobExecutionLease)
                        .where(JobExecutionLease.job_type == job.job_type)
                    )
                    if int(type_running or 0) >= job.concurrency_limit:
                        continue
                workflow_id = f"job/{job.id}/{job.attempt}"
                if supports_admission:
                    lease = await session.get(JobExecutionLease, job.id)
                    if lease is None:
                        lease = JobExecutionLease(job_id=job.id)
                        session.add(lease)
                    lease.organization_id = job.organization_id
                    lease.job_type = job.job_type
                    lease.owner = workflow_id
                    lease.expires_at = now + timedelta(seconds=get_jobs_settings().lease_seconds)
                    lease.heartbeat_at = now
                    await session.flush()
                try:
                    await client.start_workflow(
                        JobWorkflow.run,
                        {
                            "job_id": job.id,
                            "attempt": job.attempt,
                            "timeout_seconds": job.timeout_seconds or 300,
                            "maximum_attempts": job.maximum_attempts or 5,
                            "initial_backoff_seconds": job.initial_backoff_seconds or 1,
                            "maximum_backoff_seconds": job.maximum_backoff_seconds or 300,
                            "backoff_coefficient": job.backoff_coefficient or 2,
                        },
                        id=workflow_id,
                        task_queue=settings.temporal_task_queue,
                    )
                except WorkflowAlreadyStartedError:
                    pass
                if job.status == JobStatus.PENDING:
                    job.status = JobStatus.DISPATCHED
                    job.updated_at = datetime.now(UTC)
                message.dispatched_at = datetime.now(UTC)
                dispatched += 1
            await session.commit()
    finally:
        await engine.dispose()
    return dispatched


async def run_dispatcher(settings: Settings | None = None) -> None:
    configured = settings or get_settings()
    client = await Client.connect(configured.temporal_host, namespace=configured.temporal_namespace)
    while True:
        await dispatch_once(configured, client)
        await dispatch_webhooks_once(configured)
        await asyncio.sleep(get_jobs_settings().poll_interval_seconds)


if __name__ == "__main__":
    try:
        asyncio.run(run_dispatcher())
    except KeyboardInterrupt:
        pass
