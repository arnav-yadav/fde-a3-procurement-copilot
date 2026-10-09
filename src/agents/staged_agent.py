"""Architecture B: staged / 2-agent (T10).

Stage 1  Procurement Analyst (LLM + 5 tools) -> EvidencePack
Gate     completeness gate for the 4 evidence tools
Code     evaluate_policy_rules (initiator="orchestrator")
Stage 2  Policy & Risk Reviewer (LLM, no tools, forced submit_recommendation) -> AgentProposal
"""
from __future__ import annotations

import json
import re

from src import llm_client as llm
from src.agents import AgentFailed
from src.agents.common import SUBMIT_EVIDENCE_PACK, SUBMIT_RECOMMENDATION, parse_submission, tool_loop
from src.config import get_reference_date
from src.data_access import norm_key
from src.guardrails import Assembly, assemble
from src.prompts import NUDGE_ANALYST, SYSTEM_ANALYST, SYSTEM_REVIEWER, reviewer_message, user_analyst
from src.schemas import AgentProposal, EvidencePack
from src.tools import AGENT_TOOLS, EVIDENCE_TOOLS, RunContext, completeness_gate, llm_content, policy_sections

ANALYST_TOOLS = AGENT_TOOLS["analyst"]
ANALYST_MAX_TURNS = 5
REVIEWER_ATTEMPTS = 2  # first try + one repair


def _raw_tool_results(ctx: RunContext) -> dict:
    """The evidence-tool results for THIS request and its vendor (the analyst may have queried others)."""
    rid = ctx.request_id
    details = ctx.execute("get_request_details", {"request_id": rid}, initiator="orchestrator")
    vendor_key = norm_key((details.get("untrusted_text") or {}).get("vendor_name") or "")
    out: dict = {"get_request_details": details}
    for (name, key), result in ctx.cache.items():
        if name == "check_budget" and json.loads(key).get("request_id") == rid:
            out[name] = result
        elif name == "get_vendor_status" and key == vendor_key:
            out[name] = result
        elif name == "search_software_catalog" and json.loads(key)[0] == rid:
            if name in out:  # merge keyword searches
                known = {c["software_id"] for c in out[name]["candidates"]}
                merged = dict(out[name])
                merged["candidates"] = out[name]["candidates"] + [
                    c for c in result["candidates"] if c["software_id"] not in known]
                out[name] = merged
            else:
                out[name] = result
    return out


def _policy_payload(ctx: RunContext, request_id: str) -> dict:
    return ctx.execute("evaluate_policy_rules", {"request_id": request_id}, initiator="orchestrator")


def _relevant_policy_text(policy) -> str:
    refs = {int(m) for a in policy.approvals for r in a.reasons for m in re.findall(r"§(\d+)", r.policy_ref)}
    refs |= {int(m) for rs in policy.flag_reasons.values() for r in rs for m in re.findall(r"§(\d+)", r.policy_ref)}
    if policy.overlap_flag_candidates:
        refs.add(3)
    if policy.request_fields_missing:
        refs.add(1)
    refs |= {9, 11}
    sections = policy_sections()
    return "\n\n".join(f"## {n}. {sections[n]['title']}\n{sections[n]['text']}" for n in sorted(refs) if n in sections)


def run_staged(request_id: str, ctx: RunContext) -> tuple[Assembly, dict]:
    proposals: dict = {}

    # Stage 1: analyst. Code fetches the request first (C5), so the analyst never guesses the vendor name.
    details = ctx.execute("get_request_details", {"request_id": request_id}, initiator="orchestrator")
    if details.get("status") != "ok":
        raise AgentFailed(f"request details unavailable: {details.get('error')}", proposals=proposals)
    messages = [
        {"role": "system", "content": SYSTEM_ANALYST},
        {"role": "user", "content": user_analyst(request_id, get_reference_date().isoformat()) + "\n\n"
                                    + llm_content(details)},
    ]
    pack, raw_packs = tool_loop(ctx, messages, ANALYST_TOOLS, SUBMIT_EVIDENCE_PACK, EvidencePack,
                                ANALYST_MAX_TURNS, NUDGE_ANALYST, stage="analyst")
    proposals["analyst"] = pack.model_dump() if pack else (raw_packs[-1] if raw_packs else None)
    if pack is None:
        raise AgentFailed("analyst produced no valid evidence pack", proposals=proposals)

    # Gate + deterministic policy engine
    completeness_gate(ctx, request_id, include_policy=False)
    policy_payload = _policy_payload(ctx, request_id)
    if ctx.policy is None:
        raise AgentFailed("policy engine result missing", salvage=pack.evidence, proposals=proposals)

    # Stage 2: reviewer (no tools; forced submit_recommendation)
    review_messages = [
        {"role": "system", "content": SYSTEM_REVIEWER},
        {"role": "user", "content": reviewer_message(
            json.dumps(pack.model_dump(), indent=1),
            json.dumps(_raw_tool_results(ctx), default=str, separators=(",", ":")),
            json.dumps(policy_payload, default=str, separators=(",", ":")),
            _relevant_policy_text(ctx.policy),
        )},
    ]
    proposal: AgentProposal | None = None
    raw_reviews: list = []
    try:
        for attempt in range(1, REVIEWER_ATTEMPTS + 1):
            result = llm.chat_forced(review_messages, SUBMIT_RECOMMENDATION, ctx=ctx)
            msg = result.message
            calls = [c for c in (msg.tool_calls or []) if c.function.name == "submit_recommendation"]
            ctx.event("llm_turn", stage="reviewer", turn=attempt, forced=True,
                      tool_calls=[c.function.name for c in (msg.tool_calls or [])], text=(msg.content or "")[:300])
            if not calls:
                review_messages.append(llm.message_to_dict(msg))
                review_messages.append({"role": "user", "content": "Call submit_recommendation now."})
                continue
            proposal, err, raw = parse_submission(calls[0].function.arguments, AgentProposal)
            raw_reviews.append(raw)
            if proposal is not None:
                break
            ctx.event("submission_invalid", stage="reviewer", error=(err or "")[:500])
            review_messages.append(llm.message_to_dict(msg))
            for c in msg.tool_calls or []:
                review_messages.append({"role": "tool", "tool_call_id": c.id,
                                        "content": f"Submission invalid. Fix these errors and call "
                                                   f"submit_recommendation again:\n{err}"})
    except llm.LLMUnavailable as exc:
        proposals["reviewer"] = raw_reviews[-1] if raw_reviews else None
        raise AgentFailed(f"reviewer stage failed: {exc}", salvage=pack.evidence, proposals=proposals) from exc

    proposals["reviewer"] = proposal.model_dump() if proposal else (raw_reviews[-1] if raw_reviews else None)
    if proposal is None:
        raise AgentFailed("reviewer produced no valid recommendation", salvage=pack.evidence, proposals=proposals)
    return assemble(ctx.policy, proposal, ctx), proposals
