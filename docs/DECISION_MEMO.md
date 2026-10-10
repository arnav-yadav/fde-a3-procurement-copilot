# Architecture Decision Memo

## Decision

Between A (single agent) and B (analyst + reviewer), A wins, but we ship the rung below A: **workflow + one LLM call**. Code gathers the same evidence in a fixed order; one call interprets it. Our pre-registered rule (`IMPROVEMENTS.md` §3) chooses it: safe on every injection and fault case, within one case of the best on both sets, fewest LLM calls.

## Evidence

Run 3 (`evals/results/summary.md`, `decision.md`): frozen code, gemini-3.5-flash-lite, 3 trials, 23 main + 6 held-out cases (H-xx, fixed before round-2 code). AI-only: the six cases rules cannot pass by design.

| | Rules only | Workflow + 1 LLM | A: single | B: staged |
|---|---:|---:|---:|---:|
| Main, mean passes per trial (of 23) | 21.00 | 22.00 | 23.00 | 20.67 |
| Held-out, mean per trial (of 6) | 2.00 | 4.00 | 4.33 | 4.33 |
| AI-only, mean per trial (of 6) | 0.00 | 3.00 | 4.67 | 3.00 |
| All injection + fault cases, all trials | no (H-05) | yes | yes | no (G-21) |
| LLM calls / tokens per run | 0 / 0 | 0.96 / 3,832 | 3.25 / 11,291 | 3.00 / 10,136 |
| Active latency | 0.05 s | 3.0 s | 8.1 s | 8.3 s |
| Same decision in all trials | 23/23 | 23/23 | 23/23 | 20/23 |

## When multiple agents help here, and when they don't

Evidence gathering is fixed and mandatory: in all 84 LLM runs, A called the same five tools as the workflow. A fixed path is a workflow; the AI only interprets.

B's split (evidence vs policy review) is legitimate, and its reviewer caught errors: wrong PII on the negative control H-02 (every trial) and wrong "covers the need" claims (G-10, G-23). It also dropped correct PII on H-06: item by item, 7 helped, 2 hurt. Yet B was least consistent, failed the vendor-API-down case G-21 once by adding Privacy, and pays a second LLM stage per request.

## Trade-offs

A is the most accurate (23/23 main, every trial). Against the workflow it costs 3.4× the calls, 2.9× the tokens and 2.7× the latency, to gain G-08 and 1.67 AI-only passes per trial. The workflow sees the existing TaskFlow licence on G-08 but routes for approval anyway. Rules alone miss reusable tools, wording-implied PII and a paraphrased injection (H-05).

## Risks / limitations

- 29 cases, 3 trials, one free-tier model: a one-case gap is within noise.
- H-03 failed everywhere. When specialist reviews were also required, no configuration chose "use existing tool".
- 5 failed runs (workflow 2, B 3) added an overlap flag the policy did not raise.
- A verbatim quote can still be misread (G-10, G-21). By design this can only add a review, never remove one.
- Before production: make the workflow propose reuse when its own overlap check says "covers", and restrict AI-added overlap flags.

## Why this is the right MVP

AI interprets, code applies, humans decide. Code holds every threshold and trigger, and applies AI data-class findings only when they quote the request. What remains is one judgement per request, and one call makes it.
