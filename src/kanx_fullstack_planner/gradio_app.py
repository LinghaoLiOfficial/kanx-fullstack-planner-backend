from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from html import escape
from typing import Any, cast

import gradio as gr

from .core.config import get_settings
from .modules.ai.settings import get_ai_settings
from .modules.planner.graph import PLANNER_STAGES, run_graph_stream
from .modules.planner.schemas import ProjectContext
from .modules.planner.settings import get_planner_settings

STAGES = {
    "normalize_requirement": "规范化原始需求",
    "extract_business_intents": "提取业务目标",
    "decompose_candidates": "拆分敏捷需求",
    "enrich_requirements": "细化需求与验收标准",
    "analyze_dependencies": "分析重复与依赖",
    "validate_and_gate": "执行结构与业务门禁",
}

EXAMPLES = {
    "simple": {
        "raw_text": "用户登录后可以收藏商品，并在个人中心查看和取消收藏。",
        "project_name": "消费电商平台",
        "description": "面向消费者的在线购物平台。",
        "domain": "电商",
        "users": "消费者",
    },
    "complex": {
        "raw_text": (
            "我们要为电商平台增加购物车结算能力。用户可以从商品详情页和购物车进入结算，"
            "选择收货地址和配送方式，使用优惠码和积分抵扣，在线支付后生成订单。支付失败时允许重新支付，"
            "库存不足时不能创建订单，用户还需要在订单列表中查看订单状态并取消未发货订单。"
            "运营人员可以查看异常订单并进行人工处理。"
        ),
        "project_name": "电商交易平台",
        "description": "支持商品浏览、购物车、订单和运营管理的电商系统。",
        "domain": "电商交易",
        "users": "消费者,运营人员",
    },
    "ambiguous": {
        "raw_text": (
            "系统需要做一个智能推荐和提醒功能，让用户更容易发现喜欢的内容，效果要好，"
            "最好还能根据情况自动处理。"
        ),
        "project_name": "内容社区",
        "description": "用户浏览和发布内容的社区产品。",
        "domain": "内容社区",
        "users": "普通用户,内容作者,运营人员",
    },
}

GRADIO_FOCUS_CSS = """
.gradio-container :where(button, input, textarea, select, [tabindex]):focus,
.gradio-container :where(button, input, textarea, select, [tabindex]):focus-visible,
.gradio-container .cm-editor.cm-focused {
    outline: none !important;
    box-shadow: none !important;
    border-color: var(--border-color-primary) !important;
}

/* Gradio applies the orange accent to the parent block when a child is focused.
   Keep focus visible through the browser's normal caret/selection, without
   turning the whole component frame orange. */
.gradio-container :where(.block, .form, .panel, .accordion):focus-within {
    border-color: var(--border-color-primary) !important;
    box-shadow: none !important;
}
"""


def _context(project_name: str, description: str, domain: str, users: str) -> ProjectContext:
    return ProjectContext(
        project_name=project_name.strip() or None,
        project_description=description.strip() or None,
        business_domain=domain.strip() or None,
        target_users=[item.strip() for item in users.split(",") if item.strip()],
    )


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


def _stage_text(stage: str, state: str, elapsed: float) -> str:
    prefix = "运行中" if state == "running" else "已完成"
    return f"{prefix} · {STAGES.get(stage, stage)} · {elapsed:.1f}s"


def _format_stage_elapsed(seconds: float | None) -> str:
    if seconds is None:
        return "--"
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, remaining = divmod(seconds, 60)
    return f"{int(minutes)}m {remaining:.1f}s"


