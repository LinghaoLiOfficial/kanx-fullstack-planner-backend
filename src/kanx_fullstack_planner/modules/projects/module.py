from ...core.modules import MigrationDescriptor, ModuleSpec
from .api import router
from .models import Project

module = ModuleSpec(
    name="projects",
    requires=("database", "users", "auth"),
    routers=(router,),
    models=(Project,),
    migrations=(MigrationDescriptor("projects"),),
)
