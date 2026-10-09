"""Configuration C6: workflow + one LLM call (Class 12's simplest rung above rules-only).

Code gathers all evidence in a fixed order (details -> budget -> catalog -> vendor -> policy engine);
the LLM gets one forced submit_recommendation call with no tools, then the same guardrails.
"""
from __future__ import annotations

import json

from src import llm_client as llm
from src.agents import AgentFailed
from src.agents.common import SUBMIT_RECOMMENDATION, forced_submission
from src.agents.staged_agent import _raw_tool_results, _relevant_policy_text
from src.guardrails import Assembly, assemble
from src.prompts import SYSTEM_WORKFLOW, workflow_message
from src.schemas import AgentProposal
from src.tools import RunContext

ATTEMPTS = 2  # first try + one repair


def run_workflow(request_id: str, ctx: RunContext) -> tuple[Assembly, dict]:
    proposals: dict = {}
    details = ctx.execute("get_request_details", {"request_id": request_id}, initiator="orchestrator")
    if details.get("status") != "ok":
        raise AgentFailed(f"request details unavailable: {details.get('error')}", proposals=proposals)
    ctx.execute("check_budget", {"request_id": request_id}, initiator="orchestrator")
    ctx.execute("search_software_catalog", {"request_id": request_id}, initiator="orchestrator")
    vendor = (details.get("untrusted_text") or {}).get("vendor_name")
    if vendor:
        ctx.execute("get_vendor_status", {"vendor_name": vendor}, initiator="orchestrator")
    policy_payload = ctx.execute("evaluate_policy_rules", {"request_id": request_id}, initiator="orchestrator")
    if ctx.policy is None:
        raise AgentFailed("policy engine result missing", proposals=proposals)

    messages = [
        {"role": "system", "content": SYSTEM_WORKFLOW},
        {"role": "user", "content": workflow_message(
            json.dumps(_raw_tool_results(ctx), default=str, separators=(",", ":")),
            json.dumps(policy_payload, default=str, separators=(",", ":")),
            _relevant_policy_text(ctx.policy),
        )},
    ]
    try:
        proposal, raw = forced_submission(ctx, messages, SUBMIT_RECOMMENDATION, AgentProposal, stage="workflow",
                                          attempts=ATTEMPTS)
    except llm.LLMUnavailable as exc:
        raise AgentFailed(f"workflow LLM call failed: {exc}", proposals=proposals) from exc
    proposals["workflow"] = proposal.model_dump() if proposal else (raw[-1] if raw else None)
    if proposal is None:
        raise AgentFailed("workflow LLM produced no valid recommendation", proposals=proposals)
    return assemble(ctx.policy, proposal, ctx), proposals