def workflow_graph_html(
    statuses: dict[str, str] | None = None,
    elapsed_seconds: dict[str, float] | None = None,
) -> str:
    statuses = statuses or {}
    elapsed_seconds = elapsed_seconds or {}
    parts: list[str] = []
    for index, stage in enumerate(PLANNER_STAGES):
        state = statuses.get(stage, "pending")
        elapsed = _format_stage_elapsed(elapsed_seconds.get(stage))
        parts.append(
            f'<div class="wf-node {state}"><span class="wf-state">{state}</span>'
            f"<strong>{STAGES.get(stage, stage)}</strong>"
            f'<span class="wf-duration">{elapsed}</span><small>{stage}</small></div>'
        )
        if index < len(PLANNER_STAGES) - 1:
            parts.append('<div class="wf-arrow">↓</div>')
    return (
        '<div class="wf-wrap"><style>'
        ".wf-wrap{font-family:system-ui;display:flex;flex-direction:column;align-items:center;gap:6px;padding:12px;background:#f8fafc;border-radius:10px}"
        ".wf-node{width:92%;box-sizing:border-box;padding:10px 14px;border:2px solid #cbd5e1;"
        "border-radius:8px;background:white;display:grid;grid-template-columns:auto 1fr auto;"
        "column-gap:10px;align-items:center}"
        ".wf-node strong{font-size:14px}.wf-node small{grid-column:2;color:#64748b}.wf-state{"
        "font-size:10px;text-transform:uppercase;color:#64748b}"
        ".wf-duration{font-variant-numeric:tabular-nums;font-size:12px;font-weight:600;color:#475569}"
        ".wf-node.running{border-color:#f59e0b;background:#fffbeb}.wf-node.succeeded{border-color:#22c55e;background:#f0fdf4}.wf-node.failed{border-color:#ef4444;background:#fef2f2}.wf-arrow{color:#64748b;font-size:20px;line-height:16px}"
        "</style>" + "".join(parts) + "</div>"
    )


def validation_report_html(
    findings: list[dict[str, Any]] | None = None,
    ambiguities: list[dict[str, Any]] | None = None,
    state: str = "pending",
) -> str:
    findings = findings or []
    ambiguities = ambiguities or []
    errors = [item for item in findings if item.get("severity") == "error"]
    warnings = [item for item in findings if item.get("severity") == "warning"]
    if state == "pending":
        headline, detail, tone = "等待执行", "Workflow 完成后显示门禁结果", "pending"
    elif state == "running":
        headline, detail, tone = "校验中", "正在执行结构与业务门禁", "running"
    elif state == "failed":
        headline, detail, tone = "执行失败", "门禁节点未能完成", "failed"
    elif errors:
        headline, detail, tone = "未通过", "存在阻止需求交付的错误", "failed"
    elif warnings or ambiguities:
        headline, tone = "需关注", "warning"
        detail = "门禁通过，但存在警告或需要澄清的内容"
    else:
        headline, detail, tone = "通过", "所有结构与业务门禁均已通过", "passed"

    failure_codes = {str(item.get("code", "")) for item in findings}
    checks = (
        ("no_requirements", "已生成敏捷业务需求"),
        ("duplicate_key", "需求键保持唯一"),
        ("missing_source_evidence", "需求包含原文证据"),
        ("missing_acceptance_criteria", "需求包含验收标准"),
        ("coverage_failure", "原始需求已被证据覆盖"),
    )
    check_items = "".join(
        f'<li class="{"check-failed" if code in failure_codes else "check-passed"}">'
        f"<span>{'未通过' if code in failure_codes else '通过'}</span>{escape(label)}</li>"
        for code, label in checks
    )

    issue_items = "".join(
        f"<li><strong>{escape(str(item.get('severity', '')).upper())}</strong> "
        f"{escape(str(item.get('message', '')))}"
        f"{f' · {escape(str(item["feature_key"]))}' if item.get('feature_key') else ''}</li>"
        for item in findings
    )
    ambiguity_items = "".join(
        f"<li><strong>{escape(str(item.get('severity', '')).upper())}</strong> "
        f"{escape(str(item.get('question', '')))}</li>"
        for item in ambiguities
    )
    issues = (
        f'<div class="gate-issues"><h4>发现项</h4><ul>{issue_items}</ul></div>'
        if issue_items
        else ""
    )
    questions = (
        f'<div class="gate-issues"><h4>待澄清</h4><ul>{ambiguity_items}</ul></div>'
        if ambiguity_items
        else ""
    )
    checklist = (
        f'<div class="gate-checks"><h4>检查项</h4><ul>{check_items}</ul></div>'
        if state == "completed"
        else ""
    )
    return (
        f'<div class="gate-report {tone}"><style>'
        ".gate-report{font-family:system-ui;padding:12px 2px}.gate-head{display:flex;"
        "justify-content:space-between;gap:12px;align-items:flex-start}.gate-head h3{margin:0;"
        "font-size:16px}.gate-head p{margin:3px 0 0;color:#64748b;font-size:12px}.gate-counts{"
        "font-size:12px;white-space:nowrap;color:#475569}.gate-report.passed h3{color:#15803d}"
        ".gate-report.warning h3,.gate-report.running h3{color:#b45309}.gate-report.failed h3{"
        "color:#b91c1c}.gate-checks,.gate-issues{margin-top:12px;border-top:1px solid #e2e8f0;"
        "padding-top:9px}.gate-report h4{margin:0 0 6px;font-size:12px;color:#475569}"
        ".gate-report ul{list-style:none;margin:0;padding:0;display:grid;gap:5px}.gate-report li{"
        "font-size:12px;color:#334155}.gate-report li span{display:inline-block;width:48px;"
        "font-weight:600}.check-passed span{color:#15803d}.check-failed span{color:#b91c1c}"
        "</style>"
        f'<div class="gate-head"><div><h3>{headline}</h3><p>{detail}</p></div>'
        f'<div class="gate-counts">错误 {len(errors)} · 警告 {len(warnings)} · '
        f"歧义 {len(ambiguities)}</div></div>{checklist}{issues}{questions}</div>"
    )


