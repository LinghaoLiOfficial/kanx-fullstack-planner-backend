from functools import lru_cache

from pydantic import Field

from ...core.config import EnvironmentSettings


class AuthSettings(EnvironmentSettings):
    model_config = EnvironmentSettings.model_config | {"env_prefix": "AUTH_"}

    session_ttl_days: int = Field(default=30, ge=1, le=365)
    cookie_name: str = Field(default="kanx_session", min_length=1, max_length=128)
    secure_cookies: bool = False

    @property
    def session_ttl_seconds(self) -> int:
        return self.session_ttl_days * 24 * 60 * 60


@lru_cache
def get_auth_settings() -> AuthSettings:
    return AuthSettings()
