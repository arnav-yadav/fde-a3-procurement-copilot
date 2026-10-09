# IMPROVEMENTS.md: Round 2 (after Classes 12 and 13)

**Deadline:** Sunday 11 Oct 2026, night (moved in Class 13). Confirm the exact cut-off on the form.
**Applies to:** this repo at commit `662430b`. Code-change numbering continues from C1 and C2 in `docs/architecture.md`.

## 1. Where the repo stands

The build is complete and healthy. Checked from a clean clone:
- `verify_setup.py` passes and 81/81 tests pass.
- The rules-only eval reproduces.
- No secrets appear in the git history.

Eval run 2 (gemini-3.5-flash-lite, 1 trial):

| | Single (A) | Staged (B) | Rules only |
|---|---:|---:|---:|
| Golden passed | 22/23 | 20/23 | 21/23 |
| AI-only cases (G-08, G-23) | 1/2 | 0/2 | 0/2 |
| Avg LLM calls | 3.00 | 5.00 | 0 |
| Avg tokens | 10,053 | 15,148 | 0 |

The problem is not the build. The current numbers cannot answer the question the instructor said the assessment is about:
> "When to use multiple agents and when to use a single agent: what the caveats are as per the problem statement, and why exactly you would choose what." (Class 13; answers will be checked individually)

The weaknesses:
- A beats rules-only by **one** case, in one trial. The README itself says that is within noise.
- B's extra cost is partly a bug, not the architecture (finding F3).
- The memo argues "simpler wins" without the vocabulary or tests Classes 12 and 13 gave us.

## 2. Findings from the existing runs (cite these; they are ours, not generic)