def global_runtime_info_html(
    findings: list[dict[str, Any]] | None = None,
    llm_calls: list[dict[str, Any]] | None = None,
    gate_state: str = "pending",
) -> str:
    """Render global gate and retry information without overstating observability."""
    findings = findings or []
    llm_calls = llm_calls or []
    gate_errors = [item for item in findings if item.get("severity") == "error"]
    ai_settings = get_ai_settings()
    planner_settings = get_planner_settings()

    provider_limits = {
        int(record.get("control", {}).get("provider_max_retries", 0)) for record in llm_calls
    }
    if not provider_limits:
        provider_limits = {ai_settings.provider_max_retries}
    provider_policy = ", ".join(str(value) for value in sorted(provider_limits))

    schema_limits = {
        int(record.get("control", {}).get("schema_max_retries", 0)) for record in llm_calls
    }
    if not schema_limits:
        schema_limits = {ai_settings.schema_max_retries}
    schema_policy = ", ".join(str(value) for value in sorted(schema_limits))
    schema_max_attempts = ", ".join(str(value + 1) for value in sorted(schema_limits))

    invocations: dict[str, list[dict[str, Any]]] = {}
    for record in llm_calls:
        invocations.setdefault(str(record.get("invocation_id", "")), []).append(record)
    retried = [records for records in invocations.values() if len(records) > 1]
    retry_attempts = sum(len(records) - 1 for records in retried)
    retry_items: list[str] = []
    for records in retried:
        latest = records[-1]
        task = latest.get("task", {})
        subject = latest.get("subject") or {}
        audit = latest.get("audit", {})
        label = str(subject.get("label") or task.get("label") or task.get("key") or "未知任务")
        state = "成功" if audit.get("status") == "succeeded" else "失败"
        retry_items.append(f"<li>{escape(label)} · {len(records)} 次尝试 · {state}</li>")
    retry_details = f'<ul class="global-detail">{"".join(retry_items)}</ul>' if retry_items else ""

    if gate_state == "pending":
        gate_summary = "等待门禁节点执行"
        gate_tone = "neutral"
    elif gate_state == "running":
        gate_summary = "正在执行门禁校验"
        gate_tone = "running"
    elif gate_state == "failed":
        gate_summary = "门禁节点执行失败"
        gate_tone = "failed"
    elif gate_errors:
        gate_summary = f"未通过，共 {len(gate_errors)} 个错误"
        gate_tone = "failed"
    else:
        gate_summary = "已通过，无门禁错误"
        gate_tone = "passed"
    gate_items = "".join(
        f"<li><strong>{escape(str(item.get('code', 'error')))}</strong> · "
        f"{escape(str(item.get('message', '')))}</li>"
        for item in gate_errors
    )
    gate_details = f'<ul class="global-detail">{gate_items}</ul>' if gate_items else ""

    return (
        '<div class="global-info"><style>'
        ".global-info{font-family:system-ui;display:grid;grid-template-columns:repeat(2,minmax(0,1fr));"
        "gap:8px;padding:4px 0 12px}.global-card{border:1px solid #dbe3ec;border-radius:8px;"
        "padding:10px 12px;background:#fff;min-width:0}.global-card h4{margin:0 0 5px;"
        "font-size:13px;"
        "color:#334155}.global-card p{margin:2px 0;font-size:12px;color:#475569;line-height:1.45}"
        ".global-card .status{font-weight:650;color:#334155}.global-card.passed .status{"
        "color:#15803d}"
        ".global-card.running .status{color:#b45309}.global-card.failed .status{color:#b91c1c}"
        ".global-card .scope{color:#64748b}.global-detail{margin:7px 0 0;padding:7px 0 0 18px;"
        "border-top:1px solid #e2e8f0}.global-detail li{font-size:12px;color:#334155;margin:3px 0}"
        "@media(max-width:700px){.global-info{grid-template-columns:1fr}}"
        "</style>"
        f'<section class="global-card {gate_tone}"><h4>门禁错误</h4>'
        f'<p class="status">{escape(gate_summary)}</p>{gate_details}</section>'
        '<section class="global-card"><h4>LangChain LLM Provider 重试</h4>'
        f'<p class="status">最多额外重试 {escape(provider_policy)} 次</p>'
        '<p class="scope">实际重试由 Provider SDK 内部执行，当前事件不可观测。</p></section>'
        '<section class="global-card"><h4>相同输入、Prompt 和 Schema 重试</h4>'
        f'<p class="status">最多额外重试 {escape(schema_policy)} 次 · 最大尝试 '
        f"{escape(schema_max_attempts)} 次</p>"
        f'<p class="scope">本次已重试 {len(retried)} 个逻辑调用，共 {retry_attempts} 个额外 '
        f"Attempt。</p>{retry_details}</section>"
        '<section class="global-card"><h4>Temporal Activity 重试</h4>'
        f'<p class="status">最多额外重试 {planner_settings.temporal_activity_max_retries} 次 · '
        f"最大尝试 {planner_settings.activity_max_attempts} 次</p>"
        '<p class="scope">本次 Gradio 直连 LangGraph，未经过 Temporal，因此不适用；'
        "该策略用于 API/Job 执行。</p>"
        "</section></div>"
    )


