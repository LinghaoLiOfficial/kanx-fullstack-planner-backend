from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import AsyncIterator, Callable, Mapping
from datetime import UTC, datetime
from typing import Any, Protocol, cast
from uuid import uuid4

from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, ValidationError
from typing_extensions import TypedDict

from ..ai.provider import structured_output
from ..ai.settings import get_ai_settings
from .schemas import (
    AcceptanceCriterion,
    AgileRequirement,
    Ambiguity,
    BusinessIntent,
    BusinessIntentExtraction,
    BusinessScope,
    CandidateRequirements,
    DependencyAnalysis,
    ExecutionGuidance,
    ImpactScope,
    NormalizedRequirement,
    ProjectContext,
    RequirementCandidate,
)
from .settings import get_planner_settings

SYSTEM_PROMPT = (
    "你是敏捷业务需求分析器。只输出符合给定结构的 JSON。"
    "只处理业务语义，不设计 API、数据库、页面或代码。"
)

WORKFLOW_VERSION = "1.0.0"
SCHEMA_VERSION = "1.0.0"

LLM_TASK_LABELS = {
    "normalize_requirement": "规范化原始需求",
    "extract_business_intents": "提取业务目标",
    "decompose_candidates": "拆分敏捷需求",
    "enrich_requirements": "细化需求与验收标准",
    "analyze_dependencies": "分析重复与依赖",
}

PLANNER_STAGES = (
    "normalize_requirement",
    "extract_business_intents",
    "decompose_candidates",
    "enrich_requirements",
    "analyze_dependencies",
    "validate_and_gate",
)


class PlannerState(TypedDict, total=False):
    raw_text: str
    project_context: dict[str, Any]
    normalized: dict[str, Any]
    intents: list[dict[str, Any]]
    candidates: list[dict[str, Any]]
    requirements: list[dict[str, Any]]
    dependencies: dict[str, Any]
    ambiguities: list[dict[str, Any]]
    findings: list[dict[str, Any]]
    result: dict[str, Any]


def stable_hash(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), default=str
    )
    return hashlib.sha256(encoded.encode()).hexdigest()


class LLMProvider(Protocol):
    async def complete(
        self, task: str, schema: type[BaseModel], payload: Mapping[str, Any]
    ) -> BaseModel: ...


class SchemaValidationRetryError(RuntimeError):
    """A structured response remained invalid after the bounded retry."""

    def __init__(self, task: str, schema: type[BaseModel], records: list[dict[str, Any]]) -> None:
        self.task = task
        self.schema = schema.__name__
        self.records = records
        super().__init__(
            f"Schema validation failed for {task}/{schema.__name__} after {len(records)} attempt(s)"
        )


class DefaultLLMProvider:
    async def complete(
        self, task: str, schema: type[BaseModel], payload: Mapping[str, Any]
    ) -> BaseModel:
        system, user = default_messages(task, schema, payload)
        return await structured_output(schema, [("system", system), ("user", user)], task=task)


def default_messages(
    task: str, schema: type[BaseModel], payload: Mapping[str, Any]
) -> tuple[str, str]:
    system = (
        f"{SYSTEM_PROMPT} 必须严格按照 output_schema 返回一个 JSON 对象；"
        "不得增加字段，不得改名，不得使用外层包装。"
    )
    user = json.dumps(
        {"task": task, "input": payload, "output_schema": schema.model_json_schema()},
        ensure_ascii=False,
        default=str,
    )
    return system, user


