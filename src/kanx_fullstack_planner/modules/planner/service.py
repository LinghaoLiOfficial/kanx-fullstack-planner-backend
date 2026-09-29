from __future__ import annotations

import asyncio
import hashlib
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import DataError, IntegrityError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from ...core.config import Settings
from ..jobs.models import Job
from ..jobs.service import PermanentJobError
from .graph import (
    SCHEMA_VERSION,
    WORKFLOW_VERSION,
    DefaultLLMProvider,
    LLMProvider,
    run_graph_stream,
    stable_hash,
)
from .models import (
    LLMInvocation,
    RawRequirement,
    RawRequirementRevision,
    RawRequirementStatus,
    Requirement,
    RequirementDependency,
    RequirementRevision,
    ValidationFinding,
    WorkflowRun,
    WorkflowRunStatus,
    WorkflowStep,
    WorkflowStepStatus,
)
from .monitoring import redact_error
from .schemas import ProjectContext, RawRequirementInput, WorkflowResult


def raw_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def normalize_requirement_key(value: str, index: int, used: set[str]) -> str:
    base = re.sub(r"[^a-z0-9._-]+", "-", value.strip().lower()).strip(".-")
    base = base or f"requirement-{index}"
    candidate = base
    suffix = 2
    while candidate in used:
        candidate = f"{base}-{suffix}"
        suffix += 1
    used.add(candidate)
    return candidate


def is_permanent_persistence_error(error: Exception) -> bool:
    """Return whether retrying the same planner payload cannot repair the DB failure."""
    return isinstance(error, (DataError, IntegrityError, ProgrammingError))


def permanent_persistence_error(error: Exception) -> PermanentJobError:
    return PermanentJobError(str(error), code="planner_persistence_error")


class DurableLLMProvider:
    def __init__(
        self,
        provider: LLMProvider,
        factory: async_sessionmaker[AsyncSession],
        run_id: str,
    ) -> None:
        self.provider = provider
        self.factory = factory
        self.run_id = run_id
        self.lock = asyncio.Lock()

    async def complete(
        self, task: str, schema: type[BaseModel], payload: Mapping[str, Any]
    ) -> BaseModel:
        input_hash = stable_hash(payload)
        cache_key = f"{task}:{input_hash}"
        async with self.lock:
            async with self.factory() as session:
                run = await session.get(WorkflowRun, self.run_id)
                checkpoint = dict(run.checkpoint or {}) if run else {}
                cached = dict(checkpoint.get("llm_cache", {})).get(cache_key)
                if cached is not None:
                    return schema.model_validate(cached)
        result = await self.provider.complete(task, schema, payload)
        output = result.model_dump()
        async with self.lock:
            async with self.factory() as session:
                run = await session.get(WorkflowRun, self.run_id, with_for_update=True)
                if run is None:
                    raise ValueError(f"Workflow run not found: {self.run_id}")
                checkpoint = dict(run.checkpoint or {})
                cache = dict(checkpoint.get("llm_cache", {}))
                cache[cache_key] = output
                checkpoint["llm_cache"] = cache
                run.checkpoint = checkpoint
                await session.commit()
        return result