def example_values(kind: str) -> tuple[str, str, str, str, str]:
    example = EXAMPLES[kind]
    return (
        example["raw_text"],
        example["project_name"],
        example["description"],
        example["domain"],
        example["users"],
    )


def llm_task_choices(records: list[dict[str, Any]]) -> list[tuple[str, str]]:
    choices: list[tuple[str, str]] = []
    seen: set[str] = set()
    for record in records:
        task = cast(dict[str, str], record["task"])
        key = task["key"]
        if key not in seen:
            choices.append((task["label"], key))
            seen.add(key)
    return choices


def llm_invocation_choices(records: list[dict[str, Any]], task_key: str) -> list[tuple[str, str]]:
    latest: dict[str, dict[str, Any]] = {}
    for record in records:
        if record["task"]["key"] == task_key:
            latest[str(record["invocation_id"])] = record
    choices: list[tuple[str, str]] = []
    for invocation_id, record in latest.items():
        subject = cast(dict[str, str] | None, record.get("subject"))
        call_index = int(record["call_index"])
        if subject:
            label = f"{subject['key']} · {subject['label']} · 调用 {call_index}"
        else:
            label = f"{record['task']['label']} · 调用 {call_index}"
        choices.append((label, invocation_id))
    return choices


def llm_attempt_choices(records: list[dict[str, Any]], invocation_id: str) -> list[tuple[str, str]]:
    choices: list[tuple[str, str]] = []
    for record in records:
        if record["invocation_id"] != invocation_id:
            continue
        audit = cast(dict[str, Any], record["audit"])
        state = "成功" if audit["status"] == "succeeded" else "失败"
        value = f"{invocation_id}:{audit['attempt']}"
        choices.append((f"Attempt {audit['attempt']} · {state} · {audit['elapsed_ms']}ms", value))
    return choices


