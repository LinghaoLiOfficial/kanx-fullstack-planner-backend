from functools import lru_cache
from typing import Any, Self

from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator

from ...core.config import EnvironmentSettings


class LLMConfig(BaseModel):
    base_url: str = "https://api.openai.com/v1"
    api_key: SecretStr = SecretStr("")
    model: str = "gpt-4.1-mini"
    connect_timeout_seconds: float = Field(default=5, gt=0, le=120)
    read_timeout_seconds: float = Field(default=60, gt=0, le=600)
    max_retries: int = Field(default=2, ge=0, le=10)
    temperature: float = Field(default=0, ge=0, le=2)
    max_tokens: int = Field(default=2048, ge=1)
    top_p: float | None = Field(default=None, ge=0, le=1)
    frequency_penalty: float | None = Field(default=None, ge=-2, le=2)
    presence_penalty: float | None = Field(default=None, ge=-2, le=2)
    seed: int | None = None
    stop_sequences: str | list[str] | None = None
    reasoning_effort: str | None = None
    verbosity: str | None = None
    service_tier: str | None = None
    organization: str | None = None
    default_headers: dict[str, str] = Field(default_factory=dict)
    default_query: dict[str, Any] = Field(default_factory=dict)
    extra_body: dict[str, Any] = Field(default_factory=dict)
    model_kwargs: dict[str, Any] = Field(default_factory=dict)


class LLMTaskConfig(BaseModel):
    """Partial LLM configuration; unset values inherit from the defaults."""

    base_url: str | None = None
    api_key: SecretStr | None = None
    model: str | None = None
    connect_timeout_seconds: float | None = Field(default=None, gt=0, le=120)
    read_timeout_seconds: float | None = Field(default=None, gt=0, le=600)
    max_retries: int | None = Field(default=None, ge=0, le=10)
    temperature: float | None = Field(default=None, ge=0, le=2)
    max_tokens: int | None = Field(default=None, ge=1)
    top_p: float | None = Field(default=None, ge=0, le=1)
    frequency_penalty: float | None = Field(default=None, ge=-2, le=2)
    presence_penalty: float | None = Field(default=None, ge=-2, le=2)
    seed: int | None = None
    stop_sequences: str | list[str] | None = None
    reasoning_effort: str | None = None
    verbosity: str | None = None
    service_tier: str | None = None
    organization: str | None = None
    default_headers: dict[str, str] | None = None
    default_query: dict[str, Any] | None = None
    extra_body: dict[str, Any] | None = None
    model_kwargs: dict[str, Any] | None = None


class AISettings(LLMConfig, EnvironmentSettings):
    model_config = EnvironmentSettings.model_config | {
        "env_prefix": "AI_",
        "env_nested_delimiter": "__",
    }

    task_configs: dict[str, LLMTaskConfig] = Field(default_factory=dict)

    @field_validator("task_configs")
    @classmethod
    def normalize_task_names(
        cls, task_configs: dict[str, LLMTaskConfig]
    ) -> dict[str, LLMTaskConfig]:
        return {name.strip().casefold(): config for name, config in task_configs.items()}

    @model_validator(mode="after")
    def reject_empty_task_names(self) -> Self:
        if "" in self.task_configs:
            raise ValueError("LLM task names must not be empty")
        return self

    def for_task(self, task: str | None = None) -> LLMConfig:
        defaults = LLMConfig.model_validate(
            self.model_dump(exclude={"task_configs"}, round_trip=True)
        )
        if task is None:
            return defaults
        task_name = task.strip().casefold()
        if not task_name:
            raise ValueError("LLM task name must not be empty")
        override = self.task_configs.get(task_name)
        if override is None:
            return defaults
        return defaults.model_copy(update=override.model_dump(exclude_none=True))


@lru_cache
def get_ai_settings() -> AISettings:
    return AISettings()
