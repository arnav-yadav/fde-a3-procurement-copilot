"""C9: "How this was decided" — who acted, why, with which tools, at what cost.

`build_steps(trace)` is a pure function over a stored trace (tool_log + events, plus llm_log when present).
It is applied on read, so traces written before it existed (e.g. the frozen run-3 files) render too.
Per-stage tokens and LLM time need `llm_log` (one entry per LLM call); without it they are known only when
the run had a single LLM stage, and are None otherwise.
"""
from __future__ import annotations

from collections import Counter

POLICY_TOOL = "evaluate_policy_rules"
LLM_STAGE = {
    "single": ("Single agent (LLM)",
               "Decides which evidence tools to call, then submits a structured recommendation."),
    "analyst": ("Analyst agent (LLM)",
                "Gathers budget, catalog and vendor evidence and hands over a structured evidence pack."),
    "reviewer": ("Policy and risk reviewer (LLM)",
                 "Reads the raw tool results, the policy-engine result and the analyst's pack, then decides. No tools."),
    "workflow": ("Workflow LLM call",
                 "Reads the evidence gathered by code and decides in one forced call. No tools."),
}
NOT_CORRECTIONS = {"llm_turn", "submission_invalid", "fallback_used", "forced_tool_choice_fallback",
                   "implied_class_accepted"}
CORRECTION_LABEL = {
    "code_restored_approval": "Approval restored by code",
    "code_restored_flag": "Flag restored by code",
    "llm_role_dropped": "AI-proposed approval dropped",
    "llm_review_without_class": "AI-proposed review turned into a reviewer question",
    "llm_flag_dropped": "AI flag dropped",
    "llm_added_flag": "AI flag kept",
    "llm_added_missing_item": "AI missing-information item kept",
    "llm_decision_overridden": "AI decision overridden",
    "implied_class_ungrounded": "AI data class rejected (quote not in the request)",
    "ungrounded_evidence_removed": "AI evidence removed (not grounded in tool results)",
    "contradicting_evidence_removed": "AI evidence removed (contradicts the budget check)",
    "output_guardrail_triggered": "AI wording replaced by a template",
}
RAW_KEY = {"staged": "reviewer", "workflow": "workflow"}


def _names(entries: list[dict]) -> list[str]:
    return [e.get("tool", "?") for e in entries]


def _ms(entries: list[dict]) -> float:
    return round(sum(float(e.get("duration_ms") or 0) for e in entries), 1)


def _step(actor: str, kind: str, why: str, tools: list[str] | None = None, llm_calls: int = 0,
          tokens: int | None = None, ms: float | None = None, cached: int = 0, note: str | None = None) -> dict:
    return {"actor": actor, "kind": kind, "why": why, "tools": tools or [], "cached_lookups": cached,
            "llm_calls": llm_calls, "tokens": tokens, "ms": ms, "note": note}


def _llm_cost(trace: dict, turns: list[dict], stage: str, n_stages: int) -> tuple[int | None, float | None]:
    log = trace.get("llm_log")
    if isinstance(log, list) and len(log) == len(turns):
        calls = [c for c, t in zip(log, turns) if t.get("stage") == stage]
        return (sum(int(c.get("total_tokens") or 0) for c in calls),
                round(sum(float(c.get("ms") or 0) for c in calls), 1))
    if n_stages == 1:  # the run's totals belong to its only LLM stage
        return (trace.get("tokens") or {}).get("total_tokens"), trace.get("llm_ms")
    return None, None


def corrections(trace: dict) -> list[dict]:
    """Guardrail events that changed or checked the AI proposal, in order."""
    out = []
    for e in trace.get("events") or []:
        kind = e.get("event", "")
        if kind in NOT_CORRECTIONS or kind.startswith("gate_filled"):
            continue
        detail = "; ".join(f"{k}: {v}" for k, v in e.items() if k != "event" and v not in (None, "", [], {}))
        out.append({"event": kind, "label": CORRECTION_LABEL.get(kind, kind.replace("_", " ")),
                    "detail": detail[:300]})
    return out


