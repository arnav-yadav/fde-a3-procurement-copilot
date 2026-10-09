"""Agent orchestration with a scripted LLM (no network): tool scoping, handoff, call counts."""
from __future__ import annotations

import unittest

from src.agents.staged_agent import run_staged
from src.tools import AGENT_TOOLS, RunContext
from tests.fake_llm import PACK, FakeLLM, recommendation
from tests.helpers import mock_vendor_api

LOOKUPS = [("check_budget", {"request_id": "REQ-1008"}), ("search_software_catalog", {"request_id": "REQ-1008"}),
           ("get_vendor_status", {"vendor_name": "TaskFlow"})]


class StagedScopingTests(unittest.TestCase):
    def test_analyst_gets_three_lookup_tools_and_prefetched_details(self):
        fake = FakeLLM([LOOKUPS, [("submit_evidence_pack", PACK)],
                        [("submit_recommendation", recommendation(
                            required_approvals=[{"role": "Department Head", "reason": "tier"},
                                                {"role": "Procurement", "reason": "tier"}],
                            risk_flags=["existing_tool_overlap"]))]])
        ctx = RunContext("REQ-1008")
        with mock_vendor_api(), fake.patched():
            assembly, proposals = run_staged("REQ-1008", ctx)
        first = fake.calls[0]
        self.assertEqual(first["tools"], AGENT_TOOLS["analyst"] + ["submit_evidence_pack"])
        self.assertNotIn("lookup_policy_section", first["tools"])
        user_msg = first["messages"][1]["content"]
        self.assertIn("UNTRUSTED BUSINESS DATA", user_msg)
        self.assertIn("TaskFlow Pro", user_msg)
        self.assertEqual((ctx.tool_log[0]["tool"], ctx.tool_log[0]["initiator"]), ("get_request_details", "orchestrator"))
        agent_vendor_calls = [e for e in ctx.tool_log if e["tool"] == "get_vendor_status" and e["initiator"] == "agent"]
        self.assertEqual(len(agent_vendor_calls), 1)
        self.assertEqual(ctx.counters["llm_calls"], 3)
        self.assertEqual(fake.calls[2]["kind"], "forced")  # reviewer: no tools, forced submit
        turns = [e for e in ctx.events if e["event"] == "llm_turn" and e["stage"] == "analyst"]
        self.assertEqual(turns[0]["tools_exposed"]["count"], 4)
        self.assertEqual(assembly.decision.required_approvals, ["Department Head", "Procurement"])


if __name__ == "__main__":
    unittest.main()
