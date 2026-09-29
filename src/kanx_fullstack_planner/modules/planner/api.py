from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.api import current_user
from ..database.session import get_session
from ..jobs.models import Job, JobStatus
from ..jobs.service import JobService
from ..projects.api import owned
from ..projects.models import Project
from ..users.models import User
from .graph import PLANNER_STAGES
from .models import (
    RawRequirement,
    RawRequirementRevision,
    Requirement,
    RequirementRevision,
    WorkflowRun,
    WorkflowStep,
)
from .schemas import RawRequirementInput
from .service import PlannerService
from .settings import get_planner_settings

router = APIRouter(prefix="/v1", tags=["planner"])


def _generation_progress(
    run: WorkflowRun | None, job: Job | None, steps: list[WorkflowStep]
) -> dict[str, Any] | None:
    if run is None:
        return None
    status = str(run.status)
    error = run.error
    if job and job.status in (JobStatus.FAILED, JobStatus.DEAD_LETTERED, JobStatus.ABANDONED):
        status = "failed"
        error = error or job.error
    elif job and job.status == JobStatus.CANCELLED:
        status = "cancelled"

    completed = sum(step.status in ("succeeded", "skipped") for step in steps)
    total = len(PLANNER_STAGES)
    if status in ("succeeded", "needs_clarification"):
        progress = 100
    elif status in ("failed", "cancelled"):
        progress = round(completed / total * 100) if total else 0
    elif status == "running":
        progress = max(10, min(95, round(completed / total * 90) + 10)) if total else 10
    else:
        progress = 5

    public_status = (
        "succeeded"
        if status in ("succeeded", "needs_clarification")
        else "failed"
        if status in ("failed", "cancelled")
        else "running"
    )
    messages = {
        "queued": "已保存，等待生成敏捷业务需求",
        "running": "正在生成敏捷业务需求",
        "needs_clarification": "生成完成，需要补充澄清信息",
        "succeeded": "敏捷业务需求生成完成",
        "failed": "敏捷业务需求生成失败",
        "cancelled": "敏捷业务需求生成已取消",
    }
    return {
        "run_id": run.id,
        "status": public_status,
        "progress": progress,
        "message": messages.get(status, "正在生成敏捷业务需求"),
        "error_message": error,
        "updated_at": run.updated_at,
    }


@router.post(
    "/projects/{project_id}/requirements/orchestrations", status_code=status.HTTP_202_ACCEPTED
)
async def create_orchestration(
    project_id: str,
    request: RawRequirementInput,
    session: AsyncSession = Depends(get_session),  # noqa: B008
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    user: User = Depends(current_user),  # noqa: B008
) -> dict[str, Any]:
    await owned(project_id, user, session)
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
            raw_id = None
            if run.raw_requirement_revision_id:
                raw_revision = await session.get(
                    RawRequirementRevision, run.raw_requirement_revision_id
                )
                raw_id = raw_revision.raw_requirement_id if raw_revision else None
            return {
                "run_id": run.id,
                "job_id": existing_job.id,
                "raw_requirement_id": raw_id,
                "status": run.status,
            }
    planner = PlannerService()
    raw_requirement, raw_revision = await planner.create_raw_requirement(
        session, project_id, request
    )
    run = await planner.create_run(
        session, project_id, request, raw_requirement_revision_id=raw_revision.id
    )
    job = await JobService().enqueue(
        session,
        job_type="planner.raw_to_agile",
        job_version="1.0.0",
        payload={"run_id": run.id},
        idempotency_key=key,
        maximum_attempts=get_planner_settings().activity_max_attempts,
        timeout_seconds=900,
    )
    raw_requirement.status = "processing"
    run.job_id = job.id
    await session.commit()
    return {
        "run_id": run.id,
        "job_id": job.id,
        "raw_requirement_id": raw_requirement.id,
        "status": "queued",
    }


@router.get("/projects/{project_id}/raw-requirements")
async def list_raw_requirements(
    project_id: str,
    session: AsyncSession = Depends(get_session),  # noqa: B008
    user: User = Depends(current_user),  # noqa: B008
) -> list[dict[str, Any]]:
    await owned(project_id, user, session)
    rows = (
        await session.scalars(
            select(RawRequirement)
            .where(RawRequirement.project_id == project_id)
            .order_by(RawRequirement.created_at.desc())
        )
    ).all()
    result: list[dict[str, Any]] = []
    for row in rows:
        revision = await session.scalar(
            select(RawRequirementRevision)
            .where(RawRequirementRevision.raw_requirement_id == row.id)
            .order_by(RawRequirementRevision.revision.desc())
        )
        run = None
        job = None
        steps: list[WorkflowStep] = []
        if revision is not None:
            run = await session.scalar(
                select(WorkflowRun)
                .where(WorkflowRun.raw_requirement_revision_id == revision.id)
                .order_by(WorkflowRun.created_at.desc())
            )
        if run is not None:
            if run.job_id:
                job = await session.get(Job, run.job_id)
            steps = list(
                (
                    await session.scalars(select(WorkflowStep).where(WorkflowStep.run_id == run.id))
                ).all()
            )
        generation = _generation_progress(run, job, steps)
        result.append(
            {
                "id": row.id,
                "project_id": row.project_id,
                "status": row.status,
                "latest_revision": revision.revision if revision else 0,
                "raw_text": revision.description if revision else "",
                "created_at": row.created_at,
                "updated_at": row.updated_at,
                "progress_status": (
                    "success"
                    if generation and generation["status"] == "succeeded"
                    else "failed"
                    if generation and generation["status"] == "failed"
                    else "in_progress"
                ),
                "business_story_generation": generation,
            }
        )
    return result


