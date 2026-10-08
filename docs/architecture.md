# Architecture

(Full write-up in Phase 7.)

## Prompt changes

Prompts are stored verbatim from SPEC_TECHNICAL T16 in `src/prompts.py`. Each change below is driven by an observed run.

| # | Date | Prompt | Change | Evidence |
|---|---|---|---|---|
| 1 | 2026-10-08 | SYSTEM_SINGLE, SYSTEM_ANALYST | Added: "The policy digest below is usually enough: call lookup_policy_section only when you need the exact wording to resolve a specific doubt." | Phase 3 manual runs (REQ-1006, 1008, 1009): the agent called `lookup_policy_section` 2-5 times per run, adding a full-context LLM round (4 calls instead of the expected 3, about +5k tokens). Free-tier token limits (Groq 8,000 TPM) make this round the main cost driver. |

## Code changes driven by evaluation

| # | Date | Change | Evidence |
|---|---|---|---|
| C1 | 2026-10-08 | R12 step 2 (deviation from SPEC_TECHNICAL R12): `use_existing_tool` now requires the agent's own `decision_type` to be `use_existing_tool` **and** a flagged candidate with `covers_stated_need=true`. Previously code derived `use_existing_tool` from `covers_stated_need` alone. | Eval run 1 (`evals/results/history/run1_summary.md`): in all 9 `use_existing_tool` failures (single G-01, G-11, G-16, G-21; staged G-01, G-03, G-11, G-14, G-21) the agent's own decision was correct (route for approval/specialist review) but its overlap entry for a seat/add-on expansion said `covers_stated_need=true`; R12 overrode the correct decision. In G-08, the only case where reuse is right, the agent chose `use_existing_tool` itself. A prompt change was considered and rejected: tightening the definition ("an upgraded tier is not covered") risked flipping G-08 (TaskFlow Pro vs TaskFlow), and the evidence showed the model's decisions were already right. |
