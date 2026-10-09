"""C9: add `trace.steps` (build_steps) to stored run files. No LLM calls; only the `steps` key is written.

Usage: python scripts/backfill_steps.py [DIR ...]      (default: evals/results/runs)
Run files written before C9 (e.g. eval run 3 from the run3-frozen tag) have no steps; the UI and this script
compute them on read from tool_log + events. Idempotent.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.trace_steps import build_steps  # noqa: E402


def backfill(directory: Path) -> tuple[int, int]:
    changed = seen = 0
    for path in sorted(directory.rglob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        trace = data.get("trace") if isinstance(data, dict) else None
        if not isinstance(trace, dict) or "tool_log" not in trace:
            continue
        seen += 1
        steps = build_steps(trace)
        if trace.get("steps") != steps:
            trace["steps"] = steps
            path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
            changed += 1
    return seen, changed


def main() -> None:
    dirs = [Path(a) for a in sys.argv[1:]] or [ROOT / "evals" / "results" / "runs"]
    for d in dirs:
        seen, changed = backfill(d)
        print(f"{d}: {seen} run files, {changed} updated")


if __name__ == "__main__":
    main()
