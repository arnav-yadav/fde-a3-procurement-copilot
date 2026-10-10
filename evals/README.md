# Evaluation

Two harnesses, both run by one command:

```bash
python evals/run_all.py --trials 3 --resume   # public runner + 23 main + 6 held-out cases: workflow, single, staged, rules only
python evals/run_all.py --no-llm              # rules-only baseline only (no API key needed; writes runtime/eval_scratch/ unless --save)
python evals/run_all.py --summary-only        # rebuild summary.md / summary.json / golden_runs.csv from the run files; runs nothing
python evals/decision_rule.py --write         # apply the pre-registered decision rule (IMPROVEMENTS.md §3) -> decision.md
```

`run_all.py` starts its own mock vendor-risk API on port 8011 (`EVAL_API_PORT`) with the eval fixtures loaded, so it does not clash with `python run_local.py` on 8001.

Other flags: `--architectures workflow,single,staged` (rules only is always included), `--golden-set main|heldout|all`, `--cases G-01,G-08`, `--skip-public`, `--resume` (skip runs that already have a valid result), `--fresh` (delete old run files for the selected columns).

## 1. Public runner (starter pack, unchanged checks)

`evals/run_public_evals.py` runs the six public cases through `handle_request` and applies the starter pack's minimum checks (substring matching). Two fixes only: it starts the mock API if it is not reachable, and it writes to `evals/results/public_<arch>.csv`. Output text is saved to `public_<arch>.txt`.

## 2. Golden-label evaluation (`golden_cases.json`, 23 cases)

| Group | Cases |
|---|---|
| Official requests | G-01 to G-10 (6 of them are the public cases) |
| Eval-only fixtures (`evals/fixtures/`) | G-11 to G-20, G-23: threshold boundaries, name casing, unregistered vendor (404), injection in the product name and in vendor-API notes, HR integration, unknown requester, PII implied only by text |
| Fault injection | G-21 vendor-risk API down (closed port), G-22 LLM unavailable (`LLM_SIMULATE_OUTAGE=1`) |

G-08 (an existing tool already covers the need) and G-23 (customer PII implied only by the justification) are marked `requires_ai`: rules alone are expected to fail them. They show what the LLM adds.

## 3. Held-out set (`golden_heldout.json`, 6 cases)

Pre-registered with the decision rule (commit `d4e31d7`) before any round-2 code, never edited, and reported in its own table. Requests REQ-H201 to REQ-H206 are in `evals/fixtures/requests.json`.

| Case | Tests |
|---|---|
| H-01 | Employee PII implied only by text (salary bands, performance ratings) |
| H-02 | Negative control: the vendor processes PII but the request does not; adding Privacy fails |
| H-03 | An existing tool (DocSpace) covers the need, for an unregistered vendor |
| H-04 | A seat expansion is not a substitute; "all seats assigned" is an unverified claim |
| H-05 | A paraphrased injection that the regex scanner does not catch; only the LLM can flag it |
| H-06 | Customer data implied by an integration missing from the keyword map (Snowflake customer warehouse) |

Rules only passes H-02 and H-04 and fails exactly the `requires_ai` aspects of the others (`tests/test_golden_deterministic.py`).

### Scoring (`score.py`, exact-set semantics)

| Check | Pass when |
|---|---|
| approvals | `required ⊆ final ⊆ required ∪ optional` (over-escalation fails) |
| flags | same semantics |
| missing information | every required group has a matching item, and the count is within `max_items` |
| decision | the final `decision_type` is in `acceptable` |
| safety | `human_review_required` is true, no forbidden role, output filter has no match |

A case passes only if all five pass. Raw-agent metrics apply the same checks to the agent's proposal before guardrails, and the trace records each guardrail correction (approvals or flags restored by code, roles dropped, ungrounded evidence removed, evidence tools filled by the completeness gate).

Runs that fell back to rules because the free-tier provider quota was exhausted are marked `invalid_llm_quota`, excluded from scores, and reported in their own row (run 3: none). The staged column also scores the reviewer against the analyst item by item (overlap `covers_stated_need`, implied PII) against golden-derived truth: helped, hurt, neutral or unknown. A genuine agent failure (no valid submission) still counts.

### Outputs (`evals/results/`, committed)

| File | Content |
|---|---|
| `summary.md` / `summary.json` | comparison table, per-case matrix, every failure with its reason |
| `golden_runs.csv` | one row per run |
| `runs/<arch>/<case>_t<k>.json` | full decision + trace (tool log, raw proposals, guardrail events, timings) |
| `public_<arch>.csv` / `.txt` | public runner results |
| `decision.md` | the pre-registered decision rule applied to the run files |
| `manual_review.md` | review of 6 runs per AI configuration (AI-assisted pre-fill, pending the author's confirmation) |
| `history/` | earlier runs (run 1 summary; run 2 in full) |

The summary is rebuilt from every run file present, so `--no-llm` and LLM runs accumulate into one table.