class FakeLLMProvider:
    """Deterministic provider for tests and local smoke runs."""

    async def complete(
        self, task: str, schema: type[BaseModel], payload: Mapping[str, Any]
    ) -> BaseModel:
        text = str(payload.get("raw_text") or payload.get("normalized_summary") or "用户需求")
        if schema is NormalizedRequirement:
            return NormalizedRequirement(
                normalized_summary=text,
                explicit_goals=[text],
                mentioned_behaviors=[text],
            )
        if schema is BusinessIntentExtraction:
            return BusinessIntentExtraction(
                intents=[
                    BusinessIntent(
                        intent_key="intent-1",
                        name="用户需求",
                        goal=text,
                        primary_actor="用户",
                        trigger="用户提出需求",
                        desired_outcome=text,
                        confidence=0.8,
                    )
                ]
            )
        if schema is CandidateRequirements:
            return CandidateRequirements(
                candidates=[
                    RequirementCandidate(
                        draft_key="requirement-1",
                        name="实现用户需求",
                        value=text,
                        primary_actor="用户",
                        source_evidence=[text],
                    )
                ]
            )
        if schema is AgileRequirement:
            candidate = payload["requirement"]
            return AgileRequirement(
                requirement_key=str(candidate["draft_key"]),
                name=str(candidate["name"]),
                user_story=f"作为{candidate['primary_actor']}，我希望{candidate['value']}，以便达成业务目标。",
                business_goal=str(candidate["value"]),
                impact_scope=ImpactScope(
                    user_roles=[str(candidate["primary_actor"])],
                    future_asset_types=["ux", "api", "database"],
                ),
                business_scope=BusinessScope(included=[str(candidate["value"])]),
                execution_guidance=ExecutionGuidance(
                    objective=str(candidate["value"]), expected_behavior=[str(candidate["value"])]
                ),
                acceptance_criteria=[
                    AcceptanceCriterion(
                        id="ac-1",
                        given="用户提出需求",
                        when="系统处理需求",
                        then="系统提供符合需求的结果",
                    )
                ],
                source_evidence=list(candidate.get("source_evidence", [])),
            )
        if schema is DependencyAnalysis:
            return DependencyAnalysis(
                ordering=[
                    item.get("requirement_key", "") for item in payload.get("requirements", [])
                ]
            )
        raise ValueError(f"Fake provider has no response for {schema.__name__}")


async def run_graph(
    raw_text: str, context: ProjectContext, provider: LLMProvider | None = None
) -> dict[str, Any]:
    graph = build_planner_graph(provider)
    state = await graph.ainvoke({"raw_text": raw_text, "project_context": context.model_dump()})
    result = state.get("result")
    if result is None:
        raise RuntimeError("Planner graph completed without a result")
    return cast(dict[str, Any], result)


async def run_graph_stream(
    raw_text: str, context: ProjectContext, provider: LLMProvider | None = None
) -> AsyncIterator[dict[str, Any]]:
    graph = build_planner_graph(provider)
    async for mode, chunk in graph.astream(
        {"raw_text": raw_text, "project_context": context.model_dump()},
        stream_mode=["custom", "values"],
    ):
        if mode == "custom":
            yield cast(dict[str, Any], chunk)
        elif mode == "values" and "result" in chunk:
            yield {"type": "result", "data": chunk["result"]}


def _emit(event: dict[str, Any]) -> None:
    try:
        get_stream_writer()(event)
    except RuntimeError:
        # Direct graph.ainvoke() has no stream consumer.
        pass


async def _invoke_with_events(
    llm: LLMProvider,
    task: str,
    schema: type[BaseModel],
    payload: Mapping[str, Any],
    *,
    call_index: int = 1,
    subject: dict[str, str] | None = None,
) -> BaseModel:
    try:
        raw, _ = await _invoke(
            llm,
            task,
            schema,
            payload,
            call_index=call_index,
            subject=subject,
            record_sink=lambda record: _emit(
                {
                    "type": (
                        "llm_call_started" if record["audit"]["status"] == "running" else "llm_call"
                    ),
                    "data": record,
                }
            ),
        )
    except SchemaValidationRetryError as error:
        _emit({"type": "stage", "stage": task, "status": "failed", "error": str(error)})
        raise
    except Exception as error:
        _emit({"type": "stage", "stage": task, "status": "failed", "error": str(error)})
        raise
    return raw