| # | Finding | Evidence |
|---|---|---|
| F1 | **The single agent never uses its freedom to choose a path.** In all 22 runs it called the same five tools; it varied only catalog keywords (7 runs). | `evals/results/runs/single/*_t1.json`, `tool_log` with `initiator == "agent"` |
| F2 | **Handoff loss in B (C13's handoff lesson).** In G-23 the analyst wrote `"customer PII (names, emails, phone numbers)"` in the free-text `data_classes_implied`; the reviewer ignored it and routed for plain approval. | `runs/staged/G-23_t1.json` → `proposals.analyst.data_classes_implied` |
| F3 | **B's cost is partly self-inflicted.** The analyst queries `get_vendor_status` twice (it guesses a vendor in its first parallel turn) and calls `lookup_policy_section` 1–3 times, although the reviewer already receives the policy text. That gives 5.0 LLM calls on average instead of the 3–4 expected. | `runs/staged/G-02..G-06_t1.json` tool logs |
| F4 | **A review step can also break correct work.** In G-08 the analyst correctly marked TaskFlow (SW003) as covering the need; the reviewer reversed it and lost the case. | `runs/staged/G-08_t1.json` |
| F5 | **LLM-added reviews are not grounded in the request.** Staged G-12 added Privacy because "the vendor processes personal data", which is a vendor attribute, not data this request uses. | `runs/staged/G-12_t1.json` |
| F6 | **The LLM finds the data class but applies the policy only partly.** Single G-23 added Privacy for customer PII but not Security, although §5 requires both. | `runs/single/G-23_t1.json` |

F2, F5 and F6 have one root cause: **the LLM proposes reviews directly, instead of reporting what it found and letting code apply the policy.** That breaks the brief's own design principle (AI interprets, code applies thresholds).

## 3. Pre-registered decision rule (commit this before any rerun)

Commit this section and the held-out set (C7) **before** implementing C3–C6. The git history then proves the rule and the cases came first.

1. **Safety gate**: a configuration is eligible only if it passes every injection case and every fault case in every trial.
2. **Quality**: score = mean golden passes across trials on the main set (23) and, separately, on the held-out set (6).
3. **Choice**: among eligible configurations, ship the one with the **fewest LLM calls per request** whose main-set mean is within 1 case of the best **and** whose held-out mean is within 1 case of the best.
4. **Tiebreaker**: the AI-only cases (G-08, G-23, H-01, H-03, H-05, H-06).
5. **Floor**: if no LLM configuration beats rules-only by at least 2 AI-only cases, the memo must say the LLM layer has not yet earned its cost.

## 4. Changes

### C3: The LLM reports implied data classes; code applies the policy *(priority 1)*
**Why:** F2, F5, F6.

**What:**
- Add to `AgentProposal` and `EvidencePack` (replacing the free-text `data_classes_implied`):
  ```python
  class ImpliedDataClass(BaseModel):
      data_class: Literal["customer_pii", "employee_pii", "source_code", "production_access",
                          "confidential_documents", "credentials"]
      quote: str   # verbatim from the request's own text
  implied_data_classes: list[ImpliedDataClass] = []
  ```
- Guardrails accept a class only if `quote` (whitespace-normalised, case-insensitive) is a substring of the **request's own text**: product name, justification, integrations. Vendor notes, catalog text and tool summaries don't count. Rejected classes produce the event `implied_class_ungrounded`.
- Re-run the policy engine's R7/R8/R11 with `declared classes ∪ accepted implied classes`. Code then adds Security/Privacy/Legal exactly as for declared data, including cross-region Legal. Reason text: `"AI: implied <class> (quote: '…')"`, plus the policy reference.
- **Remove the direct path** where the LLM adds Security/Privacy/Legal roles. A proposed specialist role without an accepted class is dropped (`llm_review_without_class`). It is shown to the reviewer under "Questions for the reviewer", so nothing is hidden.
- Prompts (all three LLM roles): "Report data classes only from the request's own words, with a verbatim quote. What the vendor can do (e.g. 'processes personal data') is not data this request uses."

**Done when:**
- Unit tests pass for:
  - an accepted quote,
  - an ungrounded quote,
  - a quote taken from vendor text, which must be rejected,
  - PII → both Security and Privacy,
  - PII with an out-of-region vendor → Legal.
- G-12-style vendor-attribute additions are dropped.
- Replaying the stored G-23 analyst pack through the new guardrail (after mapping its text to the enum) yields Security + Privacy.

### C4: Handoff v2 *(priority 1; C13: the safest handoff is structured evidence, gaps and unsupported claims)*
**Why:** F2 and F4.

**What:**
- `EvidencePack` gains `gaps: list[str]` and `unsupported_claims: list[str]`. These are statements in the request or vendor text that no tool result supports, such as "PixelCraft is too specialist", "all 180 seats are assigned" or "assessment complete".
- The reviewer proposal gains `analyst_disagreements: list[{item, analyst_said, reviewer_says, reason}]`. The reviewer prompt requires an explicit agree or disagree for every overlap entry and every implied data class in the pack.
- The eval counts reviewer disagreements and whether each one **helped or hurt** against golden. For example, G-08 in run 2 hurt.

**Done when:** the trace shows the disagreements and `summary.md` has a row "Reviewer overrides of analyst: helped / hurt".

### C5: Scope B's tools per agent *(priority 1; C13: each agent gets only the tools it needs)*
**Why:** F3. Without this fix, "B costs 67% more" is partly our bug, and the comparison is unfair to B.

**What:**
- The orchestrator calls `get_request_details` in code and puts the result in the analyst's first message. The analyst can no longer guess a vendor.
- Analyst tools = `check_budget`, `search_software_catalog`, `get_vendor_status`. Remove `lookup_policy_section`, because the reviewer gets the policy text.
- Name the repositories in `tools.py`:
  - `COMMON_TOOLS`,
  - `EVIDENCE_TOOLS`,
  - `POLICY_TOOLS`,
  - plus a per-agent list (A = all 6, B1 = 3, B2 = 0, workflow LLM = 0).
- Record `tools_exposed` (count and the token size of the schemas) per LLM call.

**Done when:** B averages 3 or fewer LLM calls on a 5-case smoke run, with no duplicate `get_vendor_status` calls. State the change in the README so readers know run-2 B costs are pre-fix.

### C6: Fourth configuration, "workflow + one LLM call" *(priority 1; C12: what is the simplest useful design?)*
**Why:** F1. The agent always runs the same path, which is C12's definition of a case where a workflow suffices. We need to measure the rung between rules-only and the agent.

**What:**
- New `src/agents/workflow_llm.py`. Code runs `get_request_details → check_budget → search_software_catalog → get_vendor_status → evaluate_policy_rules` in that fixed order.
- Then exactly **one** LLM call: no tools, a forced `submit_recommendation`, and the same input as the B reviewer minus the analyst pack.
- Reuse the reviewer path with a flag. Same guardrails (including C3).
- Reachable only via `handle_request_with_trace(..., architecture="workflow")` and `run_all.py --architectures`. **Do not** change `Architecture` in `contracts.py`.

**Done when:** `scripts/run_one.py REQ-1008 --arch workflow` makes exactly 1 LLM call, and `summary.md` has a "Workflow + 1 LLM" column.

### C7: Held-out set *(priority 1; commit first, unchanged)*
**Why:** every C-change so far was motivated by the 23 golden cases. Re-scoring only on them would be tuning to the test set.

**What:**
- Add `evals/golden_heldout.json` (6 cases, provided in this change pack).
- Add 6 requests (`REQ-H201`…`REQ-H206`) to `evals/fixtures/requests.json`. The provided file is the existing 11 plus these 6.
- `run_all.py` gains `--golden-set main|heldout|all` (default `all`) and reports the two sets in **separate** columns or rows. Never merge them into one number.
- Labels were derived from the policy and checked against a rules-only replica. Rules alone should pass H-02 and H-04 and fail the rest: `requires_ai` marks H-01, H-03, H-05 and H-06.

| Case | Tests |
|---|---|
| H-01 | Employee PII implied only by text (salary bands, performance ratings) |
| H-02 | **Negative control**: the vendor processes PII but the request does not. Adding Privacy is a failure (F5 mode). |
| H-03 | An existing tool solves the need (DocSpace) for an unregistered vendor (Notely) |
| H-04 | A seat expansion is *not* a substitute; "all seats assigned" is an unverified claim |
| H-05 | A paraphrased injection that none of the 9 regex patterns catch. Only the LLM can flag it; approvals must not change |
| H-06 | Customer data implied by an integration missing from the keyword map (Snowflake customer warehouse) |

**Done when:** `python evals/run_all.py --no-llm --golden-set heldout` shows rules-only at 2/6. Do not edit these labels after seeing LLM results. If one looks wrong, write that in the memo instead.

### C8: Trials and quota plan *(priority 2)*
- **Final run: 3 trials** of rules_only, workflow, single and staged on main + held-out, using `--resume`. Report decision consistency across trials.
- **Budget**: about 200 LLM calls per trial without the public runner (22×(3+3+1) + 6×(3+3+1) = 196 after C5; 3 trials ≈ 600, so plan on two days of quota). Run the public runner once at the end, not per trial.
- Split the runs across Saturday and Sunday on **one** project key. Do not switch to a second project's key mid-run: it reads as working around a provider limit, and "same settings" becomes harder to defend. Remove that sentence from the README.
- **Optional replication**: one trial of workflow + single on Groq `openai/gpt-oss-120b` (1,000 requests/day; slow at 8,000 tokens/min, so run it overnight), reported as a separate table. It shows whether the ranking depends on the model.

### C9: Tracing you can see *(priority 2; C13: who ran, with what input, what output, and why)*
- Add `trace.steps[]`. Each step records:
  - `step_no`,
  - `actor` (`orchestrator | agent:A | agent:B1 | agent:B2 | llm:workflow | gate | guardrails`),
  - `why` (e.g. "fixed sequence: reviewer follows analyst", "gate: agent skipped check_budget", "C3: implied class accepted"),
  - `input_ref`, `output_ref`, `tools_called`, `llm_calls`, `tokens`, `duration_ms`.
- Streamlit: add a collapsed **"How this was decided"** expander that lists the steps in plain language, followed by the guardrail corrections. Render all text through `esc()`.
- **Optional LangSmith**: add `langsmith` to requirements. If `LANGSMITH_TRACING=true` and `LANGSMITH_API_KEY` are set, wrap the client with `langsmith.wrappers.wrap_openai`, and decorate the agent stages, tools and `guardrails.assemble` with `@traceable`. Otherwise it is a no-op.
  - Verified: `wrap_openai` works on a plain `openai.OpenAI` client pointed at Groq's base URL.
  - Add one screenshot to the README if you enable it.

### C10: Housekeeping *(priority 3)*
- `run_all.py --no-llm` rewrites committed rules-only JSON files with new run IDs and latencies, which dirties the tree on every check. Either make those fields deterministic for rules-only, or write that run to a scratch directory unless `--save` is passed.
- `evals/results/manual_review.md` is marked "AI-assisted pre-fill, pending human confirmation". **A human must actually do the review** before the README and memo quote "policy failures found manually". Otherwise remove that row.
- Keep run history as is (`history/run1`). Archive run 2 to `history/run2` before run 3.

## 5. Documentation to rewrite after run 3

**`docs/architecture.md`: add a section "Design rationale (Classes 12 and 13)".**
1. **C12 ladder**: single LLM call → chatbot → workflow → agent. Say where this problem sits: evidence gathering is a known, mandatory process (F1), so it is a workflow. The AI is for interpretation only. A chatbot is rejected because there is no multi-turn need and clarifications are asynchronous.
2. **C13 multi-agent test**: are there separable specialist responsibilities and review steps? Yes (evidence vs policy/risk review). That makes B legitimate, not "a demo that looks advanced".
3. **Sequential vs supervisor–worker**: a supervisor's benefit is skipping workers that aren't needed. The policy forbids skipping checks, so a supervisor would only add a routing call and a misrouting failure mode.
4. **Handoff** (C4), **tool scoping** (C5), and the **fixed-path cost** (B pays analyst + reviewer even for an $800 add-on, G-01).
5. **Caveats that would change the decision**:
   - free-text or email intake,
   - many more evidence sources (contracts, SSO usage, invoices), where choosing what to retrieve becomes a real decision,
   - a reviewer that measurably fixes more than it breaks (C4's helped/hurt row),
   - volume high enough that per-request cost dominates.
6. Add the C3–C10 rows to the existing "Code changes driven by evaluation" table, with evidence links.

**`docs/DECISION_MEMO.md`: rewrite to ≤ 500 words, in your own voice.** Suggested budget:

| Section | Words | Must contain |
|---|---:|---|
| Decision | 40 | The configuration chosen by the §3 rule, stated plainly |
| Evidence | table | 4 configurations × main pass (mean of 3), held-out pass, AI-only cases, injection/fault, LLM calls, tokens, active latency, consistency |
| When multiple agents help here, and when they don't | 150 | The instructor's question, answered with F1, F2, F4 and the C4 helped/hurt count. Name at least two case IDs. |
| Trade-offs | 120 | What the extra rung buys and costs, in numbers |
| Risks / limitations | 90 | Small set, free-tier model, caveats from §5 point 5 |
| Why this is the right MVP | 60 | Tie back to "AI interprets, code applies, humans decide" |

**`README.md`**: update sections 4 (add the ladder diagram), 7 (main + held-out, 3 trials, run history), 8 (four columns, plus the reviewer helped/hurt row), 9 and 10. Remove the second-project-key sentence.

## 6. Do not change
- The 23 main golden labels. They were fixed before any run.
- `src/contracts.py` and the `handle_request` signature.
- C1 and C2. Both are evidence-backed and documented.
- The Streamlit UI choice (already a documented deviation). Only add the C9 expander.

## 7. Schedule (IST)
| When | Work |
|---|---|
| **Fri night** | Commit §3 + C7 first (`"pre-register decision rule + held-out set"`). Then C3 + C5 with their tests. |
| **Sat morning** | C4, C6, C9 (including the expander). 5-case smoke runs per configuration. |
| **Sat afternoon** | Trial 1 (all 4 configurations, main + held-out). Read the failures but **do not tune prompts against held-out cases**. |
| **Sat night → Sun noon** | Trials 2 and 3 with `--resume` (daily cap), then the public runner once. Optional Groq replication overnight. |
| **Sun 12:00–16:00** | Apply the §3 rule; update architecture.md, README and memo |
| **Sun 16:00** | Code freeze |
| **Sun 16:00–21:00** | Proofread in your own voice, check every number against `summary.md`, final push, submit |

## 8. Prompt for Claude Code
```
Read IMPROVEMENTS.md fully. Work in this order and stop after each step with a diff summary and test output:
1. Commit IMPROVEMENTS.md, evals/golden_heldout.json and the updated evals/fixtures/requests.json unchanged,
   message "pre-register decision rule + held-out set". Do not edit them.
2. C3 (with tests), then C5. 3. C4. 4. C6. 5. C7 runner support (--golden-set). 6. C9. 7. C10.
After each change: python -m unittest discover -s tests, and python evals/run_all.py --no-llm --golden-set all.
Do not run LLM trials until I say so. Never edit golden labels. Record every deviation in docs/architecture.md.
```
