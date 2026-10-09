from __future__ import annotations

import json
import unittest

from evals.score import reviewer_items, score_run, set_check
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


class ReviewerItemTests(unittest.TestCase):
    """C4: item-level helped/hurt against golden-derived truth (no re-assembly)."""

    def trace(self, analyst_overlap=(), reviewer_overlap=(), analyst_classes=(), reviewer_classes=()):
        def ov(pairs):
            return [{"software_id": sid, "relationship": "substitute_could_meet_need", "covers_stated_need": c,
                     "reason": "x"} for sid, c in pairs]
        return {"proposals": {"analyst": {"overlap_assessment": ov(analyst_overlap), "implied_data_classes": list(analyst_classes)},
                              "reviewer": {"overlap_assessment": ov(reviewer_overlap), "implied_data_classes": list(reviewer_classes)}}}

    def test_g08_reviewer_reversal_is_hurt(self):
        # run 2: the analyst said SW003 covers the need, the reviewer said it does not; only use_existing_tool is acceptable
        items = reviewer_items(GOLDEN["G-08"], self.trace([("SW003", True)], [("SW003", False)]))
        self.assertEqual([i["verdict"] for i in items], ["hurt"])

    def test_g01_reviewer_correction_is_helped(self):
        items = reviewer_items(GOLDEN["G-01"], self.trace([("SW010", True)], [("SW010", False)]))
        self.assertEqual(items[0]["verdict"], "helped")

    def test_both_acceptable_is_unknown(self):
        items = reviewer_items(GOLDEN["G-02"], self.trace([("SW001", True)], [("SW001", False)]))
        self.assertEqual(items[0]["verdict"], "unknown")

    def test_implied_pii_dropped_by_reviewer_is_hurt(self):
        pii = [{"data_class": "customer_pii", "quote": "names, emails, phone numbers"}]
        items = reviewer_items(GOLDEN["G-23"], self.trace(analyst_classes=pii))
        self.assertEqual([(i["item"], i["verdict"]) for i in items], [("implied PII", "hurt")])

    def test_negative_control_pii_added_by_reviewer_is_hurt(self):
        pii = [{"data_class": "customer_pii", "quote": "x"}]
        items = reviewer_items(GOLDEN["G-12"], self.trace(reviewer_classes=pii))
        self.assertEqual(items[-1]["verdict"], "hurt")

    def test_scored_only_for_staged(self):
        trace = {**self.trace([("SW003", True)], [("SW003", False)]), "decision_type": "use_existing_tool", "path": "llm"}
        d = decision(["Department Head", "Procurement"], ["existing_tool_overlap"], label="Use existing tool")
        self.assertEqual(score_run(GOLDEN["G-08"], d, trace, "staged")["reviewer_hurt"], 1)
        self.assertIsNone(score_run(GOLDEN["G-08"], d, trace, "single")["reviewer_hurt"])
