from ...core.modules import MigrationDescriptor, ModuleSpec
from .api import router
from .models import AuthSession
from .settings import get_auth_settings

module = ModuleSpec(
    name="auth",
    requires=("database", "users"),
    routers=(router,),
    models=(AuthSession,),
    migrations=(MigrationDescriptor("auth_sessions"),),
    settings_factory=get_auth_settings,
    optional_env=("AUTH_SESSION_TTL_DAYS", "AUTH_COOKIE_NAME", "AUTH_SECURE_COOKIES"),
)
