"""Guardrails and assembler (T11): policy result + (optional) agent proposal -> ProcurementDecision.

Code is authoritative. The agent may add Security/Privacy/Legal reviews (with a reason),
the overlap and injection flags, and wording; it can never remove anything code computed.
Every correction is recorded as an event on the RunContext.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from src import data_access as da
from src import policy_engine as pe
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
_NUMBER = re.compile(r"\d+(?:\.\d+)?")
_CORPUS_NUMBER = re.compile(r"(?<![\w.])\d[\d,]*(?:\.\d+)?")
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


# C3: the LLM reports implied data classes with a verbatim quote; code checks the quote and applies the policy.
_QUOTE_STRIP = "\"'“”‘’`.,;: "
MIN_QUOTE_CHARS = 4
_ELLIPSIS = re.compile(r"\.{3,}|…")


def _norm_text(text: object) -> str:
    return " ".join(str(text or "").split()).casefold()


def request_own_text(request_id: str) -> str:
    """The request's own words: product name, justification and integrations (never vendor or catalog text)."""
    try:
        raw = da.get_request(request_id)
    except KeyError:
        return ""
    integ = raw.get("requested_integrations")
    integ = " | ".join(map(str, integ)) if isinstance(integ, (list, tuple)) else str(integ or "")
    return _norm_text(" | ".join([str(raw.get("product_name") or ""), str(raw.get("business_justification") or ""), integ]))


def ground_implied_classes(proposal, request_id: str, ctx) -> dict[str, list[str]]:
    """Accept an implied class only if its quote (or each segment of an elided quote) is a substring of the
    request's own text."""
    accepted: dict[str, list[str]] = {}
    items = list(getattr(proposal, "implied_data_classes", None) or []) if proposal is not None else []
    if not items:
        return accepted
    own = request_own_text(request_id)
    for item in items:
        quote = _norm_text(item.quote).strip(_QUOTE_STRIP)
        # An elided quote ("A ... B") is accepted only if every segment is itself a verbatim substring.
        segments = [seg.strip(_QUOTE_STRIP) for seg in _ELLIPSIS.split(quote)]
        if (all(len(seg) >= MIN_QUOTE_CHARS and seg in own for seg in segments)
                and not matches_injection(quote)):
            short = quote if len(quote) <= 80 else quote[:79] + "…"
            accepted.setdefault(item.data_class, []).append(f"AI: implied {item.data_class} (quote: '{short}')")
            ctx.event("implied_class_accepted", data_class=item.data_class, quote=item.quote)
        else:
            ctx.event("implied_class_ungrounded", data_class=item.data_class, quote=item.quote,
                      why="quote not found verbatim in the request's own text")
    return accepted


# C2: AI evidence must not contradict the code-computed budget status (number-level grounding cannot see this).
_BUDGET_WORD = re.compile(r"\bbudget", re.IGNORECASE)
_SHORTFALL = re.compile(r"\b(insufficient|not\s+(enough|sufficient|within)|exceed(s|ed|ing)?|over[\s-]?budget|"
                        r"shortfall|short\s+by|cannot\s+(cover|afford))\b", re.IGNORECASE)
_AFFORDABLE = re.compile(r"\b(within(\s+the)?(\s+\w+)?\s+budget|sufficient|enough\s+budget|can\s+cover|"
                         r"covers?\s+the)\b", re.IGNORECASE)


def contradicts_policy(finding: str, policy: PolicyResult) -> str | None:
    """Reason if an AI evidence sentence contradicts the deterministic budget check, else None."""
    text = finding or ""
    if not _BUDGET_WORD.search(text):
        return None
    status = (policy.budget or {}).get("status")
    if status == "ok" and _SHORTFALL.search(text):
        return "claims a budget shortfall, but check_budget found the request within the available budget"
    if status == "insufficient" and _AFFORDABLE.search(text) and not _SHORTFALL.search(text):
        return "claims the budget is sufficient, but check_budget found a shortfall"
    return None


def grounded(finding: str, source: str, ctx) -> tuple[bool, str]:
    called = {e["tool"] for e in ctx.tool_log if e["status"] == "ok"}
    if source != "copilot_analysis" and source not in called:
        return False, f"source '{source}' was not called in this run"
    corpus = ctx.evidence_corpus
    corpus_nocomma = corpus.replace(",", "")
    corpus_numbers = None
    for raw in GROUNDING_TOKEN.findall(finding or ""):
        token = raw.replace("$", "").replace(",", "")
        if not token or token in corpus or token in corpus_nocomma:
            continue
        if _NUMBER.fullmatch(token):  # "$800.00" vs JSON 800.0: compare by value
            if corpus_numbers is None:
                corpus_numbers = {float(n.replace(",", "")) for n in _CORPUS_NUMBER.findall(corpus)}
            if float(token) in corpus_numbers:
                continue
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
    # 0. C3: implied data classes grounded in the request's own words re-run the policy engine
    accepted = ground_implied_classes(proposal, policy.request_id, ctx)
    if accepted and any(c not in policy.data_classes for c in accepted):
        policy = pe.evaluate(policy.request_id, ctx, extra_classes=accepted)
        ctx.policy = policy

    # 1. Approvals: code-computed only. The LLM cannot add roles directly (C3); a proposed specialist
    #    review without a grounded data class becomes a question for the reviewer.
    reasons: dict[str, list[Reason]] = {a.role: list(a.reasons) for a in policy.approvals}
    hints = {a.role: a.name_hint for a in policy.approvals}
    role_questions: list[str] = []
    if proposal is not None:
        proposed = {}
        for ap in proposal.required_approvals:
            proposed.setdefault(ap.role, ap.reason)
        for role, reason in proposed.items():
            if role in reasons:
                continue
            if role in SPECIALIST_ROLES:
                ctx.event("llm_review_without_class", role=role, reason=reason)
                text = " ".join(str(reason or "").split())[:280]
                if text and not matches_injection(text):
                    role_questions.append(f"AI suggested a {role} review: {text}")
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
    questions: list[str] = list(role_questions)
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
    decision_type = decide(policy, final_roles, final_flags, proposal.overlap_assessment if proposal else None,
                           proposal.decision_type if proposal else None)
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
        if cand and not output_filter_hit(cand):
            rec_sentence, used["recommendation"] = cand, False
        elif cand:
            ctx.event("output_guardrail_triggered", field="recommendation", text=cand)
        cand = " ".join(proposal.next_step.split())
        if cand and not output_filter_hit(cand):
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
            clash = contradicts_policy(item.finding, policy)
            if clash:
                ctx.event("contradicting_evidence_removed", item=item.model_dump(), why=clash)
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
