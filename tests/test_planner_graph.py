import asyncio
import json
from datetime import datetime
from typing import Any
from uuid import UUID

from kanx_fullstack_planner import gradio_app
from kanx_fullstack_planner.gradio_app import (
    global_runtime_info_html,
    llm_attempt_choices,
    llm_invocation_choices,
    llm_task_choices,
    select_llm_task,
    validation_report_html,
    workflow_graph_html,
)
from kanx_fullstack_planner.modules.ai.settings import AISettings
from kanx_fullstack_planner.modules.planner import graph as planner_graph
from kanx_fullstack_planner.modules.planner.graph import (
    PLANNER_STAGES,
    FakeLLMProvider,
    compiled_planner_graph,
    planner_graph_mermaid,
    run_graph,
)
from kanx_fullstack_planner.modules.planner.schemas import ProjectContext


def test_fake_provider_generates_agile_requirement() -> None:
    result = asyncio.run(
        run_graph(
            "用户希望在结算时使用优惠码",
            ProjectContext(business_domain="电商"),
            FakeLLMProvider(),
        )
    )
    assert result["requirements"]
    requirement = result["requirements"][0]
    assert requirement["draft_key"] == "requirement-1"
    assert requirement["acceptance_criteria"]
    assert result["findings"] == []


def test_default_provider_includes_the_exact_output_schema(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}

    async def fake_structured_output(
        schema: Any, messages: list[Any], *, task: str
    ) -> Any:
        captured.update(schema=schema, messages=messages, task=task)
        return schema(
            normalized_summary="收藏商品",
            explicit_goals=["收藏商品"],
            mentioned_behaviors=["收藏商品"],
        )

    monkeypatch.setattr(planner_graph, "structured_output", fake_structured_output)
    result = asyncio.run(
        planner_graph.DefaultLLMProvider().complete(
            "normalize_requirement",
            planner_graph.NormalizedRequirement,
            {"raw_text": "收藏商品"},
        )
    )
    user_payload = json.loads(captured["messages"][1][1])
    assert result.normalized_summary == "收藏商品"
    assert captured["task"] == "normalize_requirement"
    assert user_payload["output_schema"]["title"] == "NormalizedRequirement"
    assert "不得使用外层包装" in captured["messages"][0][1]


def test_planner_graph_has_the_expected_topology() -> None:
    graph = compiled_planner_graph.get_graph()
    assert all(stage in graph.nodes for stage in PLANNER_STAGES)
    assert graph.edges[0].source == "__start__"
    assert graph.edges[-1].target == "__end__"
    mermaid = planner_graph_mermaid()
    assert all(stage in mermaid for stage in PLANNER_STAGES)


def test_gradio_workflow_graph_renders_runtime_statuses() -> None:
    html = workflow_graph_html(
        {"normalize_requirement": "succeeded", "decompose_candidates": "running"},
        {"normalize_requirement": 1.24, "decompose_candidates": 62.5},
    )
    assert "normalize_requirement" in html
    assert "class=\"wf-node succeeded\"" in html
    assert "class=\"wf-node running\"" in html
    assert "1.2s" in html
    assert "1m 2.5s" in html
    assert "--" in html


def test_validation_report_distinguishes_pending_passed_and_attention() -> None:
    pending = validation_report_html()
    passed = validation_report_html(state="completed")
    attention = validation_report_html(
        ambiguities=[{"severity": "high", "question": "请明确自动处理范围"}],
        state="completed",
    )
    assert "等待执行" in pending
    assert "所有结构与业务门禁均已通过" in passed
    assert "错误 0 · 警告 0 · 歧义 0" in passed
    assert "需关注" in attention
    assert "待澄清" in attention
    assert "请明确自动处理范围" in attention


