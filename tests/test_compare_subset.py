"""evals/compare_subset.py: a partial run is compared with run 3 only on cases every configuration completed."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from src.config import ROOT


def write(base: Path, arch: str, case: str, trial: int, ok: bool, status: str = "valid") -> None:
    p = base / "runs" / arch / f"{case}_t{trial}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"score": {"case_id": case, "architecture": arch, "trial": trial, "status": status,
                                       "case_pass": ok, "llm_calls": 1, "total_tokens": 100, "model": "m"}}))


class CompareSubsetTests(unittest.TestCase):
    def test_only_common_valid_cases_are_compared(self):
        with tempfile.TemporaryDirectory() as tmp:
            part, base = Path(tmp) / "part", Path(tmp) / "base"
            write(part, "workflow", "G-01", 1, False); write(part, "single", "G-01", 1, True)
            write(part, "workflow", "G-02", 1, True); write(part, "single", "G-02", 1, False, status="invalid_llm_quota")
            for t in (1, 2, 3):
                for a in ("workflow", "single"):
                    write(base, a, "G-01", t, True); write(base, a, "G-02", t, True)
            out = subprocess.run([sys.executable, str(ROOT / "evals" / "compare_subset.py"), str(part),
                                  "--base", str(base / "runs")], capture_output=True, text=True, check=True).stdout
        self.assertIn("1 (G-01)", out)
        self.assertIn("| Workflow + 1 LLM | 0/1 | 1.00/1 |", out)
        self.assertIn("| Single (A) | 1/1 | 1.00/1 |", out)
        self.assertIn("Single (A) 1", out)  # one quota-stopped run excluded


if __name__ == "__main__":
    unittest.main()
