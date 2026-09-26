import os
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from pydantic import Field, field_validator, model_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)


class AppEnvironment(StrEnum):
    LOCAL = "local"
    STAGING = "staging"
    PRODUCTION = "production"


def configured_environment() -> AppEnvironment:
    raw = os.getenv("APP_ENV", AppEnvironment.LOCAL).lower()
    return AppEnvironment.LOCAL if raw == "development" else AppEnvironment(raw)


class EnvironmentSettings(BaseSettings):
    """Common environment, mounted-secret, and local dotenv source policy."""

    model_config = SettingsConfigDict(env_file_encoding="utf-8", extra="ignore")

    def __init__(self, **values: Any) -> None:
        if "_env_file" not in values:
            values["_env_file"] = (
                (".env", ".env.local") if configured_environment() == AppEnvironment.LOCAL else None
            )
        if "_secrets_dir" not in values:
            values["_secrets_dir"] = "/run/secrets" if Path("/run/secrets").is_dir() else None
        super().__init__(**values)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        del settings_cls
        return init_settings, env_settings, file_secret_settings, dotenv_settings


class AppProfile(StrEnum):
    API = "api"
    WORKFLOW = "workflow"
    AI = "ai"
    FULL = "full"
    CUSTOM = "custom"
    IDENTITY = "identity"
    SAAS = "saas"


class Settings(EnvironmentSettings):
    app_name: str = "kanx-fullstack-planner"
    app_slug: str = "kanx-fullstack-planner"
    app_version: str = "0.1.0"
    app_env: AppEnvironment = AppEnvironment.LOCAL
    app_profile: AppProfile = AppProfile.API
    log_level: str = "INFO"
    log_file: str | None = None
    log_file_max_bytes: int = Field(default=10_485_760, ge=0)
    log_file_backup_count: int = Field(default=5, ge=0, le=100)
    log_redact_fields: str = "password,token,secret,api_key,authorization,cookie"
    otel_enabled: bool = False
    otel_service_name: str | None = None
    otel_exporter_otlp_endpoint: str | None = None
    observability_exporter_url: str | None = None
    observability_exporter_kind: str = "otlp"
    observability_exporter_timeout_seconds: float = Field(default=2.0, gt=0, le=30)

    database_url: str = "postgresql+asyncpg://foundation:foundation@localhost:55432/kanx_fullstack_planner"
    database_user: str = "foundation"
    database_password: str = "foundation"
    database_name: str = "kanx_fullstack_planner"
    database_host_port: int = Field(default=55432, ge=1, le=65535)
    compose_project_name: str | None = None

    temporal_host: str = "localhost:7233"
    temporal_host_port: int = Field(default=7233, ge=1, le=65535)
    temporal_ui_port: int = Field(default=8233, ge=1, le=65535)
    temporal_ui_url: str = "http://localhost:8233"
    temporal_namespace: str = "kanx-fullstack-planner"
    temporal_task_queue: str = "kanx-fullstack-planner-worker"
    temporal_auto_register_namespace: bool | None = None

    email_smtp_host_port: int = Field(default=1025, ge=1, le=65535)
    email_ui_port: int = Field(default=8025, ge=1, le=65535)
    storage_host_port: int = Field(default=9000, ge=1, le=65535)
    storage_console_port: int = Field(default=9001, ge=1, le=65535)

    jobs_poll_interval_seconds: float = Field(default=1.0, gt=0, le=60)
    jobs_batch_size: int = Field(default=20, ge=1, le=500)

    api_host: str = "127.0.0.1"
    api_port: int = Field(default=8000, ge=1, le=65535)
    cors_allowed_origins: str = ""
    gradio_host: str = "127.0.0.1"
    gradio_port: int = Field(default=7860, ge=1, le=65535)
    health_timeout_seconds: float = Field(default=3.0, gt=0, le=60)

    @field_validator("app_env", mode="before")
    @classmethod
    def migrate_development_environment(cls, value: object) -> object:
        return AppEnvironment.LOCAL if value == "development" else value

    @model_validator(mode="after")
    def validate_deployment_database(self) -> "Settings":
        if self.deployed:
            parsed = urlparse(self.database_url.replace("+asyncpg", ""))
            if not parsed.password:
                raise ValueError("deployed DATABASE_URL must include a non-empty password")
            if any(origin.startswith("http://") for origin in self.cors_origins):
                raise ValueError("deployed CORS_ALLOWED_ORIGINS must use HTTPS")
        return self

    @field_validator("app_slug")
    @classmethod
    def validate_slug(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not normalized or any(
            character not in "abcdefghijklmnopqrstuvwxyz0123456789-_" for character in normalized
        ):
            raise ValueError(
                "app_slug may contain only lowercase letters, digits, hyphens, and underscores"
            )
        return normalized

    @property
    def production(self) -> bool:
        return self.app_env == AppEnvironment.PRODUCTION

    @property
    def deployed(self) -> bool:
        return self.app_env in (AppEnvironment.STAGING, AppEnvironment.PRODUCTION)

    @property
    def docker_project_name(self) -> str:
        return self.compose_project_name or self.app_slug

    @property
    def cors_origins(self) -> tuple[str, ...]:
        origins = tuple(
            item.strip() for item in self.cors_allowed_origins.split(",") if item.strip()
        )
        if "*" in origins:
            raise ValueError(
                "CORS_ALLOWED_ORIGINS may not contain '*' when credentials are enabled"
            )
        return origins

    @property
    def auto_register_namespace(self) -> bool:
        return (
            self.temporal_auto_register_namespace
            if self.temporal_auto_register_namespace is not None
            else not self.production
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
