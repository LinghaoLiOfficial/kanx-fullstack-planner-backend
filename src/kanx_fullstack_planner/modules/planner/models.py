from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from sqlalchemy import JSON, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ..database.base import Base


def utcnow() -> datetime:
    return datetime.now(UTC)


class WorkflowRunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    NEEDS_CLARIFICATION = "needs_clarification"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class WorkflowStepStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"


class RequirementStatus(StrEnum):
    DRAFT = "draft"
    NEEDS_CLARIFICATION = "needs_clarification"
    READY = "ready"
    APPROVED = "approved"
    ARCHIVED = "archived"


class RawRequirementStatus(StrEnum):
    DRAFT = "draft"
    PROCESSING = "processing"
    PROCESSED = "processed"
    NEEDS_CLARIFICATION = "needs_clarification"
    FAILED = "failed"
    ARCHIVED = "archived"


class RawRequirement(Base):
    __tablename__ = "planner_raw_requirements"
    __table_args__ = (
        Index("ix_planner_raw_requirements_project_created", "project_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    project_id: Mapped[str] = mapped_column(String(128), index=True)
    status: Mapped[str] = mapped_column(String(32), default=RawRequirementStatus.DRAFT, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RawRequirementRevision(Base):
    __tablename__ = "planner_raw_requirement_revisions"
    __table_args__ = (UniqueConstraint("raw_requirement_id", "revision"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    raw_requirement_id: Mapped[str] = mapped_column(
        ForeignKey("planner_raw_requirements.id", ondelete="CASCADE"), index=True
    )
    revision: Mapped[int] = mapped_column(Integer)
    description: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64))
    project_context: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class WorkflowRun(Base):
    __tablename__ = "planner_workflow_runs"
    __table_args__ = (Index("ix_planner_runs_project_created", "project_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    project_id: Mapped[str] = mapped_column(String(128), index=True)
    job_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    raw_requirement_revision_id: Mapped[str | None] = mapped_column(
        ForeignKey("planner_raw_requirement_revisions.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    status: Mapped[str] = mapped_column(String(32), default=WorkflowRunStatus.QUEUED, index=True)
    raw_text: Mapped[str] = mapped_column(Text)
    raw_text_hash: Mapped[str] = mapped_column(String(64))
    base_snapshot_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    workflow_version: Mapped[str] = mapped_column(String(32), default="1.0.0")
    schema_version: Mapped[str] = mapped_column(String(32), default="1.0.0")
    project_context: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    checkpoint: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class WorkflowStep(Base):
    __tablename__ = "planner_workflow_steps"
    __table_args__ = (UniqueConstraint("run_id", "step_key"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    run_id: Mapped[str] = mapped_column(
        ForeignKey("planner_workflow_runs.id", ondelete="CASCADE"), index=True
    )
    step_key: Mapped[str] = mapped_column(String(96))
    module_type: Mapped[str] = mapped_column(String(16), default="system")
    status: Mapped[str] = mapped_column(String(32), default=WorkflowStepStatus.PENDING)
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    input_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    output_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    input_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    output_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class LLMInvocation(Base):
    __tablename__ = "planner_llm_invocations"
    __table_args__ = (UniqueConstraint("run_id", "invocation_id", "attempt"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    run_id: Mapped[str] = mapped_column(
        ForeignKey("planner_workflow_runs.id", ondelete="CASCADE"), index=True
    )
    step_id: Mapped[str | None] = mapped_column(
        ForeignKey("planner_workflow_steps.id", ondelete="SET NULL"), nullable=True
    )
    task_name: Mapped[str] = mapped_column(String(96))
    model: Mapped[str] = mapped_column(String(128))
    prompt_version: Mapped[str] = mapped_column(String(32), default="1.0.0")
    schema_version: Mapped[str] = mapped_column(String(32), default="1.0.0")
    input_hash: Mapped[str] = mapped_column(String(64))
    output_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="succeeded")
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    invocation_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    call_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    subject: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    input_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    control_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    system_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    user_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    call_type: Mapped[str | None] = mapped_column(String(48), nullable=True)
    max_attempts: Mapped[int | None] = mapped_column(Integer, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    raw_response: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ValidationFinding(Base):
    __tablename__ = "planner_validation_findings"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    run_id: Mapped[str] = mapped_column(
        ForeignKey("planner_workflow_runs.id", ondelete="CASCADE"), index=True
    )
    severity: Mapped[str] = mapped_column(String(16))
    code: Mapped[str] = mapped_column(String(96))
    message: Mapped[str] = mapped_column(Text)
    feature_key: Mapped[str | None] = mapped_column(String(160), nullable=True)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Requirement(Base):
    __tablename__ = "planner_requirements"
    __table_args__ = (UniqueConstraint("project_id", "requirement_key"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    project_id: Mapped[str] = mapped_column(String(128), index=True)
    requirement_key: Mapped[str] = mapped_column(String(160))
    status: Mapped[str] = mapped_column(String(32), default=RequirementStatus.DRAFT)
    current_revision: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RequirementRevision(Base):
    __tablename__ = "planner_requirement_revisions"
    __table_args__ = (UniqueConstraint("requirement_id", "revision"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    requirement_id: Mapped[str] = mapped_column(
        ForeignKey("planner_requirements.id", ondelete="CASCADE"), index=True
    )
    run_id: Mapped[str] = mapped_column(
        ForeignKey("planner_workflow_runs.id", ondelete="CASCADE"), index=True
    )
    raw_requirement_revision_id: Mapped[str | None] = mapped_column(
        ForeignKey("planner_raw_requirement_revisions.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    revision: Mapped[int] = mapped_column(Integer)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    content_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RequirementDependency(Base):
    __tablename__ = "planner_requirement_dependencies"
    __table_args__ = (
        UniqueConstraint("source_requirement_id", "target_requirement_id", "relation"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    source_requirement_id: Mapped[str] = mapped_column(
        ForeignKey("planner_requirements.id", ondelete="CASCADE"), index=True
    )
    target_requirement_id: Mapped[str] = mapped_column(
        ForeignKey("planner_requirements.id", ondelete="CASCADE"), index=True
    )
    relation: Mapped[str] = mapped_column(String(32), default="requires")
    reason: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
