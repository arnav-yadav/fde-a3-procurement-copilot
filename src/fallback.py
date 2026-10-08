"""Rules-only decision (T12): used when the LLM is unavailable, and as the --no-llm baseline."""
from __future__ import annotations

from src.guardrails import Assembly, assemble
from src.tools import RunContext, completeness_gate


def run_rules_only(ctx: RunContext, request_id: str, llm_failed: bool,
                   keep_evidence: list | None = None) -> Assembly:
    """Run the evidence tools + policy engine, then assemble with templates.

    llm_failed adds the `llm_unavailable` flag; keep_evidence carries agent evidence items
    (e.g. from B's stage 1) that must still pass the grounding check."""
    completeness_gate(ctx, request_id, include_policy=True, record=False)
    if ctx.policy is None:
        raise RuntimeError("policy engine did not produce a result")
    return assemble(ctx.policy, None, ctx, extra_flags=["llm_unavailable"] if llm_failed else None,
                    extra_evidence=keep_evidence)
