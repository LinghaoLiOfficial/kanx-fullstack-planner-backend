import asyncio
from collections.abc import Sequence

from temporalio.client import Client
from temporalio.worker import Worker

from ...core.config import Settings, get_settings
from ...core.installed_modules import INSTALLED_MODULES
from ...core.logging import configure_logging
from ...core.modules import ModuleSpec, load_modules
from ...core.observability import configure_tracing
from ..database.session import create_engine, create_session_factory
from ..jobs.worker import build_job_activity, build_schedule_activity, sync_job_types


async def run_worker(
    settings: Settings | None = None,
    modules: Sequence[ModuleSpec] | None = None,
) -> None:
    configured = settings or get_settings()
    configure_logging(configured)
    configure_tracing(configured)
    if "temporal" not in INSTALLED_MODULES:
        raise RuntimeError("Temporal worker requires a profile with the temporal module")
    configured_modules = tuple(modules) if modules is not None else load_modules(configured)
    workflows = [item for module in configured_modules for item in module.workflows]
    activities = [item for module in configured_modules for item in module.activities]
    handlers = tuple(item for module in configured_modules for item in module.job_handlers)
    engine = create_engine(configured)
    if any(module.name == "jobs" for module in configured_modules):
        factory = create_session_factory(engine)
        await sync_job_types(factory, handlers)
        activities.append(build_job_activity(factory, handlers))
        activities.append(build_schedule_activity(factory))
    if not workflows:
        raise RuntimeError("No Temporal workflows are registered")
    client = await Client.connect(configured.temporal_host, namespace=configured.temporal_namespace)
    try:
        async with Worker(
            client,
            task_queue=configured.temporal_task_queue,
            workflows=workflows,
            activities=activities,
        ):
            await asyncio.Event().wait()
    finally:
        await engine.dispose()


if __name__ == "__main__":
    try:
        asyncio.run(run_worker())
    except KeyboardInterrupt:
        pass
