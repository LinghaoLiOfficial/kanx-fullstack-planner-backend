from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager

from fastapi import FastAPI
from temporalio.client import Client

from ...core.config import Settings
from ...core.health import Check
from ...core.modules import ComposeCapability, ModuleSpec
from .workflows import SmokeWorkflow, smoke_activity


@asynccontextmanager
async def temporal_lifespan(app: FastAPI, settings: Settings) -> AsyncIterator[None]:
    app.state.temporal = await Client.connect(
        settings.temporal_host, namespace=settings.temporal_namespace
    )
    try:
        yield
    finally:
        app.state.temporal = None


def temporal_health_checks(app: FastAPI) -> Mapping[str, Check]:
    async def check() -> None:
        await app.state.temporal.service_client.check_health()

    return {"temporal": check}


module = ModuleSpec(
    name="temporal",
    requires=("database",),
    lifespan=temporal_lifespan,
    health_checks=temporal_health_checks,
    workflows=(SmokeWorkflow,),
    activities=(smoke_activity,),
    optional_dependencies=("temporalio>=1.18,<2",),
    compose_capabilities=(
        ComposeCapability("temporal"),
        ComposeCapability("temporal-ui"),
        ComposeCapability("namespace"),
    ),
)
