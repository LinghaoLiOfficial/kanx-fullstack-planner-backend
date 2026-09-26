from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from ...core.config import Settings
from ...core.health import Check
from ...core.modules import ComposeCapability, ModuleSpec
from .session import create_engine, create_session_factory


@asynccontextmanager
async def database_lifespan(app: FastAPI, settings: Settings) -> AsyncIterator[None]:
    engine = create_engine(settings)
    app.state.engine = engine
    app.state.session_factory = create_session_factory(engine)
    try:
        yield
    finally:
        await engine.dispose()


def database_health_checks(app: FastAPI) -> Mapping[str, Check]:
    async def check() -> None:
        engine: AsyncEngine = app.state.engine
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))

    return {"database": check}


module = ModuleSpec(
    name="database",
    lifespan=database_lifespan,
    health_checks=database_health_checks,
    compose_capabilities=(ComposeCapability("postgres"),),
)