def _default_invocation(records: list[dict[str, Any]], task_key: str) -> str | None:
    latest: dict[str, dict[str, Any]] = {}
    for record in records:
        if record["task"]["key"] == task_key:
            latest[str(record["invocation_id"])] = record
    failed = [
        invocation_id
        for invocation_id, record in latest.items()
        if record["audit"]["status"] == "failed"
    ]
    candidates = failed or list(latest)
    return candidates[-1] if candidates else None


def _selected_record(
    records: list[dict[str, Any]], invocation_id: str, attempt_value: str | None = None
) -> dict[str, Any] | None:
    matches = [record for record in records if record["invocation_id"] == invocation_id]
    if not matches:
        return None
    if attempt_value is None:
        return matches[-1]
    return next(
        (
            record
            for record in matches
            if f"{invocation_id}:{record['audit']['attempt']}" == attempt_value
        ),
        matches[-1],
    )


def _llm_details(record: dict[str, Any] | None) -> tuple[str, Any, Any, Any, str, Any]:
    if record is None:
        return "", {}, {}, {}, "", {}
    audit = cast(dict[str, Any], record["audit"])
    control = cast(dict[str, Any], record["control"])
    business = cast(dict[str, Any], record["business"])
    state = "成功" if audit["status"] == "succeeded" else "失败"
    summary = (
        f"**{state}** · `{control['model']}` · {audit['elapsed_ms']}ms · "
        f"Attempt {audit['attempt']}/{audit['max_attempts']}"
    )
    control_view = {key: value for key, value in control.items() if key != "system_prompt"}
    return (
        summary,
        business["input"],
        business.get("output") or {},
        control_view,
        str(control["system_prompt"]),
        audit,
    )


def select_llm_task(
    records: list[dict[str, Any]], task_key: str | None
) -> tuple[Any, Any, str, Any, Any, Any, str, Any]:
    if not task_key:
        return (
            gr.Dropdown(choices=[], value=None),
            gr.Dropdown(choices=[], value=None),
            *_llm_details(None),
        )
    invocation_choices = llm_invocation_choices(records, task_key)
    invocation_id = _default_invocation(records, task_key)
    attempt_choices = llm_attempt_choices(records, invocation_id) if invocation_id else []
    attempt_value = attempt_choices[-1][1] if attempt_choices else None
    record = _selected_record(records, invocation_id, attempt_value) if invocation_id else None
    return (
        gr.Dropdown(choices=invocation_choices, value=invocation_id),
        gr.Dropdown(choices=attempt_choices, value=attempt_value),
        *_llm_details(record),
    )


