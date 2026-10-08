"""Guardrails and assembler (T11): policy result + (optional) agent proposal -> ProcurementDecision.

Code is authoritative. The agent may add Security/Privacy/Legal reviews (with a reason),
the overlap and injection flags, and wording; it can never remove anything code computed.
Every correction is recorded as an event on the RunContext.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from src.contracts import EvidenceItem, ProcurementDecision, RunTelemetry
from src.injection import matches_injection
from src.policy_engine import (
    LBL_COST, LBL_DATA, LBL_DEPARTMENT, LBL_INTEGRATIONS, LBL_PRODUCT, LBL_PURPOSE, LBL_REQUESTER, LBL_USERS, decide,
)
from src.schemas import (
    DECISION_LABELS, SPECIALIST_FLAG, SPECIALIST_ROLES, AgentProposal, EvidenceRecord, PolicyResult, Reason,
    RoleApproval, order_flags, order_roles,
)

MAX_EVIDENCE = 12
MAX_ADDED_MISSING = 3
MAX_ITEM_CHARS = 120
REQUEST_FIELD_LABELS = [LBL_REQUESTER, LBL_DEPARTMENT, LBL_PRODUCT, LBL_COST, LBL_USERS, LBL_PURPOSE, LBL_DATA,
                        LBL_INTEGRATIONS]

OUTPUT_FILTER = [re.compile(p, re.IGNORECASE) for p in [
    r"\b(this|the)\s+(request|purchase)\s+(is|has\s+been|was)\s+(pre-?)?approved\b",
    r"\bpre-?approved\b|\bcfo[- ]approved\b|\bapproval\s+(is\s+)?granted\b",
    r"\b(approve|approving)\s+(it|this|the\s+request)\s+(now|immediately)\b",
    r"\b(purchase|order)\s+(has\s+been\s+|is\s+)?(placed|completed|made)\b",
]]

# Order matters: the date alternative must come first so "2026-09-30" is one token.
GROUNDING_TOKEN = re.compile(r"\d{4}-\d{2}-\d{2}|\$?\d[\d,]*(?:\.\d+)?|[A-Z]{1,4}-?\d{2,5}")

FLAG_SHORT = {
    "security_review_required": "Security review",
    "privacy_review_required": "Privacy review",
    "legal_review_required": "Legal review",
    "budget_insufficient": "budget exception",
    "budget_unverified": "budget not verified",
    "conflicting_vendor_evidence": "conflicting vendor evidence",
    "vendor_review_expired": "expired vendor security review",
    "vendor_risk_unavailable": "vendor risk could not be verified",
}
AGENT_MERGEABLE_FLAGS = {"existing_tool_overlap", "prompt_injection_detected"}


def output_filter_hit(text: str | None) -> bool:
    return bool(text) and any(p.search(text) for p in OUTPUT_FILTER)


def grounded(finding: str, source: str, ctx) -> tuple[bool, str]:
    called = {e["tool"] for e in ctx.tool_log if e["status"] == "ok"}
    if source != "copilot_analysis" and source not in called:
        return False, f"source '{source}' was not called in this run"
    corpus = ctx.evidence_corpus
    corpus_nocomma = corpus.replace(",", "")
    for raw in GROUNDING_TOKEN.findall(finding or ""):
        token = raw.replace("$", "").replace(",", "")
        if token and token not in corpus and token not in corpus_nocomma:
            return False, f"token '{raw}' not found in tool results"
    return True, ""


@dataclass
class Assembly:
    """Everything the UI and trace need beyond the contract."""
    decision: ProcurementDecision
    decision_type: str
    approvals: list[RoleApproval]
    flag_reasons: dict
    evidence: list[EvidenceRecord]
    questions_for_reviewer: list[str] = field(default_factory=list)
    used_templates: dict = field(default_factory=dict)


def _strip_label(text: str) -> str:
    for label in DECISION_LABELS.values():
        if text.lower().startswith(label.lower() + ":"):
            return text[len(label) + 1:].strip()
    return text.strip()


def _join(items: list[str]) -> str:
    items = [i for i in items if i]
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def _templates(decision_type: str, policy: PolicyResult, roles: list[str], flags: list[str],
               covering_products: list[str]) -> tuple[str, str]:
    requester = (policy.requester or {}).get("name") or "the requester"
    if decision_type == "request_clarification":
        fields = [m for m in policy.missing_information if m in REQUEST_FIELD_LABELS] or policy.missing_information
        return ("Request is not ready for review; required information is missing.",
                f"Ask {requester} to provide: {'; '.join(fields)}.")
    if decision_type == "use_existing_tool":
        product = _join(covering_products) or "the existing licensed tool"
        return ("An existing licensed tool may already meet this need.",
                f"Confirm with {requester} whether {product} covers the need before starting a new purchase.")
    if decision_type == "route_for_specialist_review":
        reasons = [FLAG_SHORT[f] for f in flags if f in FLAG_SHORT]
        specialists = [r for r in roles if r in SPECIALIST_ROLES]
        if {"budget_insufficient", "budget_unverified"} & set(flags) and "Finance" in roles:
            specialists = ["Finance"] + specialists
        return (f"Route for specialist review before any business approval: {_join(reasons)}.",
                f"Send the evidence pack to {_join(roles)}; {_join(specialists) or 'the specialists'} review first.")
    return ("Request is complete and within policy; route to the approvers for this cost tier.",
            f"Send the evidence pack to {_join(roles)}.")


def assemble(policy: PolicyResult, proposal: AgentProposal | None, ctx,
             extra_flags: list[str] | None = None, extra_evidence: list | None = None) -> Assembly:
    """extra_evidence: agent evidence items to keep without a full proposal (B stage-1 salvage)."""
    # 1. Approvals
    reasons: dict[str, list[Reason]] = {a.role: list(a.reasons) for a in policy.approvals}
    hints = {a.role: a.name_hint for a in policy.approvals}
    if proposal is not None:
        proposed = {}
        for ap in proposal.required_approvals:
            proposed.setdefault(ap.role, ap.reason)
        for role, reason in proposed.items():
            if role in reasons:
                continue
            if role in SPECIALIST_ROLES and reason and reason.strip():
                reasons[role] = [Reason(rule="AI", policy_ref={"Security": "§5", "Privacy": "§6", "Legal": "§7"}[role],
                                        detail=" ".join(reason.split())[:300])]
                ctx.event("llm_added_review", role=role, reason=reason)
            else:
                ctx.event("llm_role_dropped", role=role, reason=reason)
        for role in policy.roles:
            if role not in proposed:
                ctx.event("code_restored_approval", role=role)
    final_roles = order_roles(reasons)

    # 2. Flags
    flag_reasons = {k: list(v) for k, v in policy.flag_reasons.items()}
    for role in final_roles:  # specialist flags follow the accepted roles (R11)
        if role in SPECIALIST_FLAG and SPECIALIST_FLAG[role] not in flag_reasons:
            flag_reasons[SPECIALIST_FLAG[role]] = [Reason(rule="AI", policy_ref="", detail=reasons[role][0].detail)]
    if proposal is not None:
        proposed_flags = list(dict.fromkeys(proposal.risk_flags))
        if proposal.injection_observed and "prompt_injection_detected" not in proposed_flags:
            proposed_flags.append("prompt_injection_detected")
        accepted_specialist = {SPECIALIST_FLAG[r] for r in final_roles if r in SPECIALIST_FLAG}
        for f in proposed_flags:
            if f in flag_reasons:
                continue
            if f in AGENT_MERGEABLE_FLAGS or f in accepted_specialist:
                detail = "Agent assessment"
                if f == "prompt_injection_detected" and proposal.injection_excerpt:
                    detail = f"Agent observed embedded instructions: \"{' '.join(proposal.injection_excerpt.split())[:100]}\""
                flag_reasons[f] = [Reason(rule="AI", policy_ref="", detail=detail)]
                ctx.event("llm_added_flag", flag=f)
            else:
                ctx.event("llm_flag_dropped", flag=f)
        for f in policy.flags:
            if f not in proposed_flags:
                ctx.event("code_restored_flag", flag=f)
    for f in extra_flags or []:
        flag_reasons.setdefault(f, [Reason(rule="SYS", policy_ref="§10", detail="AI analysis unavailable: rule-based result")])
    final_flags = order_flags(flag_reasons)

    # 3. Missing information
    missing = list(policy.missing_information)
    questions: list[str] = []
    if proposal is not None:
        existing = {m.casefold() for m in missing}
        added = 0
        for item in proposal.missing_information:
            text = " ".join(str(item).split())
            if not text or text.casefold() in existing:
                continue
            ok = (policy.request_fields_missing and added < MAX_ADDED_MISSING and len(text) <= MAX_ITEM_CHARS
                  and not matches_injection(text) and not output_filter_hit(text))
            if ok:
                missing.append(text)
                existing.add(text.casefold())
                added += 1
                ctx.event("llm_added_missing_item", item=text)
            elif not matches_injection(text) and len(text) <= 300:
                questions.append(text)

    # 4. Decision type (R12)
    decision_type = decide(policy, final_roles, final_flags, proposal.overlap_assessment if proposal else None)
    overridden = proposal is not None and proposal.decision_type != decision_type
    if overridden:
        ctx.event("llm_decision_overridden", proposed=proposal.decision_type, final=decision_type)

    covering = []
    if proposal is not None and decision_type == "use_existing_tool":
        cands = {c["software_id"]: c for c in ctx.catalog_candidates()}
        for a in proposal.overlap_assessment:
            if a.covers_stated_need and a.software_id in policy.overlap_flag_candidates:
                name = cands.get(a.software_id, {}).get("product_name")
                covering.append(f"{name} ({a.software_id})" if name else a.software_id)

    # 5./6. Recommendation and next step
    tmpl_rec, tmpl_next = _templates(decision_type, policy, final_roles, final_flags, covering)
    used = {"recommendation": True, "next_step": True}
    rec_sentence, next_step = tmpl_rec, tmpl_next
    if proposal is not None and not overridden:
        cand = _strip_label(" ".join(proposal.recommendation.split()))
        if cand and not output_filter_hit(cand) and not matches_injection(cand):
            rec_sentence, used["recommendation"] = cand, False
        elif cand:
            ctx.event("output_guardrail_triggered", field="recommendation", text=cand)
        cand = " ".join(proposal.next_step.split())
        if cand and not output_filter_hit(cand) and not matches_injection(cand):
            next_step, used["next_step"] = cand, False
        elif cand:
            ctx.event("output_guardrail_triggered", field="next_step", text=cand)
    recommendation = f"{DECISION_LABELS[decision_type]}: {rec_sentence}"

    # 7. Evidence: deterministic first, then grounded agent items; cap 12
    evidence = list(policy.evidence)[:MAX_EVIDENCE]
    agent_items = (list(proposal.evidence) if proposal is not None else []) + list(extra_evidence or [])
    if agent_items:
        seen = {e.finding.casefold() for e in evidence}
        for item in agent_items:
            ok, why = grounded(item.finding, item.source, ctx)
            if not ok:
                ctx.event("ungrounded_evidence_removed", item=item.model_dump(), why=why)
                continue
            if item.finding.casefold() in seen or len(evidence) >= MAX_EVIDENCE:
                continue
            evidence.append(EvidenceRecord(source=item.source, finding=" ".join(item.finding.split())[:400],
                                           reference=item.reference, kind="ai"))
            seen.add(item.finding.casefold())

    approvals = [RoleApproval(role=r, reasons=reasons[r], name_hint=hints.get(r)) for r in final_roles]
    decision = ProcurementDecision(
        request_id=policy.request_id,
        recommendation=recommendation,
        evidence=[EvidenceItem(source=e.source, finding=e.finding, reference=e.reference) for e in evidence],
        required_approvals=final_roles,
        missing_information=missing,
        risk_flags=final_flags,
        next_step=next_step,
        human_review_required=True,
        telemetry=RunTelemetry(llm_calls=ctx.counters["llm_calls"], tool_calls=ctx.counters["tool_calls"],
                               tool_names=list(ctx.tool_names)),
    )
    return Assembly(decision=decision, decision_type=decision_type, approvals=approvals, flag_reasons=flag_reasons,
                    evidence=evidence, questions_for_reviewer=questions, used_templates=used)
