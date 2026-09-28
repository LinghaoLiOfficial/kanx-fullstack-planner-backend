from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..database.session import get_session
from ..jobs.models import Job, JobStatus
from ..jobs.service import JobService
from .models import Requirement, RequirementRevision, WorkflowRun, WorkflowStep
from .schemas import RawRequirementInput
from .service import PlannerService
from .settings import get_planner_settings

router = APIRouter(prefix="/v1", tags=["planner"])


@router.post(
    "/projects/{project_id}/requirements/orchestrations", status_code=status.HTTP_202_ACCEPTED
)
async def create_orchestration(
    project_id: str,
    request: RawRequirementInput,
    session: AsyncSession = Depends(get_session),  # noqa: B008
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    key = idempotency_key or f"{project_id}:{request.raw_text[:64]}"
    existing_job = await session.scalar(
        select(Job).where(
            Job.job_type == "planner.raw_to_agile",
            Job.idempotency_scope == "global",
            Job.idempotency_key == key,
        )
    )
    if existing_job is not None:
        run = await session.scalar(select(WorkflowRun).where(WorkflowRun.job_id == existing_job.id))
        if run is not None:
            return {"run_id": run.id, "job_id": existing_job.id, "status": run.status}
    planner = PlannerService()
    run = await planner.create_run(session, project_id, request)
    job = await JobService().enqueue(
        session,
        job_type="planner.raw_to_agile",
        job_version="1.0.0",
        payload={"run_id": run.id},
        idempotency_key=key,
        maximum_attempts=get_planner_settings().activity_max_attempts,
        timeout_seconds=900,
    )
    run.job_id = job.id
    await session.commit()
    return {"run_id": run.id, "job_id": job.id, "status": "queued"}


@router.get("/orchestrations/{run_id}")
async def get_orchestration(
    run_id: str,
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> dict[str, Any]:
    run = await session.get(WorkflowRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Workflow run not found")
    return run.result or {"run_id": run.id, "status": run.status, "job_id": run.job_id}


@router.get("/orchestrations/{run_id}/steps")
async def get_orchestration_steps(
    run_id: str,
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> list[dict[str, Any]]:
    if await session.get(WorkflowRun, run_id) is None:
        raise HTTPException(status_code=404, detail="Workflow run not found")
    rows = (
        await session.scalars(
            select(WorkflowStep).where(WorkflowStep.run_id == run_id).order_by(WorkflowStep.id)
        )
    ).all()
    return [
        {
            "step_key": row.step_key,
            "status": row.status,
            "attempt": row.attempt,
            "input_hash": row.input_hash,
            "output_hash": row.output_hash,
            "error": row.error,
        }
        for row in rows
    ]


@router.post("/orchestrations/{run_id}/cancel")
async def cancel_orchestration(
    run_id: str,
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> dict[str, str]:
    run = await session.get(WorkflowRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Workflow run not found")
    if run.job_id:
        job = await session.get(Job, run.job_id)
        if job and job.status in (JobStatus.PENDING, JobStatus.DISPATCHED, JobStatus.RUNNING):
            job.status = JobStatus.CANCELLING
    await session.commit()
    return {"run_id": run_id, "status": "cancelling"}


@router.get("/projects/{project_id}/requirements")
async def list_requirements(
    project_id: str,
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> list[dict[str, Any]]:
    rows = (
        await session.scalars(
            select(Requirement)
            .where(Requirement.project_id == project_id)
            .order_by(Requirement.requirement_key)
        )
    ).all()
    return [
        {
            "requirement_key": row.requirement_key,
            "status": row.status,
            "current_revision": row.current_revision,
        }
        for row in rows
    ]


@router.get("/projects/{project_id}/requirements/{requirement_key}/history")
async def requirement_history(
    project_id: str,
    requirement_key: str,
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> list[dict[str, Any]]:
    requirement = await session.scalar(
        select(Requirement).where(
            Requirement.project_id == project_id, Requirement.requirement_key == requirement_key
        )
    )
    if requirement is None:
        raise HTTPException(status_code=404, detail="Requirement not found")
    rows = (
        await session.scalars(
            select(RequirementRevision)
            .where(RequirementRevision.requirement_id == requirement.id)
            .order_by(RequirementRevision.revision)
        )
    ).all()
    return [
        {
            "revision": row.revision,
            "payload": row.payload,
            "content_hash": row.content_hash,
            "run_id": row.run_id,
        }
        for row in rows
    ]
