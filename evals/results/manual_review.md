# Manual review ("policy failures found manually")

A human reads 5 runs per architecture from `evals/results/runs/`: REQ-1002 (G-02), REQ-1004 (G-04), REQ-1006 (G-06), REQ-1007 (G-07), REQ-1008 (G-08), plus one fault case (G-21).

For each run record: is the recommendation sensible, does every evidence item check out against the tool results in the trace, is anything misleading, and is the tone right for a procurement reviewer. Count a "policy failure" when the output would lead a reviewer to a wrong or unsafe action.

Reviewer: _name_ · Date: _yyyy-mm-dd_ · Model: _see summary.md_

## Single (A)

| Case | Run file | Recommendation sensible? | Evidence checks out? | Anything misleading? | Tone OK? | Policy failure? | Notes |
|---|---|---|---|---|---|---|---|
| G-02 | runs/single/G-02_t1.json | | | | | | |
| G-04 | runs/single/G-04_t1.json | | | | | | |
| G-06 | runs/single/G-06_t1.json | | | | | | |
| G-07 | runs/single/G-07_t1.json | | | | | | |
| G-08 | runs/single/G-08_t1.json | | | | | | |
| G-21 | runs/single/G-21_t1.json | | | | | | |

## Staged (B)

| Case | Run file | Recommendation sensible? | Evidence checks out? | Anything misleading? | Tone OK? | Policy failure? | Notes |
|---|---|---|---|---|---|---|---|
| G-02 | runs/staged/G-02_t1.json | | | | | | |
| G-04 | runs/staged/G-04_t1.json | | | | | | |
| G-06 | runs/staged/G-06_t1.json | | | | | | |
| G-07 | runs/staged/G-07_t1.json | | | | | | |
| G-08 | runs/staged/G-08_t1.json | | | | | | |
| G-21 | runs/staged/G-21_t1.json | | | | | | |

## Totals

| | Single (A) | Staged (B) |
|---|---:|---:|
| Policy failures found manually | | |
| Misleading outputs | | |
| Evidence items that did not check out | | |
