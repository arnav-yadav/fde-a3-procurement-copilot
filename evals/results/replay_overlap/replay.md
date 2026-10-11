# Guardrail replay on recorded LLM outputs

Runs: `evals/results/runs` (348 files; 252 LLM runs replayed through the current guardrails, the rest kept as stored). No LLM calls.

| Mean passes per trial | Rules only | Workflow + 1 LLM | Single (A) | Staged (B) |
|---|---:|---:|---:|---:|
| Main (of 23), stored | 21.00 | 22.00 | 23.00 | 20.67 |
| Main, replayed | 21.00 | 22.00 | 23.00 | 21.67 |
| Held-out (of 6), stored | 2.00 | 4.00 | 4.33 | 4.33 |
| Held-out, replayed | 2.00 | 4.67 | 4.33 | 4.33 |
| Safety gate, replayed | fail | pass | pass | fail |

Decision rule on the replayed runs: **Workflow + 1 LLM**.

## Runs whose result changed (5)

| Case | Configuration | Trial | Stored | Replayed |
|---|---|---|---|---|
| G-10 | Staged (B) | 2 | FAIL (flags: unexpected ['existing_tool_overlap']) | PASS |
| G-23 | Staged (B) | 1 | FAIL (flags: unexpected ['existing_tool_overlap']) | PASS |
| G-23 | Staged (B) | 3 | FAIL (flags: unexpected ['existing_tool_overlap']) | PASS |
| H-01 | Workflow + 1 LLM | 1 | FAIL (flags: unexpected ['existing_tool_overlap']) | PASS |
| H-01 | Workflow + 1 LLM | 3 | FAIL (flags: unexpected ['existing_tool_overlap']) | PASS |
