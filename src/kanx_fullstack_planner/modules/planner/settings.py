from functools import lru_cache

from pydantic import Field

from ...core.config import EnvironmentSettings


class PlannerSettings(EnvironmentSettings):
    """Planner-specific Temporal retry policy."""

    model_config = EnvironmentSettings.model_config | {"env_prefix": "PLANNER_"}

    temporal_activity_max_retries: int = Field(
        default=1,
        ge=0,
        le=1,
        validation_alias="TEMPORAL_ACTIVITY_MAX_RETRIES",
    )

    @property
    def activity_max_attempts(self) -> int:
        """Temporal's total attempts: initial execution plus retries."""
        return self.temporal_activity_max_retries + 1


@lru_cache
def get_planner_settings() -> PlannerSettings:
    return PlannerSettings()
