from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class PlannerSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProjectContext(PlannerSchema):
    project_name: str | None = None
    project_description: str | None = None
    target_users: list[str] = Field(default_factory=list)
    business_domain: str | None = None
    language: str = "zh-CN"


class RawRequirementInput(PlannerSchema):
    raw_text: str = Field(min_length=3, max_length=50_000)
    base_snapshot_id: str | None = None
    project_context: ProjectContext = Field(default_factory=ProjectContext)

    @field_validator("raw_text")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("raw_text must not be empty")
        return value


class BusinessIntent(PlannerSchema):
    intent_key: str
    name: str
    goal: str
    primary_actor: str
    trigger: str
    desired_outcome: str
    scope_in: list[str] = Field(default_factory=list)
    scope_out: list[str] = Field(default_factory=list)
    related_objects: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)


class NormalizedRequirement(PlannerSchema):
    normalized_summary: str
    user_roles: list[str] = Field(default_factory=list)
    business_objects: list[str] = Field(default_factory=list)
    explicit_goals: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    mentioned_behaviors: list[str] = Field(default_factory=list)
    unresolved_terms: list[str] = Field(default_factory=list)


class Scope(PlannerSchema):
    in_scope: list[str] = Field(default_factory=list)
    out_of_scope: list[str] = Field(default_factory=list)


class AcceptanceCriterion(PlannerSchema):
    id: str
    given: str
    when: str
    then: str


class Assumption(PlannerSchema):
    id: str
    content: str
    impact: Literal["low", "medium", "high"] = "medium"
    requires_confirmation: bool = False


class Ambiguity(PlannerSchema):
    id: str
    question: str
    related_requirement_keys: list[str] = Field(default_factory=list)
    severity: Literal["low", "medium", "high"] = "medium"


class AgileRequirement(PlannerSchema):
    draft_key: str
    name: str
    user_story: str
    business_goal: str
    scope: Scope
    acceptance_criteria: list[AcceptanceCriterion] = Field(min_length=1)
    priority: Literal["critical", "high", "normal", "low"] = "normal"
    priority_confidence: float = Field(default=0.5, ge=0, le=1)
    priority_reason: str = ""
    dependencies: list[str] = Field(default_factory=list)
    affected_domains: list[str] = Field(default_factory=list)
    assumptions: list[Assumption] = Field(default_factory=list)
    source_evidence: list[str] = Field(default_factory=list)


class CandidateRequirements(PlannerSchema):
    requirements: list[AgileRequirement] = Field(default_factory=list)


class MergeCandidate(PlannerSchema):
    source_keys: list[str] = Field(default_factory=list)
    recommended_key: str
    reason: str


class DependencyRelation(PlannerSchema):
    source_key: str
    target_key: str
    relation: Literal["requires", "related", "conflicts"] = "requires"
    reason: str


class DependencyAnalysis(PlannerSchema):
    merge_candidates: list[MergeCandidate] = Field(default_factory=list)
    dependencies: list[DependencyRelation] = Field(default_factory=list)
    ordering: list[str] = Field(default_factory=list)


class ValidationFindingData(PlannerSchema):
    severity: Literal["warning", "error"]
    code: str
    message: str
    feature_key: str | None = None
    details: dict[str, object] = Field(default_factory=dict)


class WorkflowResult(PlannerSchema):
    run_id: str
    status: str
    requirements: list[AgileRequirement] = Field(default_factory=list)
    assumptions: list[Assumption] = Field(default_factory=list)
    ambiguities: list[Ambiguity] = Field(default_factory=list)
    warnings: list[ValidationFindingData] = Field(default_factory=list)
    validation_summary: dict[str, int | float] = Field(default_factory=dict)
    audit: dict[str, object] = Field(default_factory=dict)
