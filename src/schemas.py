"""Internal schemas: policy-engine result (deterministic) and agent outputs (T8)."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

DecisionType = Literal[
    "route_for_approval", "route_for_specialist_review", "request_clarification", "use_existing_tool"
]
Role = Literal["Manager", "Department Head", "Finance", "CFO", "Procurement", "Security", "Privacy", "Legal"]

ROLE_ORDER: list[str] = ["Manager", "Department Head", "Finance", "CFO", "Procurement", "Security", "Privacy", "Legal"]
SPECIALIST_ROLES = {"Security", "Privacy", "Legal"}
SPECIALIST_FLAG = {
    "Security": "security_review_required",
    "Privacy": "privacy_review_required",
    "Legal": "legal_review_required",
}

STARTER_FLAGS = [
    "existing_tool_overlap", "budget_insufficient", "security_review_required", "privacy_review_required",
    "legal_review_required", "vendor_review_expired", "conflicting_vendor_evidence", "vendor_risk_unavailable",
    "prompt_injection_detected", "missing_information",
]
ADDED_FLAGS = ["vendor_not_registered", "budget_unverified", "unrecognized_data_access_level", "llm_unavailable"]
FLAG_ORDER: list[str] = STARTER_FLAGS + ADDED_FLAGS
# Flags an agent proposal may contain (llm_unavailable is set by code only).
AGENT_ALLOWED_FLAGS = [f for f in FLAG_ORDER if f != "llm_unavailable"]

DECISION_LABELS = {
    "route_for_approval": "Route for approval",
    "route_for_specialist_review": "Route for specialist review",
    "request_clarification": "Request clarification",
    "use_existing_tool": "Use existing tool",
}


def order_roles(roles) -> list[str]:
    roles = set(roles)
    return [r for r in ROLE_ORDER if r in roles]


def order_flags(flags) -> list[str]:
    flags = list(dict.fromkeys(flags))
    known = [f for f in FLAG_ORDER if f in flags]
    return known + [f for f in flags if f not in FLAG_ORDER]


# ---------------------------------------------------------------- policy engine
class Reason(BaseModel):
    rule: str
    policy_ref: str
    detail: str


class RoleApproval(BaseModel):
    role: str
    reasons: list[Reason] = Field(default_factory=list)
    name_hint: str | None = None


class EvidenceRecord(BaseModel):
    source: str
    finding: str
    reference: str | None = None
    kind: Literal["rule", "tool", "ai"] = "rule"


class PolicyResult(BaseModel):
    request_id: str
    reference_date: str
    policy_version: str | None = None
    approvals: list[RoleApproval] = Field(default_factory=list)
    flags: list[str] = Field(default_factory=list)
    flag_reasons: dict[str, list[Reason]] = Field(default_factory=dict)
    missing_information: list[str] = Field(default_factory=list)
    request_fields_missing: bool = False
    tier: str = ""
    data_classes: list[str] = Field(default_factory=list)
    data_class_sources: dict[str, list[str]] = Field(default_factory=dict)
    overlap_flag_candidates: list[str] = Field(default_factory=list)
    requester: dict | None = None
    budget: dict = Field(default_factory=dict)
    vendor: dict = Field(default_factory=dict)
    injection: dict = Field(default_factory=dict)
    evidence: list[EvidenceRecord] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    @property
    def roles(self) -> list[str]:
        return [a.role for a in self.approvals]

    def approval(self, role: str) -> RoleApproval | None:
        return next((a for a in self.approvals if a.role == role), None)


# ---------------------------------------------------------------- agent outputs (T8)
class ApprovalProposal(BaseModel):
    role: Role
    reason: str


class OverlapAssessment(BaseModel):
    software_id: str
    relationship: Literal["same_product_expansion", "substitute_could_meet_need", "related_not_substitute"]
    covers_stated_need: bool
    reason: str


DataClass = Literal["customer_pii", "employee_pii", "source_code", "production_access", "confidential_documents",
                    "credentials"]


class ImpliedDataClass(BaseModel):
    """A data class the request's own words imply (C3). `quote` must be verbatim from the request."""
    data_class: DataClass
    quote: str


class EvidenceProposal(BaseModel):
    source: str
    finding: str
    reference: str | None = None


class AgentProposal(BaseModel):
    """submit_recommendation arguments (Architecture A, and B stage 2)."""
    decision_type: DecisionType
    recommendation: str
    next_step: str
    required_approvals: list[ApprovalProposal]
    risk_flags: list[str]
    missing_information: list[str]
    overlap_assessment: list[OverlapAssessment]
    evidence: list[EvidenceProposal]
    injection_observed: bool
    injection_excerpt: str | None = None
    implied_data_classes: list[ImpliedDataClass] = []


class EvidencePack(BaseModel):
    """submit_evidence_pack arguments (Architecture B stage 1)."""
    need_summary: str
    implied_data_classes: list[ImpliedDataClass] = []
    overlap_assessment: list[OverlapAssessment]
    vendor_observations: list[str]
    uncertainties: list[str]
    injection_observed: bool
    injection_excerpt: str | None = None
    evidence: list[EvidenceProposal]
