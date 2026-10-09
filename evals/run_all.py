"""One-command evaluation (T14).

python evals/run_all.py [--trials N] [--architectures workflow,single,staged] [--no-llm [--save]] [--cases G-01,...]
                        [--golden-set main|heldout|all]
                        [--skip-public] [--resume] [--fresh]

- Starts the mock API on EVAL_API_PORT (default 8011) with EXTRA_DATA_DIR=evals/fixtures.
- Public runner per architecture (subprocess) -> evals/results/public_<arch>.{txt,csv}.
- Golden runs per case x architecture x trial (faults injected per case) ->
  evals/results/runs/<arch>/<case>_t<k>.json, golden_runs.csv, summary.json, summary.md.
- The rules-only baseline is always included (it costs no LLM calls). --no-llm runs only that and writes to
  runtime/eval_scratch/ (untracked) unless --save is given, so it never overwrites committed results.
- The summary is rebuilt from every run file present, so separate invocations accumulate.
  --resume skips runs that already have a valid result; --fresh deletes old runs for the selected columns.
- If the provider quota is exhausted, remaining LLM runs are skipped (resume later with --resume).
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import src  # noqa: E402,F401  (loads .env)
from evals.score import (  # noqa: E402
    ARCH_ORDER, load_run_files, public_result, score_run, summarize, write_csv,
)
from run_local import start_mock_api, stop_processes  # noqa: E402

RESULTS = ROOT / "evals" / "results"
RUNS = RESULTS / "runs"
FIXTURES = "evals/fixtures"
DEAD_API = "http://127.0.0.1:9"


def set_env(values: dict) -> dict:
    old = {k: os.environ.get(k) for k in values}
    for k, v in values.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    return old


def run_public(arch: str, env: dict) -> None:
    runner_arch = "single" if arch == "rules_only" else arch
    sub_env = dict(env)
    if arch == "rules_only":
        sub_env["LLM_SIMULATE_OUTAGE"] = "1"
    out_csv = RESULTS / f"public_{arch}.csv"
    print(f"  public runner: {arch} ...", flush=True)
    proc = subprocess.run(
        [sys.executable, "evals/run_public_evals.py", "--architecture", runner_arch, "--no-start-api",
         "--out", str(out_csv.relative_to(ROOT))],
        cwd=ROOT, env=sub_env, capture_output=True, text=True,
    )
    text = "\n".join(line for line in (proc.stdout + proc.stderr).splitlines()
                     if "Starlette" not in line and "from starlette" not in line)
    if arch == "rules_only":
        text = "(rules only: LLM_SIMULATE_OUTAGE=1)\n" + text
    (RESULTS / f"public_{arch}.txt").write_text(text + "\n", encoding="utf-8")
    print(f"    -> {public_result(out_csv)}", flush=True)


def golden_run(case: dict, arch: str, trial: int, base_url: str) -> dict:
    from src.solution import handle_request_with_trace

    fault_env: dict = {"VENDOR_RISK_BASE_URL": base_url, "LLM_SIMULATE_OUTAGE": None, "EVAL_CASE_ID": case["case_id"]}
    if case["fault"] == "vendor_api_down":
        fault_env["VENDOR_RISK_BASE_URL"] = DEAD_API
    if case["fault"] == "llm_unavailable":
        fault_env["LLM_SIMULATE_OUTAGE"] = "1"
    # Rules-only column: the LLM-outage fault case runs the same fallback path, which adds llm_unavailable.
    mode = "rules_only" if arch == "rules_only" and case["fault"] != "llm_unavailable" else "llm"
    architecture = "single" if arch == "rules_only" else arch
    old = set_env(fault_env)
    try:
        decision, trace = handle_request_with_trace(case["request_id"], architecture, mode=mode)
    finally:
        set_env(old)
    d = decision.model_dump()
    score = score_run(case, d, trace, arch)
    score["trial"] = trial
    return {"case": case["case_id"], "architecture": arch, "trial": trial, "decision": d, "trace": trace,
            "score": score}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--trials", type=int, default=1)
    ap.add_argument("--architectures", default="workflow,single,staged")
    ap.add_argument("--golden-set", choices=["main", "heldout", "all"], default="all",
                    help="main = golden_cases.json (23), heldout = golden_heldout.json (6); reported separately")
    ap.add_argument("--no-llm", action="store_true", help="rules-only baseline only (no key needed)")
    ap.add_argument("--cases", default="", help="comma-separated golden case IDs")
    ap.add_argument("--skip-public", action="store_true")
    ap.add_argument("--resume", action="store_true", help="skip runs that already have a valid result file")
    ap.add_argument("--fresh", action="store_true", help="delete existing run files for the selected columns")
    ap.add_argument("--save", action="store_true",
                    help="with --no-llm: write to evals/results/ (default: runtime/eval_scratch/, untracked)")
    args = ap.parse_args()

    global RESULTS, RUNS
    if args.no_llm and not args.save:  # a deterministic check must not overwrite the committed LLM results
        RESULTS = ROOT / "runtime" / "eval_scratch"
        RUNS = RESULTS / "runs"

    archs = ["rules_only"] if args.no_llm else [a for a in args.architectures.split(",") if a] + ["rules_only"]
    archs = [a for a in ARCH_ORDER if a in archs]
    cases = json.loads((ROOT / "evals" / "golden_cases.json").read_text(encoding="utf-8"))
    heldout_path = ROOT / "evals" / "golden_heldout.json"
    heldout = json.loads(heldout_path.read_text(encoding="utf-8")) if heldout_path.is_file() else []
    pool = {"main": cases, "heldout": heldout, "all": cases + heldout}[args.golden_set]
    selected = [c for c in pool if not args.cases or c["case_id"] in args.cases.split(",")]

    RESULTS.mkdir(parents=True, exist_ok=True)
    if args.fresh:
        for a in archs:
            shutil.rmtree(RUNS / a, ignore_errors=True)

    port = int(os.getenv("EVAL_API_PORT", "8011"))
    base_url = f"http://127.0.0.1:{port}"
    env_values = {"VENDOR_RISK_BASE_URL": base_url, "EXTRA_DATA_DIR": FIXTURES}
    set_env(env_values)
    api = start_mock_api(port, env={**os.environ, **env_values}, quiet=True)
    print(f"Mock vendor-risk API on {base_url} (EXTRA_DATA_DIR={FIXTURES})")
    from src.config import get_settings
    s = get_settings()
    print(f"LLM: {s.llm_provider} / {s.model_name} · columns: {', '.join(archs)} · cases: {len(selected)} · "
          f"trials: {args.trials}")
    started = time.perf_counter()
    try:
        if not args.skip_public:
            print("Public runner:")
            for a in archs:
                run_public(a, dict(os.environ))

        print("Golden runs:")
        quota_hit: set[str] = set()
        for trial in range(1, args.trials + 1):
            for case in selected:
                for a in archs:
                    out = RUNS / a / f"{case['case_id']}_t{trial}.json"
                    if args.resume and out.is_file():
                        prev = json.loads(out.read_text(encoding="utf-8"))
                        if prev.get("score", {}).get("status") == "valid":
                            continue
                    if a in quota_hit:
                        continue
                    result = golden_run(case, a, trial, base_url)
                    out.parent.mkdir(parents=True, exist_ok=True)
                    out.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
                    sc = result["score"]
                    mark = "QUOTA" if sc["status"] != "valid" else ("PASS" if sc["case_pass"] else "FAIL")
                    print(f"  t{trial} {case['case_id']:5s} {a:10s} {mark:5s} {sc['decision_type'] or '-':28s} "
                          f"llm={sc['llm_calls']} tools={sc['tool_calls']} active={sc['latency_active_ms']:.0f}ms "
                          f"total={sc['latency_total_ms']:.0f}ms {sc['failures'][:150]}", flush=True)
                    if sc["status"] == "invalid_llm_quota":
                        quota_hit.add(a)
                        print(f"  !! provider quota exhausted for {a}: skipping its remaining runs "
                              "(rerun later with --resume, or set another MODEL_NAME/key)", flush=True)
    finally:
        stop_processes([api])

    rows = load_run_files(RUNS)
    write_csv(rows, RESULTS / "golden_runs.csv")
    public = {a: public_result(RESULTS / f"public_{a}.csv") for a in ARCH_ORDER}
    summary, md = summarize(rows, cases, public, heldout)
    models = sorted({r["model"] for r in rows if r["architecture"] != "rules_only" and r.get("model")
                     and r["status"] == "valid" and r["path"] == "llm"})
    summary["models"] = models
    (RESULTS / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    md = md.replace("generated by `evals/run_all.py`.",
                    f"LLM model(s): {', '.join(models) or 'none'} · generated by `evals/run_all.py`.")
    (RESULTS / "summary.md").write_text(md, encoding="utf-8")
    print(f"\nDone in {time.perf_counter() - started:.0f}s. Wrote {RESULTS.relative_to(ROOT).as_posix()}/summary.md, "
          "summary.json, golden_runs.csv, runs/")
    print(md.split("## Per-case matrix")[0])


if __name__ == "__main__":
    main()