class PlannerService:
    def __init__(self, provider: LLMProvider | None = None) -> None:
        self.provider = provider or DefaultLLMProvider()

    async def create_run(
        self,
        session: AsyncSession,
        project_id: str,
        request: RawRequirementInput,
        raw_requirement_revision_id: str | None = None,
        job_id: str | None = None,
    ) -> WorkflowRun:
        run = WorkflowRun(
            project_id=project_id,
            job_id=job_id,
            raw_requirement_revision_id=raw_requirement_revision_id,
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

    async def create_raw_requirement(
        self, session: AsyncSession, project_id: str, request: RawRequirementInput
    ) -> tuple[RawRequirement, RawRequirementRevision]:
        raw = RawRequirement(project_id=project_id)
        session.add(raw)
        await session.flush()
        revision = RawRequirementRevision(
            raw_requirement_id=raw.id,
            revision=1,
            description=request.raw_text,
            content_hash=raw_hash(request.raw_text),
            project_context=request.project_context.model_dump(),
        )
        session.add(revision)
        await session.flush()
        return raw, revision

    async def run_with_progress(
        self,
        factory: async_sessionmaker[AsyncSession],
        run_id: str,
        raw_text: str,
        project_context: ProjectContext,
    ) -> dict[str, Any]:
        provider = DurableLLMProvider(self.provider, factory, run_id)
        result: dict[str, Any] | None = None
        async for event in run_graph_stream(raw_text, project_context, provider):
            event_type = str(event.get("type", ""))
            if event_type == "stage":
                await self.persist_stage_event(factory, run_id, event)
            elif event_type in ("llm_call", "llm_call_started"):
                await self.persist_llm_event(factory, run_id, event["data"])
            elif event_type == "result":
                value = event.get("data")
                if isinstance(value, dict):
                    result = value
                    async with factory() as session:
                        run = await session.get(WorkflowRun, run_id, with_for_update=True)
                        if run is not None:
                            checkpoint = dict(run.checkpoint or {})
                            checkpoint["graph_result"] = result
                            run.checkpoint = checkpoint
                            await session.commit()
        if result is None:
            raise RuntimeError("Planner graph completed without a result")
        return result

    async def persist_llm_event(
        self, factory: async_sessionmaker[AsyncSession], run_id: str, record: dict[str, Any]
    ) -> None:
        audit = record["audit"]
        control = record["control"]
        business = record["business"]
        invocation_id = str(record["invocation_id"])
        attempt = int(audit["attempt"])
        async with factory() as session:
            row = await session.scalar(
                select(LLMInvocation).where(
                    LLMInvocation.run_id == run_id,
                    LLMInvocation.invocation_id == invocation_id,
                    LLMInvocation.attempt == attempt,
                )
            )
            if row is None:
                row = LLMInvocation(
                    run_id=run_id,
                    invocation_id=invocation_id,
                    attempt=attempt,
                    task_name=record["task"]["key"],
                    step_id=await session.scalar(
                        select(WorkflowStep.id).where(
                            WorkflowStep.run_id == run_id,
                            WorkflowStep.step_key == record["task"]["key"],
                        )
                    ),
                    model=control["model"],
                    prompt_version=audit["prompt_version"],
                    schema_version=audit["schema_version"],
                    input_hash=audit["input_hash"],
                    status="running",
                    call_index=record["call_index"],
                    subject=record.get("subject"),
                    input_payload=business["input"],
                    control_payload={
                        key: value
                        for key, value in control.items()
                        if key not in ("system_prompt", "user_prompt")
                    },
                    system_prompt=(
                        control.get("system_prompt")
                        if control.get("actual_prompt_recorded")
                        else None
                    ),
                    user_prompt=control.get("user_prompt"),
                    call_type=audit["call_type"],
                    max_attempts=audit["max_attempts"],
                )
                session.add(row)
            if audit["status"] != "running":
                row.status = audit["status"]
                row.latency_ms = audit["elapsed_ms"]
                row.finished_at = datetime.fromisoformat(audit["finished_at"])
                error = audit["error"]
                error_message = error.get("message") if isinstance(error, Mapping) else error
                row.error = redact_error(str(error_message)[:4000]) if error_message else None
                if audit["status"] == "succeeded":
                    row.raw_response = business["output"]
                    row.output_hash = audit["output_hash"]
            await session.commit()

    async def persist_stage_event(
        self,
        factory: async_sessionmaker[AsyncSession],
        run_id: str,
        event: dict[str, Any],
    ) -> None:
        stage = str(event.get("stage", ""))
        if not stage:
            return
        async with factory() as session:
            step = await session.scalar(
                select(WorkflowStep).where(
                    WorkflowStep.run_id == run_id, WorkflowStep.step_key == stage
                )
            )
            if step is None:
                step = WorkflowStep(
                    run_id=run_id,
                    step_key=stage,
                    module_type="system" if stage == "validate_and_gate" else "llm",
                )
                session.add(step)
            status = str(event.get("status", WorkflowStepStatus.RUNNING))
            step.status = status
            if status == WorkflowStepStatus.RUNNING:
                step.attempt = (step.attempt or 0) + 1
                step.started_at = datetime.now(UTC)
                step.error = None
            elif status == WorkflowStepStatus.SUCCEEDED:
                output = event.get("data")
                step.output_payload = output
                step.output_hash = stable_hash(output)
                step.finished_at = datetime.now(UTC)
            elif status == WorkflowStepStatus.FAILED:
                step.error = str(event.get("error", "Planner stage failed"))[:4000]
                step.finished_at = datetime.now(UTC)
            await session.commit()

    async def execute(
        self, factory: async_sessionmaker[AsyncSession], run_id: str
    ) -> dict[str, Any]:
        async with factory() as session:
            run = await session.get(WorkflowRun, run_id, with_for_update=True)
            if run is None:
                raise ValueError(f"Workflow run not found: {run_id}")
            if run.result is not None and run.status in (
                WorkflowRunStatus.SUCCEEDED,
                WorkflowRunStatus.NEEDS_CLARIFICATION,
                WorkflowRunStatus.FAILED,
            ):
                return run.result
            run.status = WorkflowRunStatus.RUNNING
            run.error = None
            run.updated_at = datetime.now(UTC)
            checkpoint = dict(run.checkpoint or {})
            completed_result = checkpoint.get("graph_result")
            raw_text = run.raw_text
            project_context = ProjectContext.model_validate(run.project_context)
            await session.commit()
        if isinstance(completed_result, dict):
            result = completed_result
        else:
            try:
                result = await self.run_with_progress(factory, run_id, raw_text, project_context)
            except Exception as error:
                await self.mark_failed(factory, run_id, error)
                if is_permanent_persistence_error(error):
                    raise permanent_persistence_error(error) from error
                raise
        async with factory() as session:
            run = await session.get(WorkflowRun, run_id, with_for_update=True)
            assert run is not None
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
            high_ambiguity = any(item["severity"] == "high" for item in result["ambiguities"])
            key_map: dict[str, str] = {}
            used_keys: set[str] = set()
            for index, payload in enumerate(result["requirements"], 1):
                original_key = str(payload["requirement_key"])
                normalized_key = normalize_requirement_key(original_key, index, used_keys)
                key_map[original_key] = normalized_key
                payload["requirement_key"] = normalized_key
            for relation in result["dependencies"].get("dependencies", []):
                relation["source_key"] = key_map.get(
                    str(relation.get("source_key", "")), relation.get("source_key", "")
                )
                relation["target_key"] = key_map.get(
                    str(relation.get("target_key", "")), relation.get("target_key", "")
                )
                for payload in result["requirements"]:
                    if payload["requirement_key"] == relation["source_key"]:
                        dependencies = payload.setdefault("dependencies", [])
                        if relation["target_key"] not in dependencies:
                            dependencies.append(relation["target_key"])
            requirement_rows: dict[str, Requirement] = {}
            for payload in result["requirements"]:
                payload["status"] = (
                    "needs_clarification"
                    if high_ambiguity
                    else "draft"
                    if any(item["severity"] == "error" for item in result["findings"])
                    else "ready"
                )
                key = str(payload["requirement_key"])
                requirement = await session.scalar(
                    select(Requirement).where(
                        Requirement.project_id == run.project_id,
                        Requirement.requirement_key == key,
                    )
                )
                if requirement is None:
                    requirement = Requirement(
                        project_id=run.project_id,
                        requirement_key=key,
                        current_revision=1,
                    )
                    session.add(requirement)
                    await session.flush()
                else:
                    requirement.current_revision += 1
                    requirement.updated_at = datetime.now(UTC)
                requirement.status = str(payload.get("status", "ready"))
                requirement_rows[key] = requirement
                session.add(
                    RequirementRevision(
                        requirement_id=requirement.id,
                        run_id=run_id,
                        revision=requirement.current_revision,
                        payload=payload,
                        content_hash=stable_hash(payload),
                        raw_requirement_revision_id=run.raw_requirement_revision_id,
                    )
                )
            for relation in result["dependencies"].get("dependencies", []):
                source = requirement_rows.get(str(relation.get("source_key", "")))
                target = requirement_rows.get(str(relation.get("target_key", "")))
                if source is not None and target is not None:
                    session.add(
                        RequirementDependency(
                            source_requirement_id=source.id,
                            target_requirement_id=target.id,
                            relation=str(relation.get("relation", "requires")),
                            reason=str(relation.get("reason", "")),
                        )
                    )
            raw_requirement_info: dict[str, object] = {}
            if run.raw_requirement_revision_id:
                raw_revision = await session.get(
                    RawRequirementRevision, run.raw_requirement_revision_id
                )
                if raw_revision is not None:
                    raw = await session.get(RawRequirement, raw_revision.raw_requirement_id)
                    if raw is not None:
                        raw.status = (
                            RawRequirementStatus.NEEDS_CLARIFICATION
                            if result["ambiguities"]
                            else RawRequirementStatus.PROCESSED
                        )
                        raw.updated_at = datetime.now(UTC)
                        raw_requirement_info = {
                            "id": raw.id,
                            "revision": raw_revision.revision,
                            "status": raw.status,
                        }
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
                raw_requirement=raw_requirement_info,
                dependency_analysis=result["dependencies"],
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
            try:
                await session.commit()
            except Exception as error:
                await session.rollback()
                await self.mark_failed(factory, run_id, error)
                if is_permanent_persistence_error(error):
                    raise permanent_persistence_error(error) from error
                raise
            return output.model_dump()

    async def mark_failed(
        self, factory: async_sessionmaker[AsyncSession], run_id: str, error: Exception
    ) -> None:
        async with factory() as session:
            run = await session.get(WorkflowRun, run_id, with_for_update=True)
            if run is None:
                return
            run.status = WorkflowRunStatus.FAILED
            run.error = str(error)[:4000]
            run.updated_at = datetime.now(UTC)
            if run.raw_requirement_revision_id:
                revision = await session.get(
                    RawRequirementRevision, run.raw_requirement_revision_id
                )
                if revision is not None:
                    raw = await session.get(RawRequirement, revision.raw_requirement_id)
                    if raw is not None:
                        raw.status = RawRequirementStatus.FAILED
                        raw.updated_at = datetime.now(UTC)
            await session.commit()


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