def select_llm_invocation(
    records: list[dict[str, Any]], invocation_id: str | None
) -> tuple[Any, str, Any, Any, Any, str, Any]:
    if not invocation_id:
        return gr.Dropdown(choices=[], value=None), *_llm_details(None)
    attempt_choices = llm_attempt_choices(records, invocation_id)
    attempt_value = attempt_choices[-1][1] if attempt_choices else None
    return (
        gr.Dropdown(choices=attempt_choices, value=attempt_value),
        *_llm_details(_selected_record(records, invocation_id, attempt_value)),
    )


def select_llm_attempt(
    records: list[dict[str, Any]], invocation_id: str | None, attempt_value: str | None
) -> tuple[str, Any, Any, Any, str, Any]:
    if not invocation_id:
        return _llm_details(None)
    return _llm_details(_selected_record(records, invocation_id, attempt_value))


async def run_planner(
    raw_text: str,
    project_name: str,
    project_description: str,
    business_domain: str,
    target_users: str,
) -> AsyncIterator[tuple[Any, ...]]:
    started = time.monotonic()
    normalized: dict[str, Any] = {}
    intents: list[dict[str, Any]] = []
    requirements: list[dict[str, Any]] = []
    requirements_display: Any = requirements
    dependencies: dict[str, Any] = {}
    findings: list[dict[str, Any]] = []
    ambiguities: list[dict[str, Any]] = []
    gate_state = "pending"
    llm_calls: list[dict[str, Any]] = []
    statuses = {stage: "pending" for stage in PLANNER_STAGES}
    stage_started: dict[str, float] = {}
    stage_elapsed: dict[str, float] = {}
    current_stage = ""
    llm_details_initialized = False

    def view(
        status_text: str,
        *,
        reset_details: bool = False,
        initialize_llm_details: bool = False,
    ) -> tuple[Any, ...]:
        nonlocal llm_details_initialized
        for stage, state in statuses.items():
            if state == "running" and stage in stage_started:
                stage_elapsed[stage] = time.monotonic() - stage_started[stage]
        if reset_details:
            llm_details_initialized = False
            task_update = gr.update(choices=[], value=None)
            details: tuple[Any, ...] = (
                gr.update(choices=[], value=None),
                gr.update(choices=[], value=None),
                "",
                {},
                {},
                {},
                "",
                {},
            )
        elif initialize_llm_details and llm_calls and not llm_details_initialized:
            task_key = str(llm_calls[-1]["task"]["key"])
            task_update = gr.update(choices=llm_task_choices(llm_calls), value=task_key)
            details = select_llm_task(llm_calls, task_key)
            llm_details_initialized = True
        else:
            task_update = gr.update(choices=llm_task_choices(llm_calls))
            details = (gr.skip(),) * 8
        return (
            status_text,
            _json(normalized),
            _json(intents),
            _json(requirements_display),
            _json(dependencies),
            validation_report_html(findings, ambiguities, gate_state),
            list(llm_calls),
            task_update,
            workflow_graph_html(statuses, stage_elapsed),
            *details,
            global_runtime_info_html(findings, llm_calls, gate_state),
        )

    if len(raw_text.strip()) < 3:
        yield view("失败 · 原始需求至少需要 3 个字符", reset_details=True)
        return
    try:
        yield view("准备运行 · 正在冻结输入", reset_details=True)
        async for event in run_graph_stream(
            raw_text.strip(),
            _context(project_name, project_description, business_domain, target_users),
        ):
            if event["type"] == "stage":
                stage = str(event["stage"])
                current_stage = stage
                statuses[stage] = str(event["status"])
                if stage == "validate_and_gate":
                    gate_state = (
                        "running"
                        if event["status"] == "running"
                        else "failed"
                        if event["status"] == "failed"
                        else "completed"
                    )
                if event["status"] == "running":
                    stage_started[stage] = time.monotonic()
                    stage_elapsed[stage] = 0
                    yield view(_stage_text(stage, "running", time.monotonic() - started))
                    continue
                if event["status"] == "failed":
                    if stage in stage_started:
                        stage_elapsed[stage] = time.monotonic() - stage_started[stage]
                    yield view(f"失败 · {event.get('error', stage)}")
                    continue
                if stage in stage_started:
                    stage_elapsed[stage] = time.monotonic() - stage_started[stage]
                data = event.get("data")
                if stage == "normalize_requirement":
                    normalized = cast(dict[str, Any], data)
                elif stage == "extract_business_intents":
                    intents = cast(list[dict[str, Any]], data)
                elif stage in {"decompose_candidates", "enrich_requirements"}:
                    requirements = cast(list[dict[str, Any]], data)
                    requirements_display = requirements
                elif stage == "analyze_dependencies":
                    dependencies = cast(dict[str, Any], data)
                elif stage == "validate_and_gate":
                    findings = cast(list[dict[str, Any]], data)
                yield view(_stage_text(stage, "succeeded", time.monotonic() - started))
                continue
            if event["type"] == "llm_call":
                record = cast(dict[str, Any], event["data"])
                llm_calls.append(record)
                audit = cast(dict[str, Any], record["audit"])
                yield view(
                    f"LLM 调用完成 · {record['task']['label']} · {audit['elapsed_ms']}ms",
                    initialize_llm_details=True,
                )
                continue
            result = cast(dict[str, Any], event["data"])
            state = "需要澄清" if result["ambiguities"] else "完成"
            normalized = cast(dict[str, Any], result["normalized"])
            intents = cast(list[dict[str, Any]], result["intents"])
            ambiguities = cast(list[dict[str, Any]], result["ambiguities"])
            requirements_display = {
                "requirements": result["requirements"],
                "ambiguities": ambiguities,
            }
            dependencies = cast(dict[str, Any], result["dependencies"])
            findings = cast(list[dict[str, Any]], result["findings"])
            yield view(f"{state} · 总耗时 {time.monotonic() - started:.1f}s")
    except Exception as error:
        if current_stage:
            statuses[current_stage] = "failed"
            if current_stage in stage_started:
                stage_elapsed[current_stage] = time.monotonic() - stage_started[current_stage]
            if current_stage == "validate_and_gate":
                gate_state = "failed"
        yield view(f"失败 · {type(error).__name__}: {error}")


