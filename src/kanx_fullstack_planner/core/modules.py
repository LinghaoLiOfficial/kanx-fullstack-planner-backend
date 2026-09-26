# mypy: disable-error-code="import-untyped"
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, FastAPI

from .config import Settings
from .health import Check
from .installed_modules import INSTALLED_MODULES

ModuleLifespan = Callable[[FastAPI, Settings], AbstractAsyncContextManager[None]]
JobHandler = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any] | None]]
SettingsFactory = Callable[[], Any]


@dataclass(frozen=True)
class MigrationDescriptor:
    name: str


@dataclass(frozen=True)
class ComposeCapability:
    name: str


@dataclass(frozen=True)
class JobTypeSpec:
    name: str
    handler: JobHandler
    version: str = "1.0.0"
    timeout_seconds: int = 300
    maximum_attempts: int = 5
    initial_backoff_seconds: float = 1.0
    maximum_backoff_seconds: float = 300.0
    backoff_coefficient: float = 2.0
    default_priority: str = "normal"
    concurrency_limit: int | None = None


JobHandlerSpec = JobTypeSpec


@dataclass(frozen=True)
class ModuleSpec:
    name: str
    requires: tuple[str, ...] = ()
    routers: tuple[APIRouter, ...] = ()
    lifespan: ModuleLifespan | None = None
    health_checks: Callable[[FastAPI], Mapping[str, Check]] | None = None
    workflows: tuple[Any, ...] = ()
    activities: tuple[Any, ...] = ()
    models: tuple[type[Any], ...] = ()
    migrations: tuple[MigrationDescriptor, ...] = ()
    permissions: tuple[str, ...] = ()
    job_handlers: tuple[JobTypeSpec, ...] = ()
    settings_factory: SettingsFactory | None = None
    required_env: tuple[str, ...] = ()
    optional_env: tuple[str, ...] = ()
    optional_dependencies: tuple[str, ...] = ()
    compose_capabilities: tuple[ComposeCapability, ...] = ()


class ModuleConfigurationError(RuntimeError):
    pass


def resolve_modules(
    requested: Sequence[str], registry: Mapping[str, ModuleSpec]
) -> tuple[ModuleSpec, ...]:
    resolved: list[ModuleSpec] = []
    done: set[str] = set()
    visiting: set[str] = set()

    def visit(name: str) -> None:
        if name in done:
            return
        if name in visiting:
            raise ModuleConfigurationError(f"Module dependency cycle at {name}")
        if name not in registry:
            raise ModuleConfigurationError(f"Unknown module: {name}")
        visiting.add(name)
        for dependency in registry[name].requires:
            visit(dependency)
        visiting.remove(name)
        done.add(name)
        resolved.append(registry[name])

    for name in requested:
        visit(name)
    return tuple(resolved)


def validate_module_registrations(modules: tuple[ModuleSpec, ...]) -> None:
    registrations: dict[str, str] = {}
    for module in modules:
        entries = [
            *(f"permission:{item}" for item in module.permissions),
            *(f"job:{item.name}@{item.version}" for item in module.job_handlers),
            *(f"migration:{item.name}" for item in module.migrations),
            *(f"compose:{item.name}" for item in module.compose_capabilities),
        ]
        if module.settings_factory is not None:
            entries.append(f"settings:{module.name}")
        for entry in entries:
            owner = registrations.setdefault(entry, module.name)
            if owner != module.name:
                raise ModuleConfigurationError(
                    f"Duplicate module registration {entry!r}: {owner!r} and {module.name!r}"
                )


def load_modules(settings: Settings) -> tuple[ModuleSpec, ...]:
    del settings
    registry: dict[str, ModuleSpec] = {}
    from ..modules.database.module import module as database

    registry[database.name] = database
    if "temporal" in INSTALLED_MODULES:
        try:
            from ..modules.temporal.module import (
                module as temporal,
            )
        except ModuleNotFoundError as error:
            raise ModuleConfigurationError("Install the workflow extra for Temporal") from error
        registry[temporal.name] = temporal
    if "ai" in INSTALLED_MODULES:
        try:
            from ..modules.ai.module import (
                module as ai,
            )
        except ModuleNotFoundError as error:
            raise ModuleConfigurationError("Install the ai extra for LangGraph") from error
        registry[ai.name] = ai
    if "jobs" in INSTALLED_MODULES:
        from ..modules.jobs.module import module as jobs

        registry[jobs.name] = jobs
    if "users" in INSTALLED_MODULES:
        from ..modules.users.module import module as users

        registry[users.name] = users
    if "rbac" in INSTALLED_MODULES:
        from ..modules.rbac.module import module as rbac

        registry[rbac.name] = rbac
    if "email" in INSTALLED_MODULES:
        from ..modules.email.module import module as email_module

        registry[email_module.name] = email_module
    if "auth" in INSTALLED_MODULES:
        from ..modules.auth.module import module as auth

        registry[auth.name] = auth
    if "storage" in INSTALLED_MODULES:
        from ..modules.storage.module import module as storage

        registry[storage.name] = storage
    modules = resolve_modules(INSTALLED_MODULES, registry)
    validate_module_registrations(modules)
    return modules
