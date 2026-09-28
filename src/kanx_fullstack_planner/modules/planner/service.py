from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from ...core.config import Settings
from ..ai.settings import get_ai_settings
from ..jobs.models import Job
from .graph import (
    SCHEMA_VERSION,
    WORKFLOW_VERSION,
    DefaultLLMProvider,
    LLMProvider,
    run_graph,
    stable_hash,
)
from .models import (
    LLMInvocation,
    Requirement,
    RequirementRevision,
    ValidationFinding,
    WorkflowRun,
    WorkflowRunStatus,
    WorkflowStep,
    WorkflowStepStatus,
)
from .schemas import ProjectContext, RawRequirementInput, WorkflowResult


def raw_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class PlannerService:
    def __init__(self, provider: LLMProvider | None = None) -> None:
        self.provider = provider or DefaultLLMProvider()

    async def create_run(
        self,
        session: AsyncSession,
        project_id: str,
        request: RawRequirementInput,
        job_id: str | None = None,
    ) -> WorkflowRun:
        run = WorkflowRun(
            project_id=project_id,
            job_id=job_id,
            raw_text=request.raw_text,
            raw_text_hash=raw_hash(request.raw_text),
            base_snapshot_id=request.base_snapshot_id,
            project_context=request.project_context.model_dump(),
            workflow_version=WORKFLOW_VERSION,
            schema_version=SCHEMA_VERSION,
        )
        session.add(run)
        await session.flush()
        return run

    async def execute(
        self, factory: async_sessionmaker[AsyncSession], run_id: str
    ) -> dict[str, Any]:
        async with factory() as session:
            run = await session.get(WorkflowRun, run_id, with_for_update=True)
            if run is None:
                raise ValueError(f"Workflow run not found: {run_id}")
            run.status = WorkflowRunStatus.RUNNING
            run.updated_at = datetime.now(UTC)
            await session.commit()
        result = await run_graph(
            run.raw_text, ProjectContext.model_validate(run.project_context), self.provider
        )
        async with factory() as session:
            run = await session.get(WorkflowRun, run_id, with_for_update=True)
            assert run is not None
            for key, payload, module_type in (
                ("normalize_requirement", result["normalized"], "llm"),
                ("extract_business_intents", {"intents": result["intents"]}, "llm"),
                ("decompose_candidates", {"requirements": result["requirements"]}, "llm"),
                ("analyze_dependencies", result["dependencies"], "llm"),
                ("validate_and_gate", {"findings": result["findings"]}, "system"),
            ):
                session.add(
                    WorkflowStep(
                        run_id=run_id,
                        step_key=key,
                        module_type=module_type,
                        status=WorkflowStepStatus.SUCCEEDED,
                        attempt=1,
                        input_hash=stable_hash(run.raw_text),
                        output_hash=stable_hash(payload),
                        output_payload=payload,
                        started_at=datetime.now(UTC),
                        finished_at=datetime.now(UTC),
                    )
                )
                if module_type == "llm":
                    session.add(
                        LLMInvocation(
                            run_id=run_id,
                            task_name=key,
                            model=get_ai_settings().model,
                            prompt_version=WORKFLOW_VERSION,
                            schema_version=SCHEMA_VERSION,
                            input_hash=stable_hash(run.raw_text),
                            output_hash=stable_hash(payload),
                            raw_response=payload,
                        )
                    )
            for finding in result["findings"]:
                session.add(
                    ValidationFinding(
                        run_id=run_id,
                        severity=finding["severity"],
                        code=finding["code"],
                        message=finding["message"],
                        feature_key=finding.get("feature_key"),
                        details=finding.get("details", {}),
                    )
                )
            for payload in result["requirements"]:
                requirement = await session.scalar(
                    select(Requirement).where(
                        Requirement.project_id == run.project_id,
                        Requirement.requirement_key == payload["draft_key"],
                    )
                )
                if requirement is None:
                    requirement = Requirement(
                        project_id=run.project_id,
                        requirement_key=payload["draft_key"],
                        current_revision=1,
                    )
                    session.add(requirement)
                    await session.flush()
                else:
                    requirement.current_revision += 1
                    requirement.updated_at = datetime.now(UTC)
                session.add(
                    RequirementRevision(
                        requirement_id=requirement.id,
                        run_id=run_id,
                        revision=requirement.current_revision,
                        payload=payload,
                        content_hash=stable_hash(payload),
                    )
                )
            high_ambiguity = any(item["severity"] == "high" for item in result["ambiguities"])
            has_error = any(item["severity"] == "error" for item in result["findings"])
            status = (
                WorkflowRunStatus.NEEDS_CLARIFICATION
                if high_ambiguity
                else WorkflowRunStatus.FAILED
                if has_error
                else WorkflowRunStatus.SUCCEEDED
            )
            run.status = status
            output = WorkflowResult(
                run_id=run_id,
                status=status,
                requirements=result["requirements"],
                assumptions=[
                    item for req in result["requirements"] for item in req.get("assumptions", [])
                ],
                ambiguities=result["ambiguities"],
                warnings=[item for item in result["findings"] if item["severity"] == "warning"],
                validation_summary={
                    "error_count": sum(item["severity"] == "error" for item in result["findings"]),
                    "warning_count": sum(
                        item["severity"] == "warning" for item in result["findings"]
                    ),
                    "requirement_count": len(result["requirements"]),
                },
                audit={
                    "workflow_version": run.workflow_version,
                    "schema_version": run.schema_version,
                },
            )
            run.result = output.model_dump()
            run.updated_at = datetime.now(UTC)
            await session.commit()
            return output.model_dump()


async def execute_job(job_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    settings = Settings()
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            job = await session.get(Job, job_id)
            if job is None:
                raise ValueError(f"Job not found: {job_id}")
            run_id = str(payload["run_id"])
        return await PlannerService().execute(factory, run_id)
    finally:
        await engine.dispose()
