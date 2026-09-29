from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kanx_fullstack_planner.app import app
from kanx_fullstack_planner.modules.users.models import User


def _register(client: TestClient, email: str, username: str) -> None:
    assert client.post("/v1/auth/register/code", json={"email": email}).status_code == 200
    response = client.post(
        "/v1/auth/register",
        json={
            "email": email,
            "username": username,
            "password": "Configuration123!",
            "verification_code": "123456",
        },
    )
    assert response.status_code == 200
    login = client.post(
        "/v1/auth/login",
        json={"email": email, "password": "Configuration123!"},
    )
    assert login.status_code == 200


async def _cleanup(factory: async_sessionmaker[AsyncSession], email: str) -> None:
    async with factory() as session:
        user = await session.scalar(select(User).where(User.email == email))
        if user is not None:
            await session.delete(user)
            await session.commit()


def test_project_configuration_can_be_read_and_updated() -> None:
    suffix = uuid4().hex
    email = f"configuration-{suffix}@example.test"
    with TestClient(app) as client:
        try:
            _register(client, email, f"configuration-{suffix}")
            created = client.post(
                "/v1/projects",
                json={"name": "Original", "description": "Original description"},
            )
            assert created.status_code == 200
            project_id = created.json()["id"]

            response = client.get(f"/v1/projects/{project_id}/configuration")
            assert response.status_code == 200
            assert response.json()["project_name"] == "Original"
            assert response.json()["target_frontend_stack_items"] == []

            updated = client.patch(
                f"/v1/projects/{project_id}/configuration",
                json={
                    "project_name": "Updated",
                    "project_description": "Updated description",
                    "target_frontend_stack_items": [
                        {"name": "Next.js", "type": "framework", "tags": [], "role": None}
                    ],
                    "target_backend_stack_items": [
                        {"name": "FastAPI", "type": "framework", "tags": [], "role": None}
                    ],
                },
            )
            assert updated.status_code == 200
            assert updated.json()["project_name"] == "Updated"
            assert updated.json()["target_frontend_stack_items"][0]["name"] == "Next.js"
            assert updated.json()["target_backend_stack_items"][0]["name"] == "FastAPI"
        finally:
            client.portal.call(_cleanup, client.app.state.session_factory, email)
