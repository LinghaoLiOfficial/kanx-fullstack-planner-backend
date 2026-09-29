from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime, timedelta

from fastapi import Response

from .models import AuthSession
from .settings import AuthSettings


def hash_session_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_session(user_id: str, settings: AuthSettings) -> tuple[str, AuthSession]:
    token = secrets.token_urlsafe(32)
    now = datetime.now(UTC)
    return token, AuthSession(
        user_id=user_id,
        token_hash=hash_session_token(token),
        created_at=now,
        expires_at=now + timedelta(days=settings.session_ttl_days),
    )


def set_session_cookie(response: Response, token: str, settings: AuthSettings) -> None:
    response.set_cookie(
        settings.cookie_name,
        token,
        httponly=True,
        secure=settings.secure_cookies,
        samesite="lax",
        path="/",
        max_age=settings.session_ttl_seconds,
    )


def clear_session_cookie(response: Response, settings: AuthSettings) -> None:
    response.delete_cookie(
        settings.cookie_name,
        httponly=True,
        secure=settings.secure_cookies,
        samesite="lax",
        path="/",
    )
