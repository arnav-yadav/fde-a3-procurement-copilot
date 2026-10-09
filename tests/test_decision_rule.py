"""IMPROVEMENTS.md §3 decision rule, applied to synthetic run rows."""
from __future__ import annotations

import json
import unittest

from evals.decision_rule import evaluate_rule
from src.config import ROOT

MAIN = json.loads((ROOT / "evals" / "golden_cases.json").read_text(encoding="utf-8"))
HELD = json.loads((ROOT / "evals" / "golden_heldout.json").read_text(encoding="utf-8"))


def rows(arch: str, fail: set[str], llm_calls: int, trials: int = 2, quota: set[str] = frozenset()) -> list[dict]:
    return [{"case_id": c["case_id"], "architecture": arch, "trial": t, "llm_calls": llm_calls,
             "status": "invalid_llm_quota" if c["case_id"] in quota else "valid", "case_pass": c["case_id"] not in fail}
            for t in range(1, trials + 1) for c in MAIN + HELD]


class DecisionRuleTests(unittest.TestCase):
    def test_cheapest_config_within_one_case_wins(self):
        r = evaluate_rule(rows("rules_only", {"G-08", "G-23", "H-01", "H-03", "H-05", "H-06"}, 0)
                          + rows("workflow", {"G-08", "H-03"}, 1)
                          + rows("single", {"H-03"}, 3), MAIN, HELD)
        self.assertFalse(r["configs"]["rules_only"]["eligible"])  # H-05 is the held-out injection case
        self.assertEqual(r["choice"], "workflow")
        self.assertTrue(r["floor"]["met"])

    def test_more_than_one_case_behind_is_not_a_candidate(self):
        r = evaluate_rule(rows("workflow", {"G-08", "G-23", "H-03"}, 1) + rows("single", {"H-03"}, 3), MAIN, HELD)
        self.assertFalse(r["configs"]["workflow"]["main_within_1"])
        self.assertEqual(r["choice"], "single")

    def test_safety_failure_in_any_trial_makes_ineligible(self):
        rs = rows("workflow", set(), 1) + rows("single", {"H-03"}, 3)
        next(x for x in rs if x["architecture"] == "workflow" and x["case_id"] == "G-21" and x["trial"] == 2)["case_pass"] = False
        r = evaluate_rule(rs, MAIN, HELD)
        self.assertEqual(r["configs"]["workflow"]["safety_failures"], ["G-21 t2"])
        self.assertEqual(r["choice"], "single")

    def test_floor_not_met_and_quota_runs_excluded(self):
        ai = {"G-08", "G-23", "H-01", "H-03", "H-05", "H-06"}
        r = evaluate_rule(rows("rules_only", ai, 0) + rows("single", ai - {"G-08"}, 3, quota={"G-01"}), MAIN, HELD)
        self.assertFalse(r["floor"]["met"])
        self.assertEqual(r["configs"]["single"]["excluded_quota_runs"], 2)
        self.assertEqual(r["configs"]["single"]["main_mean"], 21.0)  # G-01 excluded, G-23 failed


if __name__ == "__main__":
    unittest.main()
