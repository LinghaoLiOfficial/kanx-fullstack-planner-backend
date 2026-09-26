from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import ipaddress
import json
import secrets
import socket
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlparse

import httpx
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from ...core.config import Settings
from .models import Job, JobEvent, JobWebhook, WebhookDelivery
from .settings import get_jobs_settings


def _cipher() -> AESGCM:
    configured = get_jobs_settings().webhook_encryption_key.get_secret_value()
    if not configured:
        raise ValueError("JOBS_WEBHOOK_ENCRYPTION_KEY is required")
    try:
        key = base64.urlsafe_b64decode(configured + "=" * (-len(configured) % 4))
    except ValueError as error:
        raise ValueError("Invalid webhook encryption key") from error
    if len(key) != 32:
        raise ValueError("Webhook encryption key must encode exactly 32 bytes")
    return AESGCM(key)


def encrypt_secret(secret: str) -> str:
    nonce = secrets.token_bytes(12)
    encrypted = _cipher().encrypt(nonce, secret.encode(), b"job-webhook-v1")
    return base64.urlsafe_b64encode(nonce + encrypted).decode()


def decrypt_secret(value: str) -> str:
    raw = base64.urlsafe_b64decode(value)
    return _cipher().decrypt(raw[:12], raw[12:], b"job-webhook-v1").decode()


async def validate_webhook_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Webhook URL must be an HTTPS URL without credentials")
    loop = asyncio.get_running_loop()
    addresses = await loop.run_in_executor(
        None,
        lambda: socket.getaddrinfo(parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM),
    )
    for item in addresses:
        address = ipaddress.ip_address(item[4][0])
        if not address.is_global:
            raise ValueError("Webhook URL resolves to a non-public address")


async def create_webhook(
    session: AsyncSession,
    *,
    organization_id: str,
    url: str,
    event_types: list[str],
) -> tuple[JobWebhook, str]:
    await validate_webhook_url(url)
    secret = secrets.token_urlsafe(32)
    webhook = JobWebhook(
        organization_id=organization_id,
        url=url,
        event_types=sorted(set(event_types)),
        encrypted_secret=encrypt_secret(secret),
    )
    session.add(webhook)
    await session.flush()
    return webhook, secret


async def emit_job_event(
    session: AsyncSession, job: Job, event_type: str, payload: dict[str, Any]
) -> JobEvent:
    event = JobEvent(
        job_id=job.id,
        organization_id=job.organization_id,
        event_type=event_type,
        payload=payload,
    )
    session.add(event)
    await session.flush()
    if job.organization_id:
        hooks = (
            await session.scalars(
                select(JobWebhook).where(
                    JobWebhook.organization_id == job.organization_id,
                    JobWebhook.enabled.is_(True),
                )
            )
        ).all()
        for hook in hooks:
            if event_type in hook.event_types or "*" in hook.event_types:
                session.add(WebhookDelivery(webhook_id=hook.id, event_id=event.id))
    return event


async def dispatch_webhooks_once(settings: Settings) -> int:
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    delivered = 0
    try:
        async with factory() as session:
            deliveries = list(
                (
                    await session.scalars(
                        select(WebhookDelivery)
                        .where(
                            WebhookDelivery.status.in_(("pending", "retrying")),
                            WebhookDelivery.next_attempt_at <= datetime.now(UTC),
                        )
                        .order_by(WebhookDelivery.created_at)
                        .limit(20)
                        .with_for_update(skip_locked=True)
                    )
                ).all()
            )
            async with httpx.AsyncClient(
                timeout=10,
                follow_redirects=False,
                limits=httpx.Limits(max_connections=10),
            ) as client:
                for delivery in deliveries:
                    hook = await session.get(JobWebhook, delivery.webhook_id)
                    event = await session.get(JobEvent, delivery.event_id)
                    if hook is None or event is None or not hook.enabled:
                        delivery.status = "abandoned"
                        continue
                    try:
                        await validate_webhook_url(hook.url)
                        timestamp = str(int(datetime.now(UTC).timestamp()))
                        body = json.dumps(
                            {
                                "id": event.id,
                                "type": event.event_type,
                                "job_id": event.job_id,
                                "created_at": event.created_at.isoformat(),
                                "data": event.payload,
                            },
                            sort_keys=True,
                            separators=(",", ":"),
                        ).encode()
                        signature = hmac.new(
                            decrypt_secret(hook.encrypted_secret).encode(),
                            timestamp.encode() + b"." + body,
                            hashlib.sha256,
                        ).hexdigest()
                        async with client.stream(
                            "POST",
                            hook.url,
                            content=body,
                            headers={
                                "Content-Type": "application/json",
                                "X-Job-Timestamp": timestamp,
                                "X-Job-Signature": f"v1={signature}",
                            },
                        ) as response:
                            size = 0
                            async for chunk in response.aiter_bytes():
                                size += len(chunk)
                                if size > 65_536:
                                    raise RuntimeError("Webhook response exceeds 64 KiB")
                            delivery.response_code = response.status_code
                            if 200 <= response.status_code < 300:
                                delivery.status = "delivered"
                                delivered += 1
                                continue
                            raise RuntimeError(f"Webhook returned HTTP {response.status_code}")
                    except (httpx.HTTPError, OSError, RuntimeError, ValueError) as error:
                        delivery.attempt += 1
                        delivery.error = str(error)[:2000]
                        if delivery.attempt >= 8:
                            delivery.status = "dead_lettered"
                        else:
                            delivery.status = "retrying"
                            delay = min(3600, 2**delivery.attempt) + secrets.randbelow(5)
                            delivery.next_attempt_at = datetime.now(UTC) + timedelta(seconds=delay)
            await session.commit()
    finally:
        await engine.dispose()
    return delivered
