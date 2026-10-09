"""Assessment adapter: handle_request(request_id, architecture) -> ProcurementDecision.

Always returns a valid ProcurementDecision with human_review_required=True:
LLM failures fall back to the rules-only path, and any other failure produces a
conservative manual-review decision.
"""
from __future__ import annotations

import time
import os
import uuid
from collections import Counter
from datetime import datetime, timezone

from src import data_access as da
from src.config import get_reference_date, get_settings
from src.contracts import Architecture, EvidenceItem, ProcurementDecision, RunTelemetry
from src.fallback import run_rules_only
from src.schemas import DECISION_LABELS
from src.tools import RunContext
from src.tracing import traced
from src.trace_steps import build_steps
from src.agents import AgentFailed


def _run_agent(architecture: str, request_id: str, ctx: RunContext):
    """Returns (Assembly, proposals: dict). Raises LLMUnavailable / AgentFailed."""
    try:
        if architecture == "staged":
            from src.agents.staged_agent import run_staged as runner
        elif architecture == "workflow":  # C6: evaluation configuration; not part of the contract's Architecture
            from src.agents.workflow_llm import run_workflow as runner
        else:
            from src.agents.single_agent import run_single as runner
    except ImportError as exc:
        raise AgentFailed(f"agent architecture '{architecture}' not available: {exc}") from exc
    return runner(request_id, ctx)


def _error_decision(request_id: str, exc: Exception, ctx: RunContext) -> tuple[ProcurementDecision, str]:
    """Last-resort decision when even the deterministic path fails."""
    not_found = isinstance(exc, KeyError) or "Unknown request_id" in str(exc)
    if not_found:
        decision_type = "request_clarification"
        rec = "Request record could not be found; nothing can be assessed."
        nxt = "Procurement should confirm the request ID with the requester before any review."
        missing = ["request record (not found)"]
    else:
        decision_type = "route_for_specialist_review"
        rec = "Automated analysis failed; a procurement reviewer must assess this request manually."
        nxt = "Procurement reviews the request manually; no automated evidence is available."
        missing = [f"automated analysis (internal error: {type(exc).__name__})"]
    return ProcurementDecision(
        request_id=str(request_id),
        recommendation=f"{DECISION_LABELS[decision_type]}: {rec}",
        evidence=[EvidenceItem(source="copilot_analysis", finding=f"Analysis error: {type(exc).__name__}",
                               reference=None)],
        required_approvals=["Procurement"],
        missing_information=missing,
        risk_flags=["missing_information"],
        next_step=nxt,
        human_review_required=True,
        telemetry=RunTelemetry(llm_calls=ctx.counters["llm_calls"], tool_calls=ctx.counters["tool_calls"],
                               tool_names=list(ctx.tool_names)),
    ), decision_type


def _trace_meta(request_id, architecture="single", mode="llm") -> dict:
    s = get_settings()
    return {"request_id": request_id, "architecture": architecture, "mode": mode, "provider": s.llm_provider,
            "model": s.model_name, "case_id": os.getenv("EVAL_CASE_ID")}


@traced("handle_request", metadata=_trace_meta)
def handle_request_with_trace(request_id: str, architecture: Architecture = "single",
                              mode: str = "llm") -> tuple[ProcurementDecision, dict]:
    started = time.perf_counter()
    settings = get_settings()
    ctx = RunContext(str(request_id).strip())
    proposals: dict = {}
    error: str | None = None
    path = mode
    assembly = None
    decision: ProcurementDecision
    decision_type: str

    try:
        da.get_request(ctx.request_id)  # unknown request: answer without spending any LLM call (KeyError below)
        if mode == "rules_only":
            assembly = run_rules_only(ctx, ctx.request_id, llm_failed=False)
        elif settings.llm_simulate_outage or not settings.llm_configured:
            error = "LLM unavailable: " + ("simulated outage" if settings.llm_simulate_outage else "no API key configured")
            path = "fallback"
            assembly = run_rules_only(ctx, ctx.request_id, llm_failed=True)
        else:
            try:
                assembly, proposals = _run_agent(architecture, ctx.request_id, ctx)
            except Exception as exc:  # LLMUnavailable, AgentFailed or any agent bug -> rules-only fallback
                error = f"{type(exc).__name__}: {exc}"
                path = "fallback"
                ctx.event("fallback_used", reason=error)
                salvage = getattr(exc, "salvage", None)
                proposals = getattr(exc, "proposals", None) or proposals
                assembly = run_rules_only(ctx, ctx.request_id, llm_failed=True, keep_evidence=salvage)
        decision, decision_type = assembly.decision, assembly.decision_type
    except Exception as exc:  # never let the adapter raise
        error = f"{type(exc).__name__}: {exc}"
        path = "error"
        decision, decision_type = _error_decision(ctx.request_id, exc, ctx)

    total_ms = (time.perf_counter() - started) * 1000
    wait_ms = ctx.counters["llm_wait_ms"]
    counts = Counter(e["event"] for e in ctx.events)
    counts["gate_filled"] = sum(v for k, v in counts.items() if k.startswith("gate_filled:"))
    trace = {
        "run_id": f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}",
        "request_id": ctx.request_id,
        "architecture": architecture,
        "mode": mode,
        "path": path,
        "provider": settings.llm_provider if path not in ("rules_only",) else None,
        "model": settings.model_name if path not in ("rules_only",) else None,
        "reference_date": get_reference_date().isoformat(),
        "decision_type": decision_type,
        "latency_total_ms": round(total_ms, 1),
        "llm_wait_ms": round(wait_ms, 1),
        "latency_active_ms": round(total_ms - wait_ms, 1),
        "llm_ms": round(ctx.counters["llm_ms"], 1),
        "tool_ms": round(ctx.counters["tool_ms"], 1),
        "llm_calls": ctx.counters["llm_calls"],
        "tool_calls": ctx.counters["tool_calls"],
        "tokens": {k: ctx.counters[k] for k in ("prompt_tokens", "completion_tokens", "total_tokens")},
        "tool_log": ctx.tool_log,
        "llm_log": ctx.llm_log,
        "evidence_corpus": ctx.evidence_corpus,
        "proposals": proposals,
        "policy": ctx.policy.model_dump() if ctx.policy is not None else None,
        "events": ctx.events,
        "event_counts": dict(counts),
        "approvals": [a.model_dump() for a in assembly.approvals] if assembly else
                     [{"role": r, "reasons": [], "name_hint": None} for r in decision.required_approvals],
        "flag_reasons": ({k: [r.model_dump() for r in v] for k, v in assembly.flag_reasons.items()}
                         if assembly else {}),
        "evidence_kinds": [e.kind for e in assembly.evidence] if assembly else ["rule"],
        "questions_for_reviewer": assembly.questions_for_reviewer if assembly else [],
        "used_templates": assembly.used_templates if assembly else {},
        "error": error,
    }
    trace["steps"] = build_steps(trace)
    return decision, trace


def handle_request(request_id: str, architecture: Architecture = "single") -> ProcurementDecision:
    """Assessment adapter (signature fixed by the evaluation harness)."""
    decision, _ = handle_request_with_trace(request_id, architecture)
    return decision
