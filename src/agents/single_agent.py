"""Architecture A: one LLM agent with six tools + submit_recommendation (T9)."""
from __future__ import annotations

from src.agents import AgentFailed
from src.agents.common import SUBMIT_RECOMMENDATION, tool_loop
from src.config import get_reference_date
from src.guardrails import Assembly, assemble
from src.prompts import NUDGE, SYSTEM_SINGLE, user_single
from src.schemas import AgentProposal
from src.tools import TOOLS, RunContext, completeness_gate

MAX_TURNS = 6


def run_single(request_id: str, ctx: RunContext) -> tuple[Assembly, dict]:
    messages = [
        {"role": "system", "content": SYSTEM_SINGLE},
        {"role": "user", "content": user_single(request_id, get_reference_date().isoformat())},
    ]
    proposal, raw = tool_loop(ctx, messages, list(TOOLS), SUBMIT_RECOMMENDATION, AgentProposal,
                              MAX_TURNS, NUDGE, stage="single")
    proposals = {"single": raw[-1] if raw else None, "submissions": raw}
    if proposal is None:
        raise AgentFailed("single agent produced no valid submit_recommendation", proposals=proposals)
    completeness_gate(ctx, request_id, include_policy=True)
    if ctx.policy is None:
        raise AgentFailed("policy engine result missing after gate", proposals=proposals)
    proposals["single"] = proposal.model_dump()
    return assemble(ctx.policy, proposal, ctx), proposals
