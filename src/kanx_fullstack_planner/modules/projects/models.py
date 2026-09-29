from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import JSON, DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from ..database.base import Base


def utcnow() -> datetime:
    return datetime.now(UTC)


class Project(Base):
    __tablename__ = "projects"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    owner_user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(160))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="active")
    llm_prompt_language: Mapped[str] = mapped_column(String(16), default="zh-CN")
    target_users: Mapped[list[str]] = mapped_column(JSON, default=list)
    business_domain: Mapped[str | None] = mapped_column(String(160), nullable=True)
    frontend_tech_stack: Mapped[list[str]] = mapped_column(JSON, default=list)
    backend_tech_stack: Mapped[list[str]] = mapped_column(JSON, default=list)
    database_tech_stack: Mapped[list[str]] = mapped_column(JSON, default=list)
    global_constraints: Mapped[list[str]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