def build_planner_graph(provider: LLMProvider | None = None) -> Any:
    """Build the serializable planner StateGraph with an injectable provider."""
    llm = provider or DefaultLLMProvider()

    async def normalize_requirement(state: PlannerState) -> PlannerState:
        _emit({"type": "stage", "stage": "normalize_requirement", "status": "running"})
        value = await _invoke_with_events(
            llm,
            "normalize_requirement",
            NormalizedRequirement,
            {"raw_text": state["raw_text"], "project_context": state["project_context"]},
        )
        normalized = NormalizedRequirement.model_validate(value)
        data = normalized.model_dump()
        _emit(
            {"type": "stage", "stage": "normalize_requirement", "status": "succeeded", "data": data}
        )
        return {"normalized": data}

    async def extract_business_intents(state: PlannerState) -> PlannerState:
        _emit({"type": "stage", "stage": "extract_business_intents", "status": "running"})
        value = await _invoke_with_events(
            llm, "extract_business_intents", BusinessIntentExtraction, state["normalized"]
        )
        intents = BusinessIntentExtraction.model_validate(value).intents
        data = [item.model_dump() for item in intents]
        _emit(
            {
                "type": "stage",
                "stage": "extract_business_intents",
                "status": "succeeded",
                "data": data,
            }
        )
        return {"intents": data}

    async def decompose_candidates(state: PlannerState) -> PlannerState:
        _emit({"type": "stage", "stage": "decompose_candidates", "status": "running"})
        value = await _invoke_with_events(
            llm,
            "decompose_candidates",
            CandidateRequirements,
            {"normalized": state["normalized"], "intents": state["intents"]},
        )
        candidates = CandidateRequirements.model_validate(value)
        data = [item.model_dump() for item in candidates.candidates]
        _emit(
            {"type": "stage", "stage": "decompose_candidates", "status": "succeeded", "data": data}
        )
        return {"candidates": data}

    async def enrich_requirements(state: PlannerState) -> PlannerState:
        _emit({"type": "stage", "stage": "enrich_requirements", "status": "running"})
        semaphore = asyncio.Semaphore(get_planner_settings().enrichment_concurrency)

        async def enrich_one(call_index: int, requirement: dict[str, Any]) -> dict[str, Any]:
            async with semaphore:
                value = await _invoke_with_events(
                    llm,
                    "enrich_requirements",
                    AgileRequirement,
                    {
                        "requirement": requirement,
                        "normalized": state["normalized"],
                        "project_context": state["project_context"],
                    },
                    call_index=call_index,
                    subject={
                        "key": str(requirement.get("draft_key", "")),
                        "label": str(requirement.get("name", "")),
                    },
                )
                return AgileRequirement.model_validate(value).model_dump()

        enriched = await asyncio.gather(
            *(
                enrich_one(call_index, requirement)
                for call_index, requirement in enumerate(state["candidates"], 1)
            )
        )
        _emit(
            {
                "type": "stage",
                "stage": "enrich_requirements",
                "status": "succeeded",
                "data": enriched,
            }
        )
        return {"requirements": enriched}

    async def analyze_dependencies(state: PlannerState) -> PlannerState:
        _emit({"type": "stage", "stage": "analyze_dependencies", "status": "running"})
        value = await _invoke_with_events(
            llm, "analyze_dependencies", DependencyAnalysis, {"requirements": state["requirements"]}
        )
        dependencies = DependencyAnalysis.model_validate(value).model_dump()
        _emit(
            {
                "type": "stage",
                "stage": "analyze_dependencies",
                "status": "succeeded",
                "data": dependencies,
            }
        )
        return {"dependencies": dependencies}

    async def validate_and_gate(state: PlannerState) -> PlannerState:
        _emit({"type": "stage", "stage": "validate_and_gate", "status": "running"})
        normalized = NormalizedRequirement.model_validate(state["normalized"])
        requirements = [AgileRequirement.model_validate(item) for item in state["requirements"]]
        ambiguities = _ambiguities(normalized, requirements)
        findings = validate_requirements(state["raw_text"], requirements)
        result = {
            "normalized": state["normalized"],
            "intents": state["intents"],
            "requirements": state["requirements"],
            "dependencies": state["dependencies"],
            "ambiguities": [item.model_dump() for item in ambiguities],
            "findings": findings,
        }
        _emit(
            {"type": "stage", "stage": "validate_and_gate", "status": "succeeded", "data": findings}
        )
        return {
            "ambiguities": cast(list[dict[str, Any]], result["ambiguities"]),
            "findings": findings,
            "result": result,
        }

    graph = StateGraph(PlannerState)
    graph.add_node("normalize_requirement", normalize_requirement)
    graph.add_node("extract_business_intents", extract_business_intents)
    graph.add_node("decompose_candidates", decompose_candidates)
    graph.add_node("enrich_requirements", enrich_requirements)
    graph.add_node("analyze_dependencies", analyze_dependencies)
    graph.add_node("validate_and_gate", validate_and_gate)
    graph.add_edge(START, "normalize_requirement")
    for previous, current in zip(PLANNER_STAGES, PLANNER_STAGES[1:], strict=False):
        graph.add_edge(previous, current)
    graph.add_edge("validate_and_gate", END)
    return graph.compile()


