# Architecture

(Full write-up in Phase 7.)

## Prompt changes

Prompts are stored verbatim from SPEC_TECHNICAL T16 in `src/prompts.py`. Each change below is driven by an observed run.

| # | Date | Prompt | Change | Evidence |
|---|---|---|---|---|
| 1 | 2026-10-08 | SYSTEM_SINGLE, SYSTEM_ANALYST | Added: "The policy digest below is usually enough: call lookup_policy_section only when you need the exact wording to resolve a specific doubt." | Phase 3 manual runs (REQ-1006, 1008, 1009): the agent called `lookup_policy_section` 2-5 times per run, adding a full-context LLM round (4 calls instead of the expected 3, about +5k tokens). Free-tier token limits (Groq 8,000 TPM) make this round the main cost driver. |
