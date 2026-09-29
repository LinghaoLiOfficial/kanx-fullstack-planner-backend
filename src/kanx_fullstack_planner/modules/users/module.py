from ...core.modules import MigrationDescriptor, ModuleSpec
from .models import User

module = ModuleSpec(name="users", models=(User,), migrations=(MigrationDescriptor("users"),))