compiled_planner_graph = build_planner_graph()


def planner_graph_mermaid() -> str:
    return cast(str, compiled_planner_graph.get_graph().draw_mermaid())


async def _invoke(
    provider: LLMProvider,
    task: str,
    schema: type[BaseModel],
    payload: Mapping[str, Any],
    *,
    call_index: int = 1,
    subject: dict[str, str] | None = None,
    record_sink: Callable[[dict[str, Any]], None] | None = None,
) -> tuple[BaseModel, list[dict[str, Any]]]:
    """Invoke a structured node with one bounded same-input schema retry."""
    config = get_ai_settings().for_task(task)
    records: list[dict[str, Any]] = []
    max_attempts = config.schema_max_retries + 1
    invocation_id = str(uuid4())

    def finish(record: dict[str, Any]) -> None:
        records.append(record)
        if record_sink is not None:
            record_sink(record)

    def fail(record: dict[str, Any], started: float, error_class: str, error: Exception) -> None:
        record["audit"].update(
            status="failed",
            finished_at=datetime.now(UTC).isoformat(),
            elapsed_ms=round((time.perf_counter() - started) * 1000),
            error={"class": error_class, "message": f"{type(error).__name__}: {error}"},
        )
        finish(record)

    for attempt in range(1, max_attempts + 1):
        started_at = datetime.now(UTC)
        started = time.perf_counter()
        record: dict[str, Any] = {
            "invocation_id": invocation_id,
            "task": {"key": task, "label": LLM_TASK_LABELS.get(task, task)},
            "call_index": call_index,
            "subject": subject,
            "business": {"input": dict(payload), "output": None},
            "control": {
                "schema": schema.__name__,
                "model": config.model,
                "structured_output_method": config.structured_output_method,
                "temperature": config.temperature,
                "max_tokens": config.max_tokens,
                "connect_timeout_seconds": config.connect_timeout_seconds,
                "read_timeout_seconds": config.read_timeout_seconds,
                "provider_max_retries": config.provider_max_retries,
                "schema_max_retries": config.schema_max_retries,
                "system_prompt": SYSTEM_PROMPT,
            },
            "audit": {
                "status": "running",
                "call_type": "normal" if attempt == 1 else "schema_validation_retry",
                "attempt": attempt,
                "max_attempts": max_attempts,
                "started_at": started_at.isoformat(),
                "finished_at": None,
                "elapsed_ms": None,
                "input_hash": stable_hash(payload),
                "output_hash": None,
                "prompt_version": WORKFLOW_VERSION,
                "schema_version": SCHEMA_VERSION,
                "error": None,
            },
        }

        actual_provider = getattr(provider, "provider", provider)
        if isinstance(actual_provider, DefaultLLMProvider):
            system, user = default_messages(task, schema, payload)
            record["control"]["system_prompt"] = system
            record["control"]["user_prompt"] = user
            record["control"]["actual_prompt_recorded"] = True
        else:
            record["control"]["system_prompt"] = SYSTEM_PROMPT
            record["control"]["user_prompt"] = None
            record["control"]["actual_prompt_recorded"] = False
        if record_sink is not None:
            record_sink(
                dict(record, audit=dict(record["audit"]), business=dict(record["business"]))
            )

        try:
            raw_result = await provider.complete(task, schema, payload)
            result = schema.model_validate(raw_result)
        except Exception as error:
            if _is_request_schema_error(error):
                fail(record, started, "request_schema", error)
                raise
            if not _is_output_schema_error(error):
                fail(record, started, type(error).__name__, error)
                raise
            fail(record, started, "schema_validation", error)
            if attempt >= max_attempts:
                raise SchemaValidationRetryError(task, schema, records) from error
            continue
        output = result.model_dump()
        record["business"]["output"] = output
        record["audit"].update(
            status="succeeded",
            output_hash=stable_hash(output),
            finished_at=datetime.now(UTC).isoformat(),
            elapsed_ms=round((time.perf_counter() - started) * 1000),
        )
        finish(record)
        return result, records
    raise AssertionError("schema retry loop completed without a result")


def _is_request_schema_error(error: Exception) -> bool:
    message = str(error).casefold()
    return "invalid schema for response_format" in message or (
        "additionalproperties" in message and "required to be supplied" in message
    )