def raw_vs_final(trace: dict, final_approvals: list[str] | None = None) -> dict | None:
    """The AI's own proposal next to what code finally returned (None for rules-only runs)."""
    props = trace.get("proposals") or {}
    raw = props.get(RAW_KEY.get(trace.get("architecture"), "single"))
    if not isinstance(raw, dict) or "decision_type" not in raw:
        return None
    roles = [a.get("role") for a in raw.get("required_approvals") or [] if isinstance(a, dict)]
    final = final_approvals if final_approvals is not None else [a.get("role") for a in trace.get("approvals") or []]
    return {"ai_decision": raw.get("decision_type"), "final_decision": trace.get("decision_type"),
            "ai_approvals": roles, "final_approvals": final,
            "ai_flags": list(raw.get("risk_flags") or []),
            "ai_implied_data_classes": [c.get("data_class") for c in raw.get("implied_data_classes") or []
                                        if isinstance(c, dict)]}


def build_steps(trace: dict) -> list[dict]:
    path, arch = trace.get("path"), trace.get("architecture")
    if path == "error":
        return [_step("Code", "code", "The request could not be processed; a clarification result was returned.",
                      note=trace.get("error"))]
    log = trace.get("tool_log") or []
    events = trace.get("events") or []
    turns = [e for e in events if e.get("event") == "llm_turn"]
    stages = list(dict.fromkeys(e.get("stage") for e in turns))
    invalid = Counter(e.get("stage") for e in events if e.get("event") == "submission_invalid")

    agent = [e for e in log if e.get("initiator") == "agent"]
    code = [e for e in log if e.get("initiator") != "agent"]
    fresh = [e for e in code if not e.get("cache_hit")]
    cached = sum(1 for e in code if e.get("cache_hit"))
    single_llm = arch == "single" and path == "llm"
    gate = [e for e in fresh if e.get("initiator") == "gate"] if single_llm else []
    policy_runs = [e for e in fresh if e.get("tool") == POLICY_TOOL]
    gather = [e for e in fresh if e.get("tool") != POLICY_TOOL and e not in gate]

    def llm_step(stage: str) -> dict:
        actor, why = LLM_STAGE.get(stage, (f"{stage} (LLM)", "LLM stage."))
        tokens, ms = _llm_cost(trace, turns, stage, len(stages))
        uses_tools = stage in ("single", "analyst")
        note = f"{invalid[stage]} invalid submission(s) repaired or rejected" if invalid[stage] else None
        return _step(actor, "llm", why, _names(agent) if uses_tools else [],
                     llm_calls=sum(1 for t in turns if t.get("stage") == stage), tokens=tokens, ms=ms, note=note)

    policy_step = _step(
        "Policy engine (code)", "code",
        "Deterministic rules compute approvals, the budget check, review triggers and flags. "
        "The AI cannot remove anything it computes.",
        _names(policy_runs) + (_names(gather) if single_llm else []),
        ms=_ms(policy_runs), cached=cached)
    steps: list[dict] = []

    if path == "llm" and arch == "single":
        steps.append(llm_step("single"))
        if gate:
            steps.append(_step("Completeness gate (code)", "code",
                               "Runs any evidence tool the agent skipped for this request.", _names(gate), ms=_ms(gate)))
        steps.append(policy_step)
    elif path == "llm" and arch == "staged":
        if gather:  # older traces: the analyst fetched the request itself
            steps.append(_step("Orchestrator (code)", "code", "Fetches the request and hands it to the analyst.",
                               _names(gather), ms=_ms(gather)))
        steps.append(llm_step("analyst"))
        steps.append(policy_step)
        steps.append(llm_step("reviewer"))
    elif path == "llm" and arch == "workflow":
        steps.append(_step("Orchestrator (code)", "code",
                           "Runs every evidence tool in a fixed order: request, budget, catalog, vendor.",
                           _names(gather), ms=_ms(gather)))
        steps.append(policy_step)
        steps.append(llm_step("workflow"))
    else:  # rules_only or fallback
        if path == "fallback":
            steps.extend(llm_step(s) for s in stages)
            steps.append(_step("Rules-only fallback (code)", "code",
                               "The AI path failed, so the deterministic rules alone produced the result.",
                               note=trace.get("error")))
        steps.append(_step("Evidence tools (code)", "code", "Code runs every evidence tool for this request.",
                           _names(gather), ms=_ms(gather)))
        steps.append(policy_step)

    if path == "llm":
        fixes = corrections(trace)
        steps.append(_step("Guardrails (code)", "code",
                           "Checks the AI proposal against the policy engine: restores required approvals and flags, "
                           "grounds data-class quotes and evidence, filters wording.",
                           note=f"{len(fixes)} correction(s)" if fixes else "no corrections needed"))
    return steps
