from collections.abc import AsyncIterator, Sequence
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any

from fastapi import FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware

from .config import Settings, get_settings
from .errors import install_error_handling
from .health import Check, readiness
from .logging import configure_logging
from .modules import ModuleSpec, load_modules, validate_module_registrations
from .observability import configure_tracing


def create_app(
    settings: Settings | None = None, modules: Sequence[ModuleSpec] | None = None
) -> FastAPI:
    configured = settings or get_settings()
    configured_modules = tuple(modules) if modules is not None else load_modules(configured)
    validate_module_registrations(configured_modules)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configure_logging(configured)
        configure_tracing(configured)
        app.state.settings = configured
        app.state.modules = tuple(module.name for module in configured_modules)
        app.state.module_settings = {
            module.name: module.settings_factory()
            for module in configured_modules
            if module.settings_factory is not None
        }
        app.state.permissions = frozenset(
            permission for module in configured_modules for permission in module.permissions
        )
        app.state.job_handlers = {
            (handler.name, handler.version): handler.handler
            for module in configured_modules
            for handler in module.job_handlers
        }
        async with AsyncExitStack() as stack:
            for module in configured_modules:
                if module.lifespan is not None:
                    await stack.enter_async_context(module.lifespan(app, configured))
            yield

    app = FastAPI(title=configured.app_name, version=configured.app_version, lifespan=lifespan)
    install_error_handling(app, configured)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(configured.cors_origins),
        allow_credentials=True,
        allow_methods=["DELETE", "GET", "OPTIONS", "PATCH", "POST", "PUT"],
        allow_headers=[
            "Authorization",
            "Content-Type",
            "X-CSRF-Token",
            "X-Request-ID",
        ],
        expose_headers=["X-Request-ID"],
    )
    for module in configured_modules:
        for router in module.routers:
            app.include_router(router)

    @app.get("/")
    async def root() -> dict[str, str]:
        return {"name": configured.app_name, "status": "ok", "docs": "/docs"}

    @app.get("/health/live")
    async def live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready")
    async def ready(response: Response) -> dict[str, Any]:
        checks: dict[str, Check] = {}
        for module in configured_modules:
            if module.health_checks is not None:
                checks.update(module.health_checks(app))
        body, status = await readiness(checks, configured.health_timeout_seconds)
        response.status_code = status
        return body

    return app
