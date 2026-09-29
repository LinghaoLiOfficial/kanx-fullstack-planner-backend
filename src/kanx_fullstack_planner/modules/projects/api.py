from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.api import current_user
from ..database.session import get_session
from ..users.models import User
from .description_graph import generate_description_options
from .models import Project

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/projects", tags=["projects"])


class ProjectIn(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    description: str | None = None
    llm_prompt_language: str = "zh-CN"
    target_users: list[str] = []
    business_domain: str | None = None
    frontend_tech_stack: list[str] = []
    backend_tech_stack: list[str] = []
    database_tech_stack: list[str] = []
    global_constraints: list[str] = []


class ProjectPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    description: str | None = None
    llm_prompt_language: str | None = None
    target_users: list[str] | None = None
    business_domain: str | None = None
    frontend_tech_stack: list[str] | None = None
    backend_tech_stack: list[str] | None = None
    database_tech_stack: list[str] | None = None
    global_constraints: list[str] | None = None


class ProjectDescriptionOptionsIn(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    llm_prompt_language: Literal["zh-CN", "en"] = "zh-CN"


class TechStackItemIn(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    type: str = "framework"
    tags: list[str] = []
    role: str | None = None


class ProjectConfigurationPatch(BaseModel):
    project_name: str | None = Field(default=None, min_length=1, max_length=160)
    project_description: str | None = None
    llm_prompt_language: Literal["zh-CN", "en"] | None = None
    target_frontend_stack_items: list[TechStackItemIn] | None = None
    target_backend_stack_items: list[TechStackItemIn] | None = None
    global_constraints: list[str] | None = None
    coding_preferences: list[str] | None = None
    prompt_preferences: list[str] | None = None


def out(p: Project) -> dict[str, object]:
    return {
        "id": p.id,
        "name": p.name,
        "description": p.description,
        "status": p.status,
        "llm_prompt_language": p.llm_prompt_language,
        "target_users": p.target_users,
        "business_domain": p.business_domain,
        "target_frontend_stack_items": [{"name": x} for x in p.frontend_tech_stack],
        "target_backend_stack_items": [{"name": x} for x in p.backend_tech_stack],
        "frontend_tech_stack": p.frontend_tech_stack,
        "backend_tech_stack": p.backend_tech_stack,
        "database_tech_stack": p.database_tech_stack,
        "global_constraints": p.global_constraints,
        "target_stacks_configured": bool(p.frontend_tech_stack or p.backend_tech_stack),
        "created_at": p.created_at,
        "updated_at": p.updated_at,
        "last_opened_at": p.last_opened_at,
    }


def configuration_out(p: Project) -> dict[str, object]:
    frontend_items: list[dict[str, object]] = [
        {"name": name, "type": "framework", "tags": [], "role": None}
        for name in p.frontend_tech_stack
    ]
    backend_items: list[dict[str, object]] = [
        {"name": name, "type": "framework", "tags": [], "role": None}
        for name in p.backend_tech_stack
    ]
    return {
        **out(p),
        "project_id": p.id,
        "project_name": p.name,
        "project_description": p.description,
        "target_frontend_stack": ", ".join(p.frontend_tech_stack),
        "target_backend_stack": ", ".join(p.backend_tech_stack),
        "target_frontend_stack_items": frontend_items,
        "target_backend_stack_items": backend_items,
        "frontend_tech_stack": ", ".join(p.frontend_tech_stack),
        "backend_tech_stack": ", ".join(p.backend_tech_stack),
        "coding_preferences": [],
        "code_preferences": [],
        "prompt_preferences": [],
    }


async def owned(project_id: str, user: User, session: AsyncSession) -> Project:
    p = await session.scalar(
        select(Project).where(Project.id == project_id, Project.owner_user_id == user.id)
    )
    if p is None:
        raise HTTPException(404, "Project not found")
    return p


@router.get("")
async def list_projects(
    user: User = Depends(current_user),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> list[dict[str, object]]:
    return [
        out(p)
        for p in (
            await session.scalars(
                select(Project)
                .where(Project.owner_user_id == user.id)
                .order_by(Project.updated_at.desc())
            )
        ).all()
    ]


@router.post("/description-options")
async def description_options(body: ProjectDescriptionOptionsIn) -> dict[str, list[dict[str, str]]]:
    name = body.name.strip()
    if not name:
        raise HTTPException(422, "Project name must not be blank")
    try:
        return {"options": await generate_description_options(name, body.llm_prompt_language)}
    except RuntimeError as error:
        if "AI_API_KEY is required" in str(error):
            raise HTTPException(503, "AI service is not configured") from error
        logger.exception("Project description generation failed")
        raise HTTPException(502, "Project description generation failed") from error
    except Exception as error:
        logger.exception("Project description generation failed")
        raise HTTPException(502, "Project description generation failed") from error


@router.post("")
async def create_project(
    body: ProjectIn,
    user: User = Depends(current_user),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> dict[str, object]:
    p = Project(owner_user_id=user.id, **body.model_dump())
    session.add(p)
    await session.commit()
    await session.refresh(p)
    return out(p)


@router.get("/{project_id}")
async def get_project(
    project_id: str,
    user: User = Depends(current_user),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> dict[str, object]:
    return out(await owned(project_id, user, session))


@router.get("/{project_id}/configuration")
async def get_project_configuration(
    project_id: str,
    user: User = Depends(current_user),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> dict[str, object]:
    return configuration_out(await owned(project_id, user, session))


@router.patch("/{project_id}/configuration")
async def patch_project_configuration(
    project_id: str,
    body: ProjectConfigurationPatch,
    user: User = Depends(current_user),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> dict[str, object]:
    project = await owned(project_id, user, session)
    values = body.model_dump(exclude_unset=True)
    if "project_name" in values:
        project.name = values["project_name"]
    if "project_description" in values:
        project.description = values["project_description"]
    if "llm_prompt_language" in values:
        project.llm_prompt_language = values["llm_prompt_language"]
    if "target_frontend_stack_items" in values:
        project.frontend_tech_stack = [
            item["name"].strip()
            for item in values["target_frontend_stack_items"]
            if item["name"].strip()
        ]
    if "target_backend_stack_items" in values:
        project.backend_tech_stack = [
            item["name"].strip()
            for item in values["target_backend_stack_items"]
            if item["name"].strip()
        ]
    if "global_constraints" in values:
        project.global_constraints = values["global_constraints"]
    project.updated_at = datetime.now(UTC)
    await session.commit()
    await session.refresh(project)
    return configuration_out(project)


@router.patch("/{project_id}")
async def patch_project(
    project_id: str,
    body: ProjectPatch,
    user: User = Depends(current_user),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> dict[str, object]:
    p = await owned(project_id, user, session)
    for key, value in body.model_dump(exclude_unset=True).items():
        setattr(p, key, value)
    p.updated_at = datetime.now(UTC)
    await session.commit()
    return out(p)


@router.post("/{project_id}/opened")
async def opened(
    project_id: str,
    user: User = Depends(current_user),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> dict[str, object]:
    p = await owned(project_id, user, session)
    p.last_opened_at = datetime.now(UTC)
    await session.commit()
    return out(p)


@router.delete("/{project_id}", status_code=204)
async def delete_project(
    project_id: str,
    user: User = Depends(current_user),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> None:
    p = await owned(project_id, user, session)
    await session.delete(p)
    await session.commit()
