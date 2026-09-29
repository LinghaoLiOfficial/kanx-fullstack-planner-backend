from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kanx_fullstack_planner.app import app
from kanx_fullstack_planner.modules.auth.models import AuthSession
from kanx_fullstack_planner.modules.auth.sessions import hash_session_token
from kanx_fullstack_planner.modules.users.models import User

COOKIE = "kanx_session"


def _credentials() -> tuple[str, str, str]:
    suffix = uuid4().hex
    return f"auth-{suffix}@example.test", f"auth-{suffix}", "password-123"


def _register(client: TestClient, email: str, username: str, password: str) -> None:
    assert client.post("/v1/auth/register/code", json={"email": email}).status_code == 200
    response = client.post(
        "/v1/auth/register",
        json={
            "email": email,
            "username": username,
            "password": password,
            "verification_code": "123456",
        },
    )
    assert response.status_code == 200


def _login(client: TestClient, email: str, password: str) -> tuple[str, str]:
    response = client.post("/v1/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200
    token = response.cookies.get(COOKIE)
    assert token
    return token, response.headers["set-cookie"]


def _use_token(client: TestClient, token: str) -> None:
    client.cookies.clear()
    client.cookies.set(COOKIE, token, domain="testserver.local", path="/")


async def _cleanup(factory: async_sessionmaker[AsyncSession], email: str) -> None:
    async with factory() as session:
        user = await session.scalar(select(User).where(User.email == email))
        if user is not None:
            await session.delete(user)
            await session.commit()


def test_login_session_survives_application_restart() -> None:
    email, username, password = _credentials()
    with TestClient(app) as client:
        _register(client, email, username, password)
        token, set_cookie = _login(client, email, password)
        assert "HttpOnly" in set_cookie
        assert "SameSite=lax" in set_cookie
        assert "Path=/" in set_cookie
        assert "Max-Age=2592000" in set_cookie

        async def inspect() -> None:
            async with client.app.state.session_factory() as session:
                stored = await session.scalar(
                    select(AuthSession).where(AuthSession.token_hash == hash_session_token(token))
                )
                assert stored is not None
                assert stored.token_hash != token
                assert len(stored.token_hash) == 64

        client.portal.call(inspect)

    with TestClient(app) as restarted:
        try:
            _use_token(restarted, token)
            response = restarted.get("/v1/auth/me")
            assert response.status_code == 200
            assert response.json()["email"] == email
        finally:
            restarted.portal.call(_cleanup, restarted.app.state.session_factory, email)


def test_logout_revokes_only_current_session_and_is_idempotent() -> None:
    email, username, password = _credentials()
    with TestClient(app) as client:
        factory = client.app.state.session_factory
        try:
            _register(client, email, username, password)
            first_token, _ = _login(client, email, password)
            second_token, _ = _login(client, email, password)

            _use_token(client, first_token)
            logout = client.post("/v1/auth/logout")
            assert logout.status_code == 200
            assert 'kanx_session=""' in logout.headers["set-cookie"]
            assert "Max-Age=0" in logout.headers["set-cookie"]
            assert client.get("/v1/auth/me").status_code == 401
            assert client.post("/v1/auth/logout").status_code == 200

            _use_token(client, second_token)
            assert client.get("/v1/auth/me").status_code == 200

            async def inspect() -> None:
                async with factory() as session:
                    rows = (
                        await session.scalars(
                            select(AuthSession).where(
                                AuthSession.token_hash.in_(
                                    [
                                        hash_session_token(first_token),
                                        hash_session_token(second_token),
                                    ]
                                )
                            )
                        )
                    ).all()
                    sessions = {item.token_hash: item for item in rows}
                    assert sessions[hash_session_token(first_token)].revoked_at is not None
                    assert sessions[hash_session_token(second_token)].revoked_at is None

            client.portal.call(inspect)
        finally:
            client.portal.call(_cleanup, factory, email)


def test_password_change_revokes_other_sessions_only() -> None:
    email, username, password = _credentials()
    with TestClient(app) as client:
        factory = client.app.state.session_factory
        try:
            _register(client, email, username, password)
            current_token, _ = _login(client, email, password)
            other_token, _ = _login(client, email, password)

            _use_token(client, current_token)
            assert client.patch("/v1/auth/me", json={"display_name": "Updated"}).status_code == 200
            _use_token(client, other_token)
            assert client.get("/v1/auth/me").status_code == 200

            _use_token(client, current_token)
            response = client.patch("/v1/auth/me", json={"password": "new-password-123"})
            assert response.status_code == 200
            assert client.get("/v1/auth/me").status_code == 200

            _use_token(client, other_token)
            assert client.get("/v1/auth/me").status_code == 401
            assert (
                client.post(
                    "/v1/auth/login", json={"email": email, "password": password}
                ).status_code
                == 401
            )
            assert (
                client.post(
                    "/v1/auth/login", json={"email": email, "password": "new-password-123"}
                ).status_code
                == 200
            )
        finally:
            client.portal.call(_cleanup, factory, email)


def test_invalid_expired_revoked_and_disabled_sessions_are_rejected() -> None:
    email, username, password = _credentials()
    with TestClient(app) as client:
        factory = client.app.state.session_factory
        try:
            _register(client, email, username, password)
            token, _ = _login(client, email, password)

            _use_token(client, "forged-token")
            assert client.get("/v1/auth/me").status_code == 401

            async def expire() -> None:
                async with factory() as session:
                    stored = await session.scalar(
                        select(AuthSession).where(
                            AuthSession.token_hash == hash_session_token(token)
                        )
                    )
                    assert stored is not None
                    stored.expires_at = datetime.now(UTC) - timedelta(seconds=1)
                    await session.commit()

            client.portal.call(expire)
            _use_token(client, token)
            assert client.get("/v1/auth/me").status_code == 401

            fresh_token, _ = _login(client, email, password)

            async def revoke() -> None:
                async with factory() as session:
                    stored = await session.scalar(
                        select(AuthSession).where(
                            AuthSession.token_hash == hash_session_token(fresh_token)
                        )
                    )
                    assert stored is not None
                    stored.revoked_at = datetime.now(UTC)
                    await session.commit()

            client.portal.call(revoke)
            _use_token(client, fresh_token)
            assert client.get("/v1/auth/me").status_code == 401

            active_token, _ = _login(client, email, password)

            async def disable_user() -> None:
                async with factory() as session:
                    user = await session.scalar(select(User).where(User.email == email))
                    assert user is not None
                    user.is_active = False
                    await session.commit()

            client.portal.call(disable_user)
            _use_token(client, active_token)
            assert client.get("/v1/auth/me").status_code == 401
        finally:
            client.portal.call(_cleanup, factory, email)
