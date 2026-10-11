"""Compare a partial run with eval run 3 on the cases both runs completed. No LLM calls.

python evals/compare_subset.py evals/results/groq_run3 [--archs workflow,single] [--write]

Only cases where every listed configuration has a valid run in the partial run are compared; the run-3 value is
the mean passes per trial over its 3 trials on the same cases. --write saves comparison.md in the partial run's folder.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evals.score import ARCH_TITLE, load_run_files  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("partial")
    ap.add_argument("--archs", default="workflow,single")
    ap.add_argument("--base", default=str(ROOT / "evals" / "results" / "runs"))
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()
    archs = args.archs.split(",")
    part = [r for r in load_run_files(Path(args.partial) / "runs") if r["architecture"] in archs]
    base = [r for r in load_run_files(Path(args.base)) if r["architecture"] in archs and r["status"] == "valid"]
    valid = {a: {r["case_id"] for r in part if r["architecture"] == a and r["status"] == "valid"} for a in archs}
    common = sorted(set.intersection(*valid.values()))
    excluded = {a: sum(1 for r in part if r["architecture"] == a and r["status"] != "valid") for a in archs}
    models = sorted({r.get("model") for r in part if r["status"] == "valid" and r.get("model")})
    lines = [f"# Partial run compared with eval run 3 on the same cases",
             "", f"Partial run model(s): {', '.join(models) or 'n/a'}. Cases where every configuration has a valid run: "
             f"{len(common)} ({', '.join(common)}). Quota-stopped runs excluded: "
             + ", ".join(f"{ARCH_TITLE[a]} {excluded[a]}" for a in archs) + ".", "",
             "| Configuration | Partial run: passes | Run 3: mean passes per trial | Partial run: avg LLM calls | Partial run: avg tokens | Partial run: failed cases |",
             "|---|---:|---:|---:|---:|---|"]
    for a in archs:
        mine = [r for r in part if r["architecture"] == a and r["case_id"] in common and r["status"] == "valid"]
        ref = [r for r in base if r["architecture"] == a and r["case_id"] in common]
        trials = len({int(r.get("trial", 1)) for r in ref}) or 1
        fails = ", ".join(sorted(r["case_id"] for r in mine if not r["case_pass"])) or "none"
        lines.append(f"| {ARCH_TITLE[a]} | {sum(r['case_pass'] for r in mine)}/{len(common)} | "
                     f"{sum(r['case_pass'] for r in ref) / trials:.2f}/{len(common)} | "
                     f"{sum(r['llm_calls'] for r in mine) / len(mine):.2f} | {sum(r['total_tokens'] for r in mine) / len(mine):.0f} | {fails} |")
    md = "\n".join(lines) + "\n"
    print(md)
    if args.write:
        (Path(args.partial) / "comparison.md").write_text(md, encoding="utf-8")


if __name__ == "__main__":
    main()
