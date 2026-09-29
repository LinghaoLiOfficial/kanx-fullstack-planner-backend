from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.api import current_user
from ..database.session import get_session
from ..jobs.models import Job, JobStatus
from ..projects.models import Project
from ..users.models import User
from .api import _generation_progress
from .graph import PLANNER_STAGES
from .models import LLMInvocation, ValidationFinding, WorkflowRun, WorkflowStep
from .monitoring import redact_error

router = APIRouter(prefix="/v1/admin/llm-tasks", tags=["admin-llm-tasks"])


def require_admin(user: User = Depends(current_user)) -> User:  # noqa: B008
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Administrator access required")
    return user


def effective_status(run: WorkflowRun, job: Job | None) -> str:
    if job and job.status in (JobStatus.FAILED, JobStatus.DEAD_LETTERED, JobStatus.ABANDONED):
        return "failed"
    if job and job.status == JobStatus.CANCELLED:
        return "cancelled"
    return str(run.status)


def summary(
    run: WorkflowRun,
    project: Project,
    owner: User,
    job: Job | None,
    steps: list[WorkflowStep],
) -> dict[str, Any]:
    status = effective_status(run, job)
    progress = _generation_progress(run, job, steps)
    return {
        "id": run.id,
        "project_id": project.id,
        "project_name": project.name,
        "user_id": owner.id,
        "username": owner.username,
        "status": status,
        "progress": progress["progress"] if progress else 0,
        "completed_steps": sum(s.status in ("succeeded", "skipped") for s in steps),
        "total_steps": len(PLANNER_STAGES),
        "job_id": run.job_id,
        "job_attempt": job.attempt if job else None,
        "created_at": run.created_at,
        "updated_at": run.updated_at,
    }


@router.get("")
async def list_llm_tasks(
    user_id: str | None = None,
    project_id: str | None = None,
    status: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    session: AsyncSession = Depends(get_session),  # noqa: B008
    _admin: User = Depends(require_admin),  # noqa: B008
) -> dict[str, Any]:
    if status and status not in {
        "queued",
        "running",
        "needs_clarification",
        "succeeded",
        "failed",
        "cancelled",
    }:
        raise HTTPException(422, "Invalid status")
    base = (
        select(WorkflowRun, Project, User, Job)
        .join(Project, WorkflowRun.project_id == Project.id)
        .join(User, Project.owner_user_id == User.id)
        .outerjoin(Job, WorkflowRun.job_id == Job.id)
    )
    if user_id:
        base = base.where(User.id == user_id)
    if project_id:
        base = base.where(Project.id == project_id)
    if status:
        base = base.where(
            case(
                (Job.status.in_(("failed", "dead_lettered", "abandoned")), "failed"),
                (Job.status == "cancelled", "cancelled"),
                else_=WorkflowRun.status,
            )
            == status
        )
    total = await session.scalar(select(func.count()).select_from(base.subquery()))
    rows = (
        await session.execute(
            base.order_by(WorkflowRun.created_at.desc(), WorkflowRun.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).all()
    run_ids = [run.id for run, _, _, _ in rows]
    steps = (
        (await session.scalars(select(WorkflowStep).where(WorkflowStep.run_id.in_(run_ids)))).all()
        if run_ids
        else []
    )
    by_run: dict[str, list[WorkflowStep]] = {run_id: [] for run_id in run_ids}
    for step in steps:
        by_run[step.run_id].append(step)
    return {
        "items": [
            summary(run, project, owner, job, by_run[run.id]) for run, project, owner, job in rows
        ],
        "total": total or 0,
        "page": page,
        "page_size": page_size,
    }


@router.get("/{run_id}")
async def get_llm_task(
    run_id: str,
    session: AsyncSession = Depends(get_session),  # noqa: B008
    _admin: User = Depends(require_admin),  # noqa: B008
) -> dict[str, Any]:
    row = (
        await session.execute(
            select(WorkflowRun, Project, User, Job)
            .join(Project, WorkflowRun.project_id == Project.id)
            .join(User, Project.owner_user_id == User.id)
            .outerjoin(Job, WorkflowRun.job_id == Job.id)
            .where(WorkflowRun.id == run_id)
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(404, "Task not found")
    run, project, owner, job = row
    steps = (await session.scalars(select(WorkflowStep).where(WorkflowStep.run_id == run_id))).all()
    invocations = (
        await session.scalars(
            select(LLMInvocation)
            .where(LLMInvocation.run_id == run_id)
            .order_by(LLMInvocation.created_at, LLMInvocation.id)
        )
    ).all()
    findings = (
        await session.scalars(select(ValidationFinding).where(ValidationFinding.run_id == run_id))
    ).all()
    step_by_key = {step.step_key: step for step in steps}
    terminal_status = effective_status(run, job)
    return {
        **summary(run, project, owner, job, list(steps)),
        "error": redact_error(run.error or (job.error if job else None)),
        "raw_text": run.raw_text,
        "project_context": run.project_context,
        "steps": [
            {
                "key": key,
                "status": (
                    terminal_status
                    if key in step_by_key
                    and step_by_key[key].status == "running"
                    and terminal_status in ("failed", "cancelled")
                    else step_by_key[key].status
                    if key in step_by_key
                    else "pending"
                ),
                "attempt": step_by_key[key].attempt if key in step_by_key else 0,
                "started_at": step_by_key[key].started_at if key in step_by_key else None,
                "finished_at": step_by_key[key].finished_at if key in step_by_key else None,
                "error": redact_error(step_by_key[key].error) if key in step_by_key else None,
            }
            for key in PLANNER_STAGES
        ],
        "invocations": [
            {
                "id": item.id,
                "invocation_id": item.invocation_id,
                "task_name": item.task_name,
                "call_index": item.call_index,
                "subject": item.subject,
                "attempt": item.attempt,
                "max_attempts": item.max_attempts,
                "call_type": item.call_type,
                "status": (
                    terminal_status
                    if item.status == "running" and terminal_status in ("failed", "cancelled")
                    else item.status
                ),
                "model": item.model,
                "latency_ms": item.latency_ms,
                "created_at": item.created_at,
                "finished_at": item.finished_at,
                "input_hash": item.input_hash,
                "output_hash": item.output_hash,
                "input": item.input_payload,
                "output": item.raw_response,
                "control": item.control_payload,
                "system_prompt": item.system_prompt,
                "user_prompt": item.user_prompt,
                "error": redact_error(item.error),
            }
            for item in invocations
        ],
        "findings": [
            {"severity": item.severity, "code": item.code, "message": item.message}
            for item in findings
        ],
    }
