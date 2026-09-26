from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from ..modules.database.audit import AuditEvent
from .logging import redact
from .request_context import (
    get_organization_id,
    get_request_id,
    get_trace_context,
    get_user_id,
)


async def record_audit(
    session: AsyncSession,
    action: str,
    *,
    outcome: str = "success",
    resource_type: str | None = None,
    resource_id: str | None = None,
    details: dict[str, Any] | None = None,
    error: str | None = None,
) -> AuditEvent:
    trace_id, _ = get_trace_context()
    event = AuditEvent(
        action=action,
        outcome=outcome,
        user_id=get_user_id(),
        organization_id=get_organization_id(),
        request_id=get_request_id(),
        trace_id=trace_id,
        resource_type=resource_type,
        resource_id=resource_id,
        details=redact(details) if details else None,
        error=error[:2000] if error else None,
    )
    add = getattr(session, "add", None)
    if add is not None:
        add(event)
    return event
