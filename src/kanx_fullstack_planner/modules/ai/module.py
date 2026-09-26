from ...core.modules import ModuleSpec
from .settings import get_ai_settings

module = ModuleSpec(
    name="ai",
    requires=("database",),
    settings_factory=get_ai_settings,
    optional_env=(
        "AI_BASE_URL",
        "AI_API_KEY",
        "AI_MODEL",
        "AI_CONNECT_TIMEOUT_SECONDS",
        "AI_READ_TIMEOUT_SECONDS",
        "AI_MAX_RETRIES",
        "AI_TEMPERATURE",
        "AI_MAX_TOKENS",
        "AI_TASK_CONFIGS",
    ),
    optional_dependencies=(
        "langchain-core>=1.6,<2",
        "langchain-openai>=1.0,<2",
        "langgraph>=1.2,<2",
    ),
)
