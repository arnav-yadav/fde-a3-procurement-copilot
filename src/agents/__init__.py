"""Agent architectures: A (single_agent) and B (staged_agent)."""
from __future__ import annotations


class AgentFailed(Exception):
    """The agent could not produce a valid proposal.

    salvage: agent evidence items to keep if they pass the grounding check.
    proposals: raw (pre-guardrail) outputs gathered before the failure, for the trace."""

    def __init__(self, message: str, salvage: list | None = None, proposals: dict | None = None):
        super().__init__(message)
        self.salvage = salvage or []
        self.proposals = proposals or {}
