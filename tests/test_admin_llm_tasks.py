from __future__ import annotations

from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kanx_fullstack_planner.app import app
from kanx_fullstack_planner.modules.jobs.models import Job
from kanx_fullstack_planner.modules.planner.models import LLMInvocation, WorkflowRun, WorkflowStep
from kanx_fullstack_planner.modules.planner.service import PlannerService
from kanx_fullstack_planner.modules.projects.models import Project
from kanx_fullstack_planner.modules.users.models import User


def _register(client: TestClient, name: str) -> str:
    email = f"{name}@example.test"
    assert client.post("/v1/auth/register/code", json={"email": email}).status_code == 200
    assert (
        client.post(
            "/v1/auth/register",
            json={
                "email": email,
                "username": name,
                "password": "Monitoring123!",
                "verification_code": "123456",
            },
        ).status_code
        == 200
    )
    return email


async def _seed(factory: async_sessionmaker[AsyncSession], admin: str, owner: str) -> list[str]:
    async with factory() as session:
        admin_user = await session.scalar(select(User).where(User.email == admin))
        owner_user = await session.scalar(select(User).where(User.email == owner))
        assert admin_user and owner_user
        admin_user.role = "admin"
        projects = [Project(owner_user_id=owner_user.id, name=f"Project {n}") for n in range(2)]
        session.add_all(projects)
        await session.flush()
        jobs = [
            Job(
                job_type="planner.raw_to_agile",
                payload={},
                payload_hash="hash",
                idempotency_key=str(uuid4()),
                status="running",
            ),
            Job(
                job_type="planner.raw_to_agile",
                payload={},
                payload_hash="hash",
                idempotency_key=str(uuid4()),
                status="cancelled",
            ),
        ]
        session.add_all(jobs)
        await session.flush()
        runs = [
            WorkflowRun(
                project_id=project.id,
                job_id=job.id,
                raw_text=f"secret {n}",
                raw_text_hash="hash",
                status="running",
                project_context={},
            )
            for n, (project, job) in enumerate(zip(projects, jobs, strict=True))
        ]
        session.add_all(runs)
        await session.flush()
        session.add(
            WorkflowStep(run_id=runs[0].id, step_key="normalize_requirement", status="succeeded")
        )
        session.add(
            LLMInvocation(
                run_id=runs[0].id,
                task_name="normalize_requirement",
                model="test-model",
                prompt_version="1",
                schema_version="1",
                input_hash="hash",
                status="succeeded",
                raw_response={"secret": "output"},
            )
        )
        session.add(
            WorkflowStep(run_id=runs[1].id, step_key="normalize_requirement", status="running")
        )
        session.add(
            LLMInvocation(
                run_id=runs[1].id,
                task_name="normalize_requirement",
                model="test-model",
                prompt_version="1",
                schema_version="1",
                input_hash="hash",
                status="running",
            )
        )
        await session.commit()
        return [owner_user.id, projects[0].id, runs[0].id, runs[1].id]


async def _cleanup(
    factory: async_sessionmaker[AsyncSession], emails: list[str], run_ids: list[str]
) -> None:
    async with factory() as session:
        job_ids = []
        for run_id in run_ids:
            run = await session.get(WorkflowRun, run_id)
            if run:
                if run.job_id:
                    job_ids.append(run.job_id)
                await session.delete(run)
        await session.flush()
        for job_id in job_ids:
            job = await session.get(Job, job_id)
            if job:
                await session.delete(job)
        for email in emails:
            user = await session.scalar(select(User).where(User.email == email))
            if user:
                await session.delete(user)
        await session.commit()


