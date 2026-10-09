# Architecture Decision Memo

## Decision

Ship **Architecture A, the single agent**. It passed more golden cases with fewer LLM calls and lower latency. The staged variant did not earn its extra cost.

## Evidence

Same 23 golden cases, one trial, gemini-3.5-flash-lite, temperature 0 (`evals/results/history/run2/summary.md`, `manual_review.md`).

| Metric | Single | Staged | Rules only |
|---|---:|---:|---:|
| Golden cases passed | 22/23 | 20/23 | 21/23 |
| AI-only cases (G-08, G-23) | 1/2 | 0/2 | 0/2 |
| Injection / fault cases | 3/3, 2/2 | 3/3, 2/2 | 3/3, 2/2 |
| Avg active latency | 6,720 ms | 9,997 ms | 54 ms |
| Avg latency incl. rate-limit waits | 18,521 ms | 30,497 ms | 54 ms |
| Avg LLM / tool calls | 3.00 / 5.35 | 5.00 / 7.04 | 0 / 5.00 |
| Ungrounded evidence removed | 3 | 0 | n/a |
| Policy failures found manually (6 runs) | 1 | 0 | n/a |

## Trade-offs

The single agent correctly suggested the existing TaskFlow licence (G-08), which rules cannot do. Neither agent fully caught customer PII implied only by free text (G-23): single added Privacy but not Security.

Staged improved evidence quality, with nothing removed for grounding and nothing misleading in the manual sample, which supports its hypothesis. But it decided worse. It missed G-08, and it added Privacy because the vendor "processes personal data" when the request involved none (G-12; G-13 in run 1). It cost 67% more LLM calls and 49% more active latency.

The largest gain came from code. In run 1 (`history/run1_summary.md`: single 18/23, staged 16/23), rule R12 overrode correct agent decisions when an overlap entry was inconsistent. Requiring the agent's own decision to agree (change C1) fixed every such failure in both architectures.

Deterministic code carried safety in every column. Every architecture passed all injection and fault cases, and guardrails never had to restore a dropped approval or flag.

## Risks / limitations

- 23 cases and one trial: one or two cases of difference is within noise. The free tier's 500 requests/day prevented multi-trial runs.
- Grounding checks numbers, not meaning. In G-08 the single agent wrote "$7,000 remaining… insufficient": every number exists in the tool results, but the claim is wrong. A budget-status cross-check (C2) now removes it; extend that to vendor and data-class claims, and run several trials on a paid tier, before production.
- Free-text data classes (G-23) remain a gap. A deterministic PII keyword scan of justifications would close it cheaply.

## Why this is the right MVP

The client's risk sits in deterministic checks, which code handles identically for every architecture. The LLM's job is narrow: overlap judgement, intent in free text and clear wording. One agent with six tools does it better here, at 3 LLM calls per request. A second agent added cost and lowered the pass rate. Simpler wins.
