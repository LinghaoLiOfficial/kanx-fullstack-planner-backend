from __future__ import annotations

from typing import Any

from langchain_openai import ChatOpenAI
from pydantic import BaseModel

from .settings import AISettings, LLMConfig, get_ai_settings


def create_chat_model(settings: AISettings | None = None, *, task: str | None = None) -> ChatOpenAI:
    configured = (settings or get_ai_settings()).for_task(task)
    if not configured.api_key.get_secret_value():
        raise RuntimeError("AI_API_KEY is required to create a chat model")
    optional_parameters = _optional_parameters(configured)
    return ChatOpenAI(
        model=configured.model,
        api_key=configured.api_key,
        base_url=configured.base_url,
        timeout=(configured.connect_timeout_seconds, configured.read_timeout_seconds),
        max_retries=configured.max_retries,
        temperature=configured.temperature,
        max_completion_tokens=configured.max_tokens,
        **optional_parameters,
    )


def _optional_parameters(configured: LLMConfig) -> dict[str, Any]:
    names = (
        "top_p",
        "frequency_penalty",
        "presence_penalty",
        "seed",
        "stop_sequences",
        "reasoning_effort",
        "verbosity",
        "service_tier",
        "organization",
    )
    parameters = {name: value for name in names if (value := getattr(configured, name)) is not None}
    for name in ("default_headers", "default_query", "extra_body", "model_kwargs"):
        if value := getattr(configured, name):
            parameters[name] = value
    return parameters


async def structured_output[Schema: BaseModel](
    schema: type[Schema],
    messages: list[Any],
    settings: AISettings | None = None,
    *,
    task: str | None = None,
) -> Schema:
    model = create_chat_model(settings, task=task).with_structured_output(schema)
    result = await model.ainvoke(messages)
    return schema.model_validate(result)