def test_validation_report_renders_failures_and_escapes_business_text() -> None:
    html = validation_report_html(
        findings=[
            {
                "severity": "error",
                "code": "duplicate_key",
                "message": "需求键 <重复>",
                "feature_key": "<script>",
            }
        ],
        state="completed",
    )
    assert "未通过" in html
    assert "错误 1 · 警告 0 · 歧义 0" in html
    assert "需求键 &lt;重复&gt;" in html
    assert "&lt;script&gt;" in html
    assert "需求键保持唯一" in html


def test_global_runtime_info_shows_retry_policies_and_temporal_scope() -> None:
    html = global_runtime_info_html()
    assert "LangChain LLM Provider 重试" in html
    assert "相同输入、Prompt 和 Schema 重试" in html
    assert "实际重试由 Provider SDK 内部执行，当前事件不可观测" in html
    assert "Temporal Activity 重试" in html
    assert "本次 Gradio 直连 LangGraph，未经过 Temporal，因此不适用" in html
    assert "本次已重试 0 个逻辑调用，共 0 个额外 Attempt" in html


def test_global_runtime_info_reports_gate_errors_and_observed_schema_retries() -> None:
    def record(attempt: int, status: str) -> dict[str, Any]:
        return {
            "invocation_id": "same-invocation",
            "task": {"key": "normalize_requirement", "label": "规范化原始需求"},
            "call_index": 1,
            "subject": None,
            "control": {"provider_max_retries": 1, "schema_max_retries": 1},
            "audit": {"attempt": attempt, "status": status},
        }

    html = global_runtime_info_html(
        findings=[
            {
                "severity": "error",
                "code": "duplicate_key",
                "message": "需求键 <重复>",
            }
        ],
        llm_calls=[record(1, "failed"), record(2, "succeeded")],
        gate_state="completed",
    )
    assert "未通过，共 1 个错误" in html
    assert "需求键 &lt;重复&gt;" in html
    assert "本次已重试 1 个逻辑调用，共 1 个额外 Attempt" in html
    assert "规范化原始需求 · 2 次尝试 · 成功" in html


def test_gradio_registers_global_runtime_information() -> None:
    demo = gradio_app.build_demo()
    labels = {
        component.get("props", {}).get("label")
        for component in demo.config["components"]
    }
    assert "全局运行信息" in labels


def test_gradio_removes_component_focus_frames() -> None:
    theme = gradio_app.gradio_theme().to_dict()["theme"]
    assert theme["input_border_color_focus"] == "*border_color_primary"
    assert theme["input_shadow_focus"] == "none"
    assert theme["checkbox_border_color_focus"] == "*checkbox_border_color"
    assert ":focus-visible" in gradio_app.GRADIO_FOCUS_CSS
    assert "box-shadow: none !important" in gradio_app.GRADIO_FOCUS_CSS


def test_validation_reports_duplicate_keys() -> None:
    from kanx_fullstack_planner.modules.planner.graph import validate_requirements
    from kanx_fullstack_planner.modules.planner.schemas import AgileRequirement

    item = AgileRequirement(
        draft_key="same",
        name="需求",
        user_story="作为用户，我希望完成需求。",
        business_goal="完成需求",
        scope={"in_scope": ["需求"], "out_of_scope": []},
        acceptance_criteria=[{"id": "ac-1", "given": "前置", "when": "动作", "then": "结果"}],
        source_evidence=["需求"],
    )
    findings = validate_requirements("需求", [item, item])
    assert any(item["code"] == "duplicate_key" for item in findings)


def test_dependency_schema_has_no_untyped_object_items() -> None:
    from kanx_fullstack_planner.modules.planner.schemas import DependencyAnalysis

    schema = json.dumps(DependencyAnalysis.model_json_schema())
    assert '"additionalProperties": {}' not in schema
    assert '"items": {"type": "object"}' not in schema
    assert '"additionalProperties": false' in schema