def _is_output_schema_error(error: Exception) -> bool:
    if isinstance(error, ValidationError):
        return True
    name = type(error).__name__.casefold()
    message = str(error).casefold()
    return (
        "jsondecode" in name
        or "outputparser" in name
        or any(
            phrase in message
            for phrase in ("validation error", "json parse", "invalid json", "field required")
        )
    )


async def _complete_intents(
    provider: LLMProvider, normalized: NormalizedRequirement
) -> list[BusinessIntent]:
    raw = await provider.complete(
        "extract_business_intents", BusinessIntentExtraction, normalized.model_dump()
    )
    extraction = (
        raw
        if isinstance(raw, BusinessIntentExtraction)
        else BusinessIntentExtraction.model_validate(raw)
    )
    return extraction.intents


async def _enrich(
    provider: LLMProvider,
    requirements: list[RequirementCandidate],
    normalized: NormalizedRequirement,
    context: ProjectContext,
) -> list[AgileRequirement]:
    enriched: list[AgileRequirement] = []
    for requirement in requirements:
        result = await provider.complete(
            "enrich_requirements",
            AgileRequirement,
            {
                "requirement": requirement.model_dump(),
                "normalized": normalized.model_dump(),
                "project_context": context.model_dump(),
            },
        )
        enriched.append(
            result
            if isinstance(result, AgileRequirement)
            else AgileRequirement.model_validate(result)
        )
    return enriched


def _ambiguities(
    normalized: NormalizedRequirement, requirements: list[AgileRequirement]
) -> list[Ambiguity]:
    result: list[Ambiguity] = []
    for index, term in enumerate(normalized.unresolved_terms, 1):
        result.append(
            Ambiguity(
                id=f"ambiguity-{index}",
                question=f"请明确术语：{term}",
                related_requirement_keys=[item.requirement_key for item in requirements],
                severity="high",
            )
        )
    return result


def validate_requirements(
    raw_text: str, requirements: list[AgileRequirement]
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    if not requirements:
        findings.append(
            {"severity": "error", "code": "no_requirements", "message": "未生成任何敏捷业务需求"}
        )
    seen: set[str] = set()
    for item in requirements:
        if item.requirement_key in seen:
            findings.append(
                {
                    "severity": "error",
                    "code": "duplicate_key",
                    "message": "需求候选键重复",
                    "feature_key": item.requirement_key,
                }
            )
        seen.add(item.requirement_key)
        if not item.source_evidence:
            findings.append(
                {
                    "severity": "warning",
                    "code": "missing_source_evidence",
                    "message": "需求缺少原文证据",
                    "feature_key": item.requirement_key,
                }
            )
        if not item.name:
            findings.append(
                {
                    "severity": "error",
                    "code": "missing_name",
                    "message": "需求缺少名称",
                    "feature_key": item.requirement_key,
                }
            )
        if not item.user_story:
            findings.append(
                {
                    "severity": "error",
                    "code": "missing_user_story",
                    "message": "需求缺少用户故事",
                    "feature_key": item.requirement_key,
                }
            )
        if not item.business_scope.included:
            findings.append(
                {
                    "severity": "error",
                    "code": "missing_business_scope",
                    "message": "需求缺少包含的业务范围",
                    "feature_key": item.requirement_key,
                }
            )
        if (
            not item.impact_scope.business_domains
            and not item.impact_scope.user_roles
            and not item.impact_scope.business_objects
        ):
            findings.append(
                {
                    "severity": "error",
                    "code": "missing_impact_scope",
                    "message": "需求缺少影响范围",
                    "feature_key": item.requirement_key,
                }
            )
        if not item.execution_guidance.objective:
            findings.append(
                {
                    "severity": "error",
                    "code": "missing_execution_guidance",
                    "message": "需求缺少执行说明",
                    "feature_key": item.requirement_key,
                }
            )
        if not item.acceptance_criteria:
            findings.append(
                {
                    "severity": "error",
                    "code": "missing_acceptance_criteria",
                    "message": "需求缺少验收标准",
                    "feature_key": item.requirement_key,
                }
            )
    if raw_text and requirements and not any(item.source_evidence for item in requirements):
        findings.append(
            {"severity": "error", "code": "coverage_failure", "message": "原始需求未被需求证据覆盖"}
        )
    return findings
