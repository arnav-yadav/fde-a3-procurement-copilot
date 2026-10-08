"""Debug one request: final decision, raw agent proposal(s) and guardrail events.

Usage: python scripts/run_one.py REQ-1001 [--arch single|staged] [--rules-only] [--fixtures] [--api-down]
Starts the mock API if it is not running. Saves the trace to runtime/traces/.
"""
from __future__ import annotations

import argparse
import atexit
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import requests  # noqa: E402

import src  # noqa: E402,F401  (loads .env)
from run_local import port_from_url, start_mock_api, stop_processes  # noqa: E402
from src.solution import handle_request_with_trace  # noqa: E402


def ensure_api() -> None:
    base = os.getenv("VENDOR_RISK_BASE_URL", "http://127.0.0.1:8001").rstrip("/")
    try:
        if requests.get(f"{base}/health", timeout=1).ok:
            return
    except requests.RequestException:
        pass
    proc = start_mock_api(port_from_url(base), env=os.environ.copy(), quiet=True)
    atexit.register(stop_processes, [proc])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("request_id")
    ap.add_argument("--arch", choices=["single", "staged"], default="single")
    ap.add_argument("--rules-only", action="store_true")
    ap.add_argument("--fixtures", action="store_true", help="load evals/fixtures (EXTRA_DATA_DIR)")
    ap.add_argument("--api-down", action="store_true", help="point the vendor client at a closed port")
    args = ap.parse_args()
    if args.fixtures:
        os.environ["EXTRA_DATA_DIR"] = "evals/fixtures"
    ensure_api()
    if args.api_down:
        os.environ["VENDOR_RISK_BASE_URL"] = "http://127.0.0.1:9"

    decision, trace = handle_request_with_trace(args.request_id, args.arch,
                                                mode="rules_only" if args.rules_only else "llm")
    print("=== FINAL DECISION")
    print(json.dumps(decision.model_dump(), indent=2))
    print("\n=== RAW PROPOSAL(S) (pre-guardrail)")
    print(json.dumps(trace["proposals"], indent=2, default=str)[:6000])
    print("\n=== GUARDRAIL / RUN EVENTS")
    for e in trace["events"]:
        if e["event"] != "llm_turn":
            print(" ", json.dumps(e, default=str)[:300])
    print("\n=== TURNS")
    for e in trace["events"]:
        if e["event"] == "llm_turn":
            print(f"  [{e['stage']}] turn {e['turn']} forced={e['forced']} calls={e['tool_calls']}")
    print(f"\npath={trace['path']} decision_type={trace['decision_type']} llm_calls={trace['llm_calls']} "
          f"tool_calls={trace['tool_calls']} active_ms={trace['latency_active_ms']:.0f} "
          f"wait_ms={trace['llm_wait_ms']:.0f} tokens={trace['tokens']['total_tokens']} error={trace['error']}")
    out = ROOT / "runtime" / "traces" / f"{trace['run_id']}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"decision": decision.model_dump(), "trace": trace}, indent=2, default=str))
    print(f"trace: {out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
