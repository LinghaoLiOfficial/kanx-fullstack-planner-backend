from ..ai.settings import get_ai_settings


def redact_error(value: str | None) -> str | None:
    if not value:
        return value
    settings = get_ai_settings()
    secrets = [settings.api_key.get_secret_value()]
    secrets.extend(
        config.api_key.get_secret_value()
        for config in settings.task_configs.values()
        if config.api_key is not None
    )
    for secret in secrets:
        if secret:
            value = value.replace(secret, "[REDACTED]")
    return value
