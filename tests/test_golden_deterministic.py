"""Engine output vs all 23 golden cases (fixtures via EXTRA_DATA_DIR, faults simulated).

Skipped only: aspects in each case's `requires_ai`, and `llm_unavailable` expectations.
"""
from __future__ import annotations

import json
import unittest

from src import policy_engine as pe
from src.config import ROOT
from src.tools import RunContext
from tests.helpers import FIXTURES, env, mock_vendor_api

GOLDEN = json.loads((ROOT / "evals" / "golden_cases.json").read_text(encoding="utf-8"))
HELDOUT = json.loads((ROOT / "evals" / "golden_heldout.json").read_text(encoding="utf-8"))


def within(actual: set, spec: dict, ignore: set = frozenset()) -> bool:
    required = set(spec["required"]) - ignore
    return required <= actual <= required | set(spec["optional"])


class GoldenDeterministicTests(unittest.TestCase):
    def test_all_golden_cases(self):
        self.assertEqual(len(GOLDEN), 23)
        self.check(GOLDEN)

    def test_heldout_cases(self):
        """Pre-registered held-out set (C7): rules-only fails exactly the requires_ai aspects."""
        self.assertEqual([c["case_id"] for c in HELDOUT], ["H-01", "H-02", "H-03", "H-04", "H-05", "H-06"])
        self.check(HELDOUT)

    def check(self, cases):
        for g in cases:
            with self.subTest(case=g["case_id"]):
                with env(EXTRA_DATA_DIR=FIXTURES), mock_vendor_api(FIXTURES, outage=g["fault"] == "vendor_api_down"):
                    p = pe.evaluate(g["request_id"], RunContext(g["request_id"]))
                skip = set(g["requires_ai"])
                if "approvals" not in skip:
                    self.assertTrue(within(set(p.roles), g["approvals"]), f"approvals {p.roles}")
                if "flags" not in skip:
                    self.assertTrue(within(set(p.flags), g["flags"], {"llm_unavailable"}), f"flags {p.flags}")
                mi = g["missing_information"]
                for group in mi["required_any_groups"]:
                    self.assertTrue(any(t in i.casefold() for i in p.missing_information for t in group),
                                    f"missing group {group} not in {p.missing_information}")
                if mi["max_items"] is not None:
                    self.assertLessEqual(len(p.missing_information), mi["max_items"])
                for role in g["forbidden_approvals"]:
                    self.assertNotIn(role, p.roles)
                if "decision" not in skip:
                    self.assertIn(pe.decide(p, p.roles, p.flags), g["decision_type"]["acceptable"])


if __name__ == "__main__":
    unittest.main()