@router.get("/raw-requirements/{raw_requirement_id}/history")
async def raw_requirement_history(
    raw_requirement_id: str,
    session: AsyncSession = Depends(get_session),  # noqa: B008
    user: User = Depends(current_user),  # noqa: B008
) -> list[dict[str, Any]]:
    raw = await session.get(RawRequirement, raw_requirement_id)
    if (
        raw is None
        or await session.scalar(
            select(Project).where(Project.id == raw.project_id, Project.owner_user_id == user.id)
        )
        is None
    ):
        raise HTTPException(status_code=404, detail="Raw requirement not found")
    rows = (
        await session.scalars(
            select(RawRequirementRevision)
            .where(RawRequirementRevision.raw_requirement_id == raw_requirement_id)
            .order_by(RawRequirementRevision.revision)
        )
    ).all()
    return [
        {"revision": row.revision, "description": row.description, "content_hash": row.content_hash}
        for row in rows
    ]


@router.get("/orchestrations/{run_id}")
async def get_orchestration(
    run_id: str,
    session: AsyncSession = Depends(get_session),  # noqa: B008
    user: User = Depends(current_user),  # noqa: B008
) -> dict[str, Any]:
    run = await session.get(WorkflowRun, run_id)
    if (
        run is None
        or await session.scalar(
            select(Project).where(Project.id == run.project_id, Project.owner_user_id == user.id)
        )
        is None
    ):
        raise HTTPException(status_code=404, detail="Workflow run not found")
    effective_status = run.status
    effective_error = run.error
    if run.job_id:
        job = await session.get(Job, run.job_id)
        if job and job.status in (
            JobStatus.FAILED,
            JobStatus.DEAD_LETTERED,
            JobStatus.ABANDONED,
        ):
            effective_status = "failed"
            effective_error = effective_error or job.error
        elif job and job.status == JobStatus.CANCELLED:
            effective_status = "cancelled"
    if run.result:
        return {
            **run.result,
            "status": effective_status,
            "job_id": run.job_id,
            "error": effective_error,
            "created_at": run.created_at,
            "updated_at": run.updated_at,
        }
    return {
        "run_id": run.id,
        "job_id": run.job_id,
        "status": effective_status,
        "requirements": [],
        "raw_requirement": {},
        "dependency_analysis": None,
        "assumptions": [],
        "ambiguities": [],
        "warnings": [],
        "validation_summary": {},
        "audit": {},
        "error": effective_error,
        "created_at": run.created_at,
        "updated_at": run.updated_at,
    }


@router.get("/orchestrations/{run_id}/steps")
async def get_orchestration_steps(
    run_id: str,
    session: AsyncSession = Depends(get_session),  # noqa: B008
    user: User = Depends(current_user),  # noqa: B008
) -> list[dict[str, Any]]:
    run = await session.get(WorkflowRun, run_id)
    if (
        run is None
        or await session.scalar(
            select(Project).where(Project.id == run.project_id, Project.owner_user_id == user.id)
        )
        is None
    ):
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
    user: User = Depends(current_user),  # noqa: B008
) -> dict[str, str]:
    run = await session.get(WorkflowRun, run_id)
    if (
        run is None
        or await session.scalar(
            select(Project).where(Project.id == run.project_id, Project.owner_user_id == user.id)
        )
        is None
    ):
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
    user: User = Depends(current_user),  # noqa: B008
) -> list[dict[str, Any]]:
    await owned(project_id, user, session)
    rows = (
        await session.scalars(
            select(Requirement)
            .where(Requirement.project_id == project_id)
            .order_by(Requirement.requirement_key)
        )
    ).all()
    result: list[dict[str, Any]] = []
    for row in rows:
        revision = await session.scalar(
            select(RequirementRevision).where(
                RequirementRevision.requirement_id == row.id,
                RequirementRevision.revision == row.current_revision,
            )
        )
        result.append(
            {
                "requirement_key": row.requirement_key,
                "id": row.id,
                "project_id": row.project_id,
                "status": row.status,
                "current_revision": row.current_revision,
                "asset": revision.payload if revision is not None else None,
            }
        )
    return result


@router.get("/projects/{project_id}/requirements/{requirement_key}/history")
async def requirement_history(
    project_id: str,
    requirement_key: str,
    session: AsyncSession = Depends(get_session),  # noqa: B008
    user: User = Depends(current_user),  # noqa: B008
) -> list[dict[str, Any]]:
    await owned(project_id, user, session)
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
