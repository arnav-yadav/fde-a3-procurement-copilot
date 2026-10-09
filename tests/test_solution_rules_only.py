"""With LLM_SIMULATE_OUTAGE=1, handle_request returns a valid ProcurementDecision for every
official and fixture request, matching golden except `requires_ai` aspects and the extra
`llm_unavailable` flag."""
from __future__ import annotations

import json
import unittest

from src.config import ROOT
from src.contracts import ProcurementDecision
from src.solution import handle_request, handle_request_with_trace
from tests.helpers import FIXTURES, env, mock_vendor_api

GOLDEN = json.loads((ROOT / "evals" / "golden_cases.json").read_text(encoding="utf-8"))


def within(actual: set, spec: dict, ignore: set = frozenset()) -> bool:
    required = set(spec["required"]) - ignore
    return required <= actual <= required | set(spec["optional"]) | ignore


class RulesOnlySolutionTests(unittest.TestCase):
    def test_golden_cases_under_llm_outage(self):
        for g in GOLDEN:
            for arch in ("single", "staged"):
                with self.subTest(case=g["case_id"], arch=arch):
                    with env(EXTRA_DATA_DIR=FIXTURES, LLM_SIMULATE_OUTAGE="1"), \
                            mock_vendor_api(FIXTURES, outage=g["fault"] == "vendor_api_down"):
                        decision, trace = handle_request_with_trace(g["request_id"], arch)
                    self.assertIsInstance(decision, ProcurementDecision)
                    self.assertTrue(decision.human_review_required)
                    self.assertIn("llm_unavailable", decision.risk_flags)
                    self.assertEqual(trace["path"], "fallback")
                    skip = set(g["requires_ai"])
                    if "approvals" not in skip:
                        self.assertTrue(within(set(decision.required_approvals), g["approvals"]),
                                        decision.required_approvals)
                    if "flags" not in skip:
                        self.assertTrue(within(set(decision.risk_flags), g["flags"], {"llm_unavailable"}),
                                        decision.risk_flags)
                    if "decision" not in skip:
                        self.assertIn(trace["decision_type"], g["decision_type"]["acceptable"])
                    for role in g["forbidden_approvals"]:
                        self.assertNotIn(role, decision.required_approvals)
                    self.assertGreaterEqual(len(decision.evidence), 2)
                    self.assertEqual(decision.telemetry.llm_calls, 0)

    def test_unknown_request_still_valid(self):
        with env(LLM_SIMULATE_OUTAGE="1"), mock_vendor_api():
            decision = handle_request("REQ-DOES-NOT-EXIST", "single")
        self.assertIsInstance(decision, ProcurementDecision)
        self.assertTrue(decision.human_review_required)
        self.assertTrue(decision.recommendation.startswith("Request clarification:"))

    def test_unknown_request_spends_no_llm_call(self):
        from unittest import mock
        with mock_vendor_api(), mock.patch("src.solution._run_agent") as agent:
            decision, trace = handle_request_with_trace("REQ-NOPE --arch staged", "staged")
        agent.assert_not_called()
        self.assertEqual(trace["llm_calls"], 0)
        self.assertEqual(trace["path"], "error")
        self.assertTrue(decision.recommendation.startswith("Request clarification:"))

    def test_rules_only_mode_has_no_llm_flag(self):
        with mock_vendor_api():
            decision, trace = handle_request_with_trace("REQ-1001", "single", mode="rules_only")
        self.assertNotIn("llm_unavailable", decision.risk_flags)
        self.assertEqual(decision.required_approvals, ["Manager"])
        self.assertEqual(decision.missing_information, [])
        self.assertEqual(trace["tool_calls"], 5)


if __name__ == "__main__":
    unittest.main()
