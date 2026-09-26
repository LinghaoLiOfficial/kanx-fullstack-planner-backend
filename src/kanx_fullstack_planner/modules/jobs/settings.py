from functools import lru_cache

from pydantic import Field, SecretStr

from ...core.config import EnvironmentSettings


class JobsSettings(EnvironmentSettings):
    model_config = EnvironmentSettings.model_config | {"env_prefix": "JOBS_"}

    poll_interval_seconds: float = Field(default=1.0, gt=0, le=60)
    batch_size: int = Field(default=20, ge=1, le=500)
    activity_timeout_seconds: int = Field(default=600, ge=1, le=86400)
    maximum_attempts: int = Field(default=5, ge=1, le=100)
    global_concurrency: int = Field(default=100, ge=1, le=10000)
    tenant_concurrency: int = Field(default=10, ge=1, le=1000)
    lease_seconds: int = Field(default=60, ge=10, le=3600)
    max_batch_size: int = Field(default=1000, ge=1, le=10000)
    webhook_encryption_key: SecretStr = SecretStr("")


@lru_cache
def get_jobs_settings() -> JobsSettings:
    return JobsSettings()
