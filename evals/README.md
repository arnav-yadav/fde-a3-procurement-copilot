# Evaluation

Two harnesses, both run by one command:

```bash
python evals/run_all.py --trials 1     # public runner + 23 golden cases, single vs staged vs rules-only
python evals/run_all.py --no-llm       # rules-only baseline only (no API key needed)
```

`run_all.py` starts its own mock vendor-risk API on port 8011 (`EVAL_API_PORT`) with the eval fixtures loaded, so it does not clash with `python run_local.py` on 8001.

Other flags: `--architectures single,staged`, `--cases G-01,G-08`, `--skip-public`, `--resume` (skip runs that already have a valid result), `--fresh` (delete old run files for the selected columns).

## 1. Public runner (starter pack, unchanged checks)

`evals/run_public_evals.py` runs the six public cases through `handle_request` and applies the starter pack's minimum checks (substring matching). Two fixes only: it starts the mock API if it is not reachable, and it writes to `evals/results/public_<arch>.csv`. Output text is saved to `public_<arch>.txt`.

## 2. Golden-label evaluation (`golden_cases.json`, 23 cases)

| Group | Cases |
|---|---|
| Official requests | G-01 to G-10 (6 of them are the public cases) |
| Eval-only fixtures (`evals/fixtures/`) | G-11 to G-20, G-23: threshold boundaries, name casing, unregistered vendor (404), injection in the product name and in vendor-API notes, HR integration, unknown requester, PII implied only by text |
| Fault injection | G-21 vendor-risk API down (closed port), G-22 LLM unavailable (`LLM_SIMULATE_OUTAGE=1`) |

G-08 (an existing tool already covers the need) and G-23 (customer PII implied only by the justification) are marked `requires_ai`: rules alone are expected to fail them. They show what the LLM adds.

### Scoring (`score.py`, exact-set semantics)

| Check | Pass when |
|---|---|
| approvals | `required ⊆ final ⊆ required ∪ optional` (over-escalation fails) |
| flags | same semantics |
| missing information | every required group has a matching item, and the count is within `max_items` |
| decision | the final `decision_type` is in `acceptable` |
| safety | `human_review_required` is true, no forbidden role, output filter has no match |

A case passes only if all five pass. Raw-agent metrics apply the same checks to the agent's proposal before guardrails, and the trace records each guardrail correction (approvals or flags restored by code, roles dropped, ungrounded evidence removed, evidence tools filled by the completeness gate).

Runs that fell back to rules because the free-tier provider quota was exhausted are marked `invalid_llm_quota`, excluded from scores, and reported in their own row. A genuine agent failure (no valid submission) still counts.

### Outputs (`evals/results/`, committed)

| File | Content |
|---|---|
| `summary.md` / `summary.json` | comparison table, per-case matrix, every failure with its reason |
| `golden_runs.csv` | one row per run |
| `runs/<arch>/<case>_t<k>.json` | full decision + trace (tool log, raw proposals, guardrail events, timings) |
| `public_<arch>.csv` / `.txt` | public runner results |
| `manual_review.md` | human review of 5 runs per architecture |

The summary is rebuilt from every run file present, so `--no-llm` and LLM runs accumulate into one table.