def test_schema_validation_retries_once_with_the_same_input(monkeypatch: Any) -> None:
    class Provider(FakeLLMProvider):
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, Any]]] = []

        async def complete(self, task: str, schema: Any, payload: Any) -> Any:
            self.calls.append((task, dict(payload)))
            if task == "normalize_requirement" and len(self.calls) == 1:
                return {"unexpected": "invalid output"}
            return await super().complete(task, schema, payload)

    provider = Provider()
    monkeypatch.setattr(
        planner_graph,
        "get_ai_settings",
        lambda: AISettings(schema_max_retries=1),
    )
    result = asyncio.run(
        run_graph("用户希望收藏商品", ProjectContext(business_domain="电商"), provider)
    )

    normalize_calls = [item for item in provider.calls if item[0] == "normalize_requirement"]
    assert len(normalize_calls) == 2
    assert normalize_calls[0][1] == normalize_calls[1][1]
    assert result["requirements"]


def test_schema_validation_retry_is_limited_to_one(monkeypatch: Any) -> None:
    class Provider(FakeLLMProvider):
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, task: str, schema: Any, payload: Any) -> Any:
            if task == "normalize_requirement":
                self.calls += 1
                return {"unexpected": "invalid output"}
            return await super().complete(task, schema, payload)

    provider = Provider()
    monkeypatch.setattr(
        planner_graph,
        "get_ai_settings",
        lambda: AISettings(schema_max_retries=1),
    )

    async def collect() -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        try:
            async for event in planner_graph.run_graph_stream(
                "用户希望收藏商品", ProjectContext(business_domain="电商"), provider
            ):
                events.append(event)
        except planner_graph.SchemaValidationRetryError:
            pass
        return events

    events = asyncio.run(collect())
    calls = [event["data"] for event in events if event["type"] == "llm_call"]
    assert provider.calls == 2
    assert len(calls) == 2
    assert [item["audit"]["attempt"] for item in calls] == [1, 2]
    assert len({item["invocation_id"] for item in calls}) == 1
    assert all(
        item["audit"]["error"]["class"] == "schema_validation" for item in calls
    )


def test_llm_call_event_is_grouped_by_semantics_control_and_audit() -> None:
    async def collect() -> list[dict[str, Any]]:
        return [
            event["data"]
            async for event in planner_graph.run_graph_stream(
                "用户希望收藏商品", ProjectContext(business_domain="电商"), FakeLLMProvider()
            )
            if event["type"] == "llm_call"
        ]

    record = asyncio.run(collect())[0]
    assert set(record) == {
        "invocation_id", "task", "call_index", "subject", "business", "control", "audit"
    }
    UUID(record["invocation_id"])
    assert record["task"] == {"key": "normalize_requirement", "label": "规范化原始需求"}
    assert record["business"]["input"]["raw_text"] == "用户希望收藏商品"
    assert record["business"]["output"]["normalized_summary"] == "用户希望收藏商品"
    assert record["control"]["schema"] == "NormalizedRequirement"
    assert record["control"]["system_prompt"]
    assert record["audit"]["status"] == "succeeded"
    assert record["audit"]["prompt_version"] == planner_graph.WORKFLOW_VERSION
    datetime.fromisoformat(record["audit"]["started_at"])
    datetime.fromisoformat(record["audit"]["finished_at"])


def test_provider_failure_emits_a_failed_call_record() -> None:
    class Provider(FakeLLMProvider):
        async def complete(self, task: str, schema: Any, payload: Any) -> Any:
            raise RuntimeError("provider unavailable")

    async def collect() -> tuple[list[dict[str, Any]], Exception | None]:
        events: list[dict[str, Any]] = []
        try:
            async for event in planner_graph.run_graph_stream(
                "用户希望收藏商品", ProjectContext(), Provider()
            ):
                events.append(event)
        except Exception as error:
            return events, error
        return events, None

    events, error = asyncio.run(collect())
    records = [event["data"] for event in events if event["type"] == "llm_call"]
    assert isinstance(error, RuntimeError)
    assert len(records) == 1
    assert records[0]["audit"]["status"] == "failed"
    assert records[0]["audit"]["error"]["class"] == "RuntimeError"


