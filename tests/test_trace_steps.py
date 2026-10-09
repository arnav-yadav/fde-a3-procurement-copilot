"""C9: build_steps(trace) — who acted, why, which tools, cost — on new traces and on traces stored before it existed."""
from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest import mock

from src.config import ROOT
from src.solution import handle_request_with_trace
from src.trace_steps import build_steps, corrections, raw_vs_final
from tests.fake_llm import PACK, FakeLLM, recommendation
from tests.helpers import env, mock_vendor_api

LOOKUPS = [("check_budget", {"request_id": "REQ-1008"}), ("search_software_catalog", {"request_id": "REQ-1008"}),
           ("get_vendor_status", {"vendor_name": "TaskFlow"})]
TIER = [{"role": "Department Head", "reason": "tier"}, {"role": "Procurement", "reason": "tier"}]
LLM_ON = SimpleNamespace(llm_simulate_outage=False, llm_configured=True, llm_provider="fake", model_name="fake-model")


def run(arch: str, script: list) -> dict:
    fake = FakeLLM(script)
    with mock_vendor_api(), fake.patched(), mock.patch("src.solution.get_settings", return_value=LLM_ON):
        _, trace = handle_request_with_trace("REQ-1008", arch)
    assert trace["path"] == "llm", trace["error"]
    return trace


def actors(steps: list[dict]) -> list[str]:
    return [s["actor"] for s in steps]


class BuildStepsTests(unittest.TestCase):
    def test_workflow_steps(self):
        trace = run("workflow", [[("submit_recommendation", recommendation(
            required_approvals=TIER, risk_flags=["existing_tool_overlap"]))]])
        steps = trace["steps"]
        self.assertEqual(actors(steps), ["Orchestrator (code)", "Policy engine (code)", "Workflow LLM call",
                                         "Guardrails (code)"])
        self.assertEqual(steps[0]["tools"], ["get_request_details", "check_budget", "search_software_catalog",
                                             "get_vendor_status"])
        self.assertEqual(steps[1]["tools"], ["evaluate_policy_rules"])
        self.assertEqual((steps[2]["llm_calls"], steps[2]["tokens"], steps[2]["tools"]), (1, 110, []))
        self.assertEqual(len(trace["llm_log"]), 1)

    def test_staged_steps_split_cost_per_stage(self):
        trace = run("staged", [LOOKUPS, [("submit_evidence_pack", PACK)],
                               [("submit_recommendation", recommendation(required_approvals=TIER,
                                                                         risk_flags=["existing_tool_overlap"]))]])
        steps = trace["steps"]
        self.assertEqual(actors(steps), ["Orchestrator (code)", "Analyst agent (LLM)", "Policy engine (code)",
                                         "Policy and risk reviewer (LLM)", "Guardrails (code)"])
        self.assertEqual(steps[0]["tools"], ["get_request_details"])
        self.assertEqual(steps[1]["tools"], ["check_budget", "search_software_catalog", "get_vendor_status"])
        self.assertEqual((steps[1]["llm_calls"], steps[1]["tokens"]), (2, 220))
        self.assertEqual((steps[3]["llm_calls"], steps[3]["tokens"], steps[3]["tools"]), (1, 110, []))
        self.assertEqual(json.loads(json.dumps(steps)), steps)  # JSON-safe for the stored trace

    def test_staged_gate_fill_gets_its_own_step_after_the_analyst(self):
        # the analyst skips check_budget; the completeness gate runs it before the policy engine
        trace = run("staged", [LOOKUPS[1:], [("submit_evidence_pack", PACK)],
                               [("submit_recommendation", recommendation(required_approvals=TIER,
                                                                         risk_flags=["existing_tool_overlap"]))]])
        steps = trace["steps"]
        self.assertEqual(actors(steps), ["Orchestrator (code)", "Analyst agent (LLM)", "Completeness gate (code)",
                                         "Policy engine (code)", "Policy and risk reviewer (LLM)", "Guardrails (code)"])
        self.assertEqual(steps[0]["tools"], ["get_request_details"])
        self.assertEqual(steps[2]["tools"], ["check_budget"])

    def test_trace_without_llm_log(self):
        # stored before llm_log existed: a two-stage run cannot split tokens; a one-stage run uses the totals
        trace = run("staged", [LOOKUPS, [("submit_evidence_pack", PACK)],
                               [("submit_recommendation", recommendation(required_approvals=TIER,
                                                                         risk_flags=["existing_tool_overlap"]))]])
        old = {k: v for k, v in trace.items() if k not in ("llm_log", "steps")}
        steps = build_steps(old)
        self.assertEqual([s["tokens"] for s in steps if s["kind"] == "llm"], [None, None])
        single = {**old, "architecture": "single", "tokens": {"total_tokens": 999}, "llm_ms": 5.0,
                  "events": [{"event": "llm_turn", "stage": "single"}] * 3}
        llm = [s for s in build_steps(single) if s["kind"] == "llm"]
        self.assertEqual((llm[0]["llm_calls"], llm[0]["tokens"], llm[0]["ms"]), (3, 999, 5.0))

    def test_archived_run_files_all_build(self):
        files = sorted((ROOT / "evals" / "results" / "history").rglob("runs/*/*.json"))
        self.assertTrue(files)
        for f in files:
            steps = build_steps(json.loads(f.read_text(encoding="utf-8"))["trace"])
            self.assertTrue(steps, f)
            self.assertIn("Policy engine (code)", actors(steps), f)

    def test_rules_only_and_fallback(self):
        with mock_vendor_api():
            _, rules = handle_request_with_trace("REQ-1008", "single", mode="rules_only")
        self.assertEqual(actors(rules["steps"]), ["Evidence tools (code)", "Policy engine (code)"])
        self.assertIsNone(raw_vs_final(rules))
        with env(LLM_SIMULATE_OUTAGE="1"), mock_vendor_api():
            _, fb = handle_request_with_trace("REQ-1008", "staged")
        self.assertEqual(actors(fb["steps"]), ["Rules-only fallback (code)", "Evidence tools (code)",
                                               "Policy engine (code)"])
        self.assertIn("simulated outage", fb["steps"][0]["note"])

    def test_corrections_and_raw_vs_final(self):
        # the AI asks for Privacy with no data class: code drops the role and asks the reviewer instead
        trace = run("workflow", [[("submit_recommendation", recommendation(
            required_approvals=TIER + [{"role": "Privacy", "reason": "vendor processes personal data"}],
            risk_flags=["existing_tool_overlap"]))]])
        labels = [c["label"] for c in corrections(trace)]
        self.assertIn("AI-proposed review turned into a reviewer question", labels)
        rvf = raw_vs_final(trace)
        self.assertIn("Privacy", rvf["ai_approvals"])
        self.assertNotIn("Privacy", rvf["final_approvals"])
        self.assertEqual(trace["steps"][-1]["note"], f"{len(labels)} correction(s)")


if __name__ == "__main__":
    unittest.main()
