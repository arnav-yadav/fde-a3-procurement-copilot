"""Replay the current guardrails on recorded LLM outputs. No LLM calls.

python evals/replay_guardrails.py [--runs evals/results/runs] [--out evals/results/replay_overlap]

Every tool is deterministic, and every run file stores the AI proposal the guardrails received. For each LLM run,
this re-runs the evidence tools and the policy engine for the same request and fault (mock vendor-risk API,
EXTRA_DATA_DIR=evals/fixtures), passes the stored proposal through the *current* `guardrails.assemble`, and
scores the result. Rules-only and fallback runs are kept as stored (no AI proposal to replay).

Use it to measure a guardrail change on the exact model outputs of an earlier run: replay once with the old code
(it should reproduce the stored scores), change the guardrail, replay again. Writes replay.md and rows.json to --out.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

import src  # noqa: E402,F401  (loads .env)
from evals.decision_rule import evaluate_rule  # noqa: E402
from evals.run_all import DEAD_API, FIXTURES, set_env  # noqa: E402
from evals.score import ARCH_TITLE, load_run_files, raw_proposal, score_run  # noqa: E402
from run_local import start_mock_api, stop_processes  # noqa: E402

PORT = int(os.getenv("REPLAY_API_PORT", "8013"))


def replay_run(case: dict, data: dict, base_url: str) -> dict:
    from src.guardrails import assemble
    from src.schemas import AgentProposal
    from src.tools import RunContext, completeness_gate

    trace, arch = data["trace"], data["architecture"]
    raw = raw_proposal(trace)
    env = {"VENDOR_RISK_BASE_URL": DEAD_API if case["fault"] == "vendor_api_down" else base_url,
           "LLM_SIMULATE_OUTAGE": None}
    old = set_env(env)
    try:
        ctx = RunContext(case["request_id"])
        completeness_gate(ctx, case["request_id"], include_policy=True, record=False)
        assembly = assemble(ctx.policy, AgentProposal.model_validate(raw), ctx)
    finally:
        set_env(old)
    events = Counter(e["event"] for e in ctx.events)
    new_trace = {**trace, "decision_type": assembly.decision_type, "event_counts": dict(events)}
    row = score_run(case, assembly.decision.model_dump(), new_trace, arch)
    row["trial"] = data["score"].get("trial")
    row["replayed"] = True
    row["new_flags"] = assembly.decision.risk_flags
    return row


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", default="evals/results/runs")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    cases = {c["case_id"]: c for c in json.loads((ROOT / "evals/golden_cases.json").read_text(encoding="utf-8"))
             + json.loads((ROOT / "evals/golden_heldout.json").read_text(encoding="utf-8"))}
    main_cases = [c for c in json.loads((ROOT / "evals/golden_cases.json").read_text(encoding="utf-8"))]
    held_cases = json.loads((ROOT / "evals/golden_heldout.json").read_text(encoding="utf-8"))

    set_env({"EXTRA_DATA_DIR": FIXTURES})
    base_url = f"http://127.0.0.1:{PORT}"
    api = start_mock_api(PORT, env={**os.environ, "EXTRA_DATA_DIR": FIXTURES}, quiet=True)
    stored, replayed, changed = [], [], []
    try:
        for path in sorted(Path(args.runs).glob("*/*.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            s = data["score"]
            stored.append(s)
            replayable = (data["architecture"] != "rules_only" and s["status"] == "valid"
                          and data["trace"].get("path") == "llm" and raw_proposal(data["trace"]) is not None)
            if not replayable:
                replayed.append({**s, "replayed": False})
                continue
            r = replay_run(cases[s["case_id"]], data, base_url)
            replayed.append(r)
            if r["case_pass"] != s["case_pass"] or r["failures"] != s["failures"]:
                changed.append({"case": s["case_id"], "arch": data["architecture"], "trial": s.get("trial"),
                                "stored": "PASS" if s["case_pass"] else f"FAIL ({s['failures'][:90]})",
                                "replay": "PASS" if r["case_pass"] else f"FAIL ({r['failures'][:90]})"})
    finally:
        stop_processes([api])

    before = evaluate_rule(stored, main_cases, held_cases)["configs"]
    after_res = evaluate_rule(replayed, main_cases, held_cases)
    after = after_res["configs"]
    lines = ["# Guardrail replay on recorded LLM outputs", "",
             f"Runs: `{args.runs}` ({len(stored)} files; {sum(r.get('replayed') for r in replayed)} LLM runs replayed "
             "through the current guardrails, the rest kept as stored). No LLM calls.", "",
             "| Mean passes per trial | " + " | ".join(ARCH_TITLE[a] for a in before) + " |",
             "|---|" + "---:|" * len(before)]
    for label, key in (("Main (of 23), stored", "main_mean"), ("Main, replayed", "main_mean"),
                       ("Held-out (of 6), stored", "heldout_mean"), ("Held-out, replayed", "heldout_mean")):
        src_cfg = after if "replayed" in label else before
        lines.append(f"| {label} | " + " | ".join(f"{src_cfg[a][key]:.2f}" for a in before) + " |")
    lines.append("| Safety gate, replayed | " + " | ".join("pass" if after[a]["eligible"] else "fail" for a in after) + " |")
    lines += ["", f"Decision rule on the replayed runs: **{ARCH_TITLE.get(after_res['choice'], after_res['choice'])}**.", "",
              f"## Runs whose result changed ({len(changed)})", ""]
    if changed:
        lines += ["| Case | Configuration | Trial | Stored | Replayed |", "|---|---|---|---|---|"]
        lines += [f"| {c['case']} | {ARCH_TITLE[c['arch']]} | {c['trial']} | {c['stored']} | {c['replay']} |" for c in changed]
    else:
        lines.append("None: the replay reproduces every stored result.")
    md = "\n".join(lines) + "\n"
    print(md)
    if args.out:
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        (out / "replay.md").write_text(md, encoding="utf-8")
        (out / "rows.json").write_text(json.dumps(replayed, indent=1, default=str), encoding="utf-8")


if __name__ == "__main__":
    main()
