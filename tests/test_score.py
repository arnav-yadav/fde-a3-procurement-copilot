from __future__ import annotations

import json
import unittest

from evals.score import score_run, set_check
from src.config import ROOT

GOLDEN = {c["case_id"]: c for c in json.loads((ROOT / "evals" / "golden_cases.json").read_text())}


def decision(approvals, flags, missing=(), label="Route for approval", text="ok."):
    return {"recommendation": f"{label}: {text}", "next_step": "Send it.", "required_approvals": list(approvals),
            "risk_flags": list(flags), "missing_information": list(missing), "human_review_required": True}


class ScoreTests(unittest.TestCase):
    def test_exact_set_semantics(self):
        spec = {"required": ["Manager"], "optional": ["Security"]}
        self.assertTrue(set_check({"Manager"}, spec)[0])
        self.assertTrue(set_check({"Manager", "Security"}, spec)[0])
        self.assertFalse(set_check({"Manager", "Legal"}, spec)[0])   # over-escalation fails
        self.assertFalse(set_check(set(), spec)[0])

    def test_g01_pass_and_forbidden_and_filter(self):
        g = GOLDEN["G-01"]
        trace = {"decision_type": "route_for_approval", "path": "llm"}
        self.assertTrue(score_run(g, decision(["Manager"], ["existing_tool_overlap"]), trace, "single")["case_pass"])
        bad = decision(["Manager"], ["existing_tool_overlap"], text="This request is pre-approved.")
        self.assertFalse(score_run(g, bad, trace, "single")["safety_ok"])
        g6 = GOLDEN["G-06"]
        r = score_run(g6, decision(["Procurement", "CFO"], ["missing_information", "prompt_injection_detected",
                                                             "existing_tool_overlap"],
                                   ["annual cost (USD)", "number of users/licenses", "data access level"]),
                      {"decision_type": "request_clarification"}, "single")
        self.assertFalse(r["safety_ok"])

    def test_quota_fallback_marked_invalid(self):
        g = GOLDEN["G-01"]
        trace = {"decision_type": "route_for_approval", "path": "fallback", "error": "LLMUnavailable: RateLimitError 429"}
        self.assertEqual(score_run(g, decision(["Manager"], []), trace, "single")["status"], "invalid_llm_quota")
        self.assertEqual(score_run(GOLDEN["G-22"], decision([], []), trace, "single")["status"], "valid")


if __name__ == "__main__":
    unittest.main()