def test_enrichment_calls_have_ordered_business_subjects() -> None:
    from kanx_fullstack_planner.modules.planner.schemas import CandidateRequirements

    class Provider(FakeLLMProvider):
        async def complete(self, task: str, schema: Any, payload: Any) -> Any:
            result = await super().complete(task, schema, payload)
            if schema is CandidateRequirements:
                first = result.requirements[0]
                second = first.model_copy(
                    update={"draft_key": "requirement-2", "name": "取消收藏"}
                )
                return CandidateRequirements(requirements=[first, second])
            return result

    async def collect() -> list[dict[str, Any]]:
        return [
            event["data"]
            async for event in planner_graph.run_graph_stream(
                "用户希望收藏和取消收藏", ProjectContext(), Provider()
            )
            if event["type"] == "llm_call"
            and event["data"]["task"]["key"] == "enrich_requirements"
        ]

    records = asyncio.run(collect())
    assert [record["call_index"] for record in records] == [1, 2]
    assert [record["subject"]["key"] for record in records] == [
        "requirement-1", "requirement-2"
    ]


def test_llm_call_selector_helpers_group_and_prioritize_failures() -> None:
    def record(invocation_id: str, call_index: int, status: str, attempt: int) -> dict[str, Any]:
        return {
            "invocation_id": invocation_id,
            "task": {"key": "enrich_requirements", "label": "细化需求与验收标准"},
            "call_index": call_index,
            "subject": {"key": f"requirement-{call_index}", "label": f"需求 {call_index}"},
            "business": {"input": {"index": call_index}, "output": {}},
            "control": {"model": "test", "system_prompt": "system"},
            "audit": {
                "status": status, "attempt": attempt, "max_attempts": 2, "elapsed_ms": 10
            },
        }

    records = [record("one", 1, "succeeded", 1), record("two", 2, "failed", 1)]
    assert llm_task_choices(records) == [("细化需求与验收标准", "enrich_requirements")]
    assert [value for _, value in llm_invocation_choices(records, "enrich_requirements")] == [
        "one", "two"
    ]
    assert llm_attempt_choices(records, "two")[0][1] == "two:1"
    invocation_update, attempt_update, summary, *_ = select_llm_task(
        records, "enrich_requirements"
    )
    assert invocation_update.value == "two"
    assert attempt_update.value == "two:1"
    assert "失败" in summary


def test_run_planner_populates_the_first_llm_call_details(monkeypatch: Any) -> None:
    record = {
        "invocation_id": "invocation-1",
        "task": {"key": "normalize_requirement", "label": "规范化原始需求"},
        "call_index": 1,
        "subject": None,
        "business": {"input": {"raw_text": "收藏商品"}, "output": {"summary": "收藏"}},
        "control": {"model": "test-model", "system_prompt": "system"},
        "audit": {
            "status": "succeeded", "attempt": 1, "max_attempts": 2, "elapsed_ms": 12
        },
    }

    async def fake_stream(*args: Any, **kwargs: Any) -> Any:
        yield {"type": "stage", "stage": "normalize_requirement", "status": "running"}
        yield {"type": "llm_call", "data": record}

    monkeypatch.setattr(gradio_app, "run_graph_stream", fake_stream)

    async def collect() -> list[tuple[Any, ...]]:
        return [item async for item in gradio_app.run_planner("收藏商品", "", "", "", "")]

    outputs = asyncio.run(collect())
    llm_output = outputs[-1]
    assert llm_output[7]["value"] == "normalize_requirement"
    assert llm_output[9].value == "invocation-1"
    assert llm_output[10].value == "invocation-1:1"
    assert "test-model" in llm_output[11]
    assert llm_output[12] == {"raw_text": "收藏商品"}