def gradio_theme() -> Any:
    return gr.themes.Default().set(
        input_border_color_focus="*border_color_primary",
        input_shadow_focus="none",
        checkbox_border_color_focus="*checkbox_border_color",
    )


def build_demo() -> gr.Blocks:
    with gr.Blocks(title="Kanx 全栈上下文编排器") as demo:
        gr.Markdown("# 原始需求 → 敏捷业务需求\n输入自然语言需求，逐阶段生成可验收的业务需求草稿。")
        with gr.Row():
            with gr.Column(scale=2):
                raw_text = gr.Textbox(
                    label="原始用户需求",
                    lines=12,
                    placeholder="例如：用户可以收藏商品，并在商品降价时收到提醒。",
                )
                with gr.Row():
                    project_name = gr.Textbox(label="项目名称")
                    business_domain = gr.Textbox(label="业务领域")
                project_description = gr.Textbox(label="项目描述", lines=3)
                target_users = gr.Textbox(label="目标用户（逗号分隔）")
                gr.Markdown("### 示例输入")
                with gr.Row():
                    simple_example = gr.Button("单一功能需求")
                    complex_example = gr.Button("多目标复杂需求")
                    ambiguous_example = gr.Button("高歧义需求")
                with gr.Row():
                    run = gr.Button("运行 Workflow", variant="primary")
                    clear = gr.ClearButton()
            with gr.Column(scale=1):
                status = gr.Textbox(label="运行状态", lines=3, interactive=False)
                validation_report = gr.HTML(value=validation_report_html(), label="门禁与校验")
        global_info = gr.HTML(value=global_runtime_info_html(), label="全局运行信息")
        workflow_graph = gr.HTML(value=workflow_graph_html(), label="Workflow 图")
        with gr.Tab("规范化需求"):
            normalized = gr.Code(
                label="Normalized Requirement", language="json", lines=18, interactive=False
            )
        with gr.Tab("业务目标"):
            intents = gr.Code(
                label="Business Intents", language="json", lines=18, interactive=False
            )
        with gr.Tab("敏捷业务需求"):
            requirements = gr.Code(
                label="Agile Requirements", language="json", lines=28, interactive=False
            )
        with gr.Tab("依赖分析"):
            dependencies = gr.Code(
                label="Dependency Analysis", language="json", lines=18, interactive=False
            )
        llm_call_state = gr.State([])
        with gr.Accordion("LLM 调用详情", open=False):
            with gr.Row():
                llm_task = gr.Dropdown(label="LLM 任务", choices=[], interactive=True)
                llm_invocation = gr.Dropdown(label="调用实例", choices=[], interactive=True)
                llm_attempt = gr.Dropdown(label="Attempt", choices=[], interactive=True)
            llm_summary = gr.Markdown()
            with gr.Tab("业务语义"):
                with gr.Row():
                    llm_business_input = gr.JSON(label="业务输入", open=False)
                    llm_business_output = gr.JSON(label="结构化输出", open=False)
            with gr.Tab("系统控制"):
                llm_control = gr.JSON(label="调用参数", open=False)
                with gr.Accordion("System Prompt", open=False):
                    llm_system_prompt = gr.Code(
                        label="System Prompt",
                        language=None,
                        lines=5,
                        interactive=False,
                        show_line_numbers=False,
                    )
            with gr.Tab("审计治理"):
                llm_audit = gr.JSON(label="审计信息", open=False)
        llm_detail_outputs = [
            llm_invocation,
            llm_attempt,
            llm_summary,
            llm_business_input,
            llm_business_output,
            llm_control,
            llm_system_prompt,
            llm_audit,
        ]
        outputs = [
            status,
            normalized,
            intents,
            requirements,
            dependencies,
            validation_report,
            llm_call_state,
            llm_task,
            workflow_graph,
            *llm_detail_outputs,
            global_info,
        ]
        run.click(
            run_planner,
            [raw_text, project_name, project_description, business_domain, target_users],
            outputs,
        )
        clear.add(
            [
                raw_text,
                project_name,
                project_description,
                business_domain,
                target_users,
                *outputs[:-1],
            ]
        )
        clear.click(lambda: global_runtime_info_html(), outputs=global_info)
        llm_task.change(
            select_llm_task,
            [llm_call_state, llm_task],
            llm_detail_outputs,
        )
        llm_invocation.change(
            select_llm_invocation,
            [llm_call_state, llm_invocation],
            [llm_attempt, *llm_detail_outputs[2:]],
        )
        llm_attempt.change(
            select_llm_attempt,
            [llm_call_state, llm_invocation, llm_attempt],
            llm_detail_outputs[2:],
        )
        example_inputs = [
            raw_text,
            project_name,
            project_description,
            business_domain,
            target_users,
        ]
        simple_example.click(lambda: example_values("simple"), outputs=example_inputs)
        complex_example.click(lambda: example_values("complex"), outputs=example_inputs)
        ambiguous_example.click(lambda: example_values("ambiguous"), outputs=example_inputs)
    return cast(gr.Blocks, demo)


def main() -> None:
    settings = get_settings()
    build_demo().launch(
        server_name=settings.gradio_host,
        server_port=settings.gradio_port,
        theme=gradio_theme(),
        css=GRADIO_FOCUS_CSS,
    )


if __name__ == "__main__":
    main()
