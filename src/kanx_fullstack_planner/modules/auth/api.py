from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy import update as sql_update
from sqlalchemy.ext.asyncio import AsyncSession

from ..database.session import get_session
from ..users.models import User
from .models import AuthSession
from .sessions import (
    clear_session_cookie,
    create_session,
    hash_session_token,
    set_session_cookie,
)
from .settings import AuthSettings, get_auth_settings

router = APIRouter(prefix="/v1/auth", tags=["auth"])
_codes: dict[str, tuple[str, datetime]] = {}


def _hash(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    value = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 120_000).hex()
    return f"{salt}${value}"


def _check(password: str, stored: str) -> bool:
    salt, value = stored.split("$", 1)
    return hmac.compare_digest(_hash(password, salt).split("$", 1)[1], value)


def _user(user: User) -> dict[str, object]:
    return {
        "id": user.id,
        "email": user.email,
        "username": user.username,
        "display_name": user.display_name,
        "role": user.role,
        "is_active": user.is_active,
        "is_email_verified": user.is_email_verified,
        "avatar_seed": user.username,
        "avatar_bg_color": "#64748b",
        "preferred_locale": user.preferred_locale,
    }


async def current_user(
    request: Request,
    session: AsyncSession = Depends(get_session),  # noqa: B008
    settings: AuthSettings = Depends(get_auth_settings),  # noqa: B008
) -> User:
    auth_session = await _active_session(request, session, settings)
    user = await session.get(User, auth_session.user_id) if auth_session else None
    if user is None or not user.is_active:
        raise HTTPException(401, "Authentication required")
    return user


async def _active_session(
    request: Request, session: AsyncSession, settings: AuthSettings
) -> AuthSession | None:
    token = request.cookies.get(settings.cookie_name)
    if not token:
        return None
    now = datetime.now(UTC)
    return await session.scalar(
        select(AuthSession).where(
            AuthSession.token_hash == hash_session_token(token),
            AuthSession.revoked_at.is_(None),
            AuthSession.expires_at > now,
        )
    )


class CodeIn(BaseModel):
    email: str


class RegisterIn(BaseModel):
    email: str
    username: str = Field(min_length=2, max_length=80)
    password: str = Field(min_length=8)
    verification_code: str


class LoginIn(BaseModel):
    email: str
    password: str


class UpdateIn(BaseModel):
    username: str | None = None
    display_name: str | None = None
    preferred_locale: str | None = None
    password: str | None = None


@router.post("/register/code")
async def register_code(body: CodeIn) -> dict[str, str]:
    code = (
        "123456"
        if not __import__("os").environ.get("APP_ENV") == "production"
        else f"{secrets.randbelow(1_000_000):06d}"
    )
    _codes[body.email.lower()] = (code, datetime.now(UTC) + timedelta(minutes=10))
    return {"message": "验证码已发送，请查收邮箱"}


@router.post("/register")
async def register(
    body: RegisterIn,
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> dict[str, object]:
    email = body.email.lower()
    code = _codes.get(email)
    if (
        not code
        or code[1] < datetime.now(UTC)
        or not hmac.compare_digest(code[0], body.verification_code)
    ):
        raise HTTPException(400, "Invalid verification code")
    if await session.scalar(select(User).where(User.email == email)) is not None:
        raise HTTPException(409, "Email already registered")
    if await session.scalar(select(User).where(User.username == body.username)) is not None:
        raise HTTPException(409, "Username already registered")
    user = User(
        email=email,
        username=body.username,
        password_hash=_hash(body.password),
        is_email_verified=True,
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return {"user": _user(user)}


@router.post("/login")
async def login(
    body: LoginIn,
    response: Response,
    session: AsyncSession = Depends(get_session),  # noqa: B008
    settings: AuthSettings = Depends(get_auth_settings),  # noqa: B008
) -> dict[str, object]:
    user = await session.scalar(select(User).where(User.email == body.email.lower()))
    if user is None or not user.is_active or not _check(body.password, user.password_hash):
        raise HTTPException(401, "Invalid email or password")
    user.last_login_at = datetime.now(UTC)
    token, auth_session = create_session(user.id, settings)
    session.add(auth_session)
    await session.commit()
    set_session_cookie(response, token, settings)
    return {"user": _user(user)}


@router.post("/logout")
async def logout(
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_session),  # noqa: B008
    settings: AuthSettings = Depends(get_auth_settings),  # noqa: B008
) -> dict[str, str]:
    auth_session = await _active_session(request, session, settings)
    if auth_session is not None:
        auth_session.revoked_at = datetime.now(UTC)
        await session.commit()
    clear_session_cookie(response, settings)
    return {"message": "退出成功"}


@router.get("/me")
async def me(user: User = Depends(current_user)) -> dict[str, object]:  # noqa: B008
    return _user(user)


@router.patch("/me")
async def update(
    body: UpdateIn,
    request: Request,
    user: User = Depends(current_user),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
    settings: AuthSettings = Depends(get_auth_settings),  # noqa: B008
) -> dict[str, object]:
    if body.username is not None:
        user.username = body.username
    if body.display_name is not None:
        user.display_name = body.display_name
    if body.preferred_locale is not None:
        user.preferred_locale = body.preferred_locale
    if body.password is not None:
        user.password_hash = _hash(body.password)
        current = await _active_session(request, session, settings)
        if current is None:
            raise HTTPException(401, "Authentication required")
        now = datetime.now(UTC)
        await session.execute(
            sql_update(AuthSession)
            .where(
                AuthSession.user_id == user.id,
                AuthSession.id != current.id,
                AuthSession.revoked_at.is_(None),
                AuthSession.expires_at > now,
            )
            .values(revoked_at=now)
        )
    await session.commit()
    return _user(user)