def test_admin_monitor_access_filters_and_detail() -> None:
    suffix = uuid4().hex
    admin = f"monitor-admin-{suffix}"
    owner = f"monitor-user-{suffix}"
    with TestClient(app) as client:
        admin_email = _register(client, admin)
        owner_email = _register(client, owner)
        factory = client.app.state.session_factory
        owner_id, project_id, first, second = client.portal.call(
            _seed, factory, admin_email, owner_email
        )
        try:
            assert client.get("/v1/admin/llm-tasks").status_code == 401
            assert (
                client.post(
                    "/v1/auth/login", json={"email": owner_email, "password": "Monitoring123!"}
                ).status_code
                == 200
            )
            assert client.get("/v1/admin/llm-tasks").status_code == 403
            assert client.get(f"/v1/admin/llm-tasks/{first}").status_code == 403

            assert (
                client.post(
                    "/v1/auth/login", json={"email": admin_email, "password": "Monitoring123!"}
                ).status_code
                == 200
            )
            listing = client.get(
                "/v1/admin/llm-tasks", params={"user_id": owner_id, "page_size": 1}
            )
            assert listing.status_code == 200
            assert listing.json()["total"] == 2
            assert len(listing.json()["items"]) == 1
            assert "raw_text" not in listing.text
            assert "secret" not in listing.text
            filtered = client.get(
                "/v1/admin/llm-tasks", params={"project_id": project_id, "status": "running"}
            ).json()
            assert filtered["total"] == 1
            assert filtered["items"][0]["id"] == first
            assert filtered["items"][0]["progress"] > 0
            assert (
                client.get("/v1/admin/llm-tasks", params={"status": "cancelled"}).json()["items"][
                    0
                ]["id"]
                == second
            )
            details = client.get(f"/v1/admin/llm-tasks/{first}").json()
            assert details["raw_text"] == "secret 0"
            assert details["steps"][0]["status"] == "succeeded"
            assert details["invocations"][0]["input"] is None
            assert details["invocations"][0]["output"] == {"secret": "output"}
            cancelled = client.get(f"/v1/admin/llm-tasks/{second}").json()
            assert cancelled["status"] == "cancelled"
            assert cancelled["steps"][0]["status"] == "cancelled"
            assert cancelled["invocations"][0]["status"] == "cancelled"
            assert client.get(f"/v1/admin/llm-tasks/{uuid4()}").status_code == 404
        finally:
            client.portal.call(_cleanup, factory, [admin_email, owner_email], [first, second])


def test_llm_attempts_persist_running_failed_and_succeeded() -> None:
    with TestClient(app) as client:
        factory = client.app.state.session_factory

        async def exercise() -> None:
            async with factory() as session:
                run = WorkflowRun(project_id="test", raw_text="test", raw_text_hash="hash")
                session.add(run)
                await session.commit()
                run_id = run.id

            try:
                service = PlannerService()
                record = {
                    "invocation_id": str(uuid4()),
                    "task": {"key": "normalize_requirement"},
                    "call_index": 1,
                    "subject": None,
                    "business": {"input": {"raw_text": "private"}, "output": None},
                    "control": {
                        "model": "test",
                        "schema": "Example",
                        "provider_max_retries": 1,
                        "actual_prompt_recorded": True,
                        "system_prompt": "system",
                        "user_prompt": "user",
                    },
                    "audit": {
                        "attempt": 1,
                        "status": "running",
                        "call_type": "normal",
                        "max_attempts": 2,
                        "prompt_version": "1",
                        "schema_version": "1",
                        "input_hash": "hash",
                        "elapsed_ms": None,
                    },
                }
                await service.persist_llm_event(factory, run_id, record)
                async with factory() as session:
                    stored = await session.scalar(
                        select(LLMInvocation).where(LLMInvocation.run_id == run_id)
                    )
                    assert stored and stored.status == "running"
                    assert stored.input_payload == {"raw_text": "private"}
                record["audit"].update(
                    status="failed",
                    elapsed_ms=12,
                    finished_at="2026-01-01T00:00:00+00:00",
                    error={"message": "invalid schema"},
                )
                await service.persist_llm_event(factory, run_id, record)
                record["audit"].update(
                    attempt=2,
                    status="running",
                    call_type="schema_validation_retry",
                    elapsed_ms=None,
                )
                record["business"]["output"] = None
                await service.persist_llm_event(factory, run_id, record)
                record["audit"].update(
                    status="succeeded",
                    elapsed_ms=9,
                    error=None,
                    output_hash="result",
                )
                record["business"]["output"] = {"ok": True}
                await service.persist_llm_event(factory, run_id, record)
                async with factory() as session:
                    attempts = (
                        await session.scalars(
                            select(LLMInvocation)
                            .where(LLMInvocation.run_id == run_id)
                            .order_by(LLMInvocation.attempt)
                        )
                    ).all()
                    assert [(item.attempt, item.status) for item in attempts] == [
                        (1, "failed"),
                        (2, "succeeded"),
                    ]
                    assert attempts[1].raw_response == {"ok": True}
                    assert attempts[1].system_prompt == "system"
            finally:
                async with factory() as session:
                    run = await session.get(WorkflowRun, run_id)
                    if run:
                        await session.delete(run)
                        await session.commit()

        client.portal.call(exercise)
