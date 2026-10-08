# PLAN.md: Build plan (spec-driven, phase by phase)

**Deadline:** submission closes 8 Oct 2026, end of day. **Submit by 23:15 IST.**

## Clock schedule (IST; times include the human review at each STOP)
| Time | Phase | Milestone |
|---|---|---|
| 15:00–15:30 | P0 Setup + scaffold fixes | |
| 15:30–16:40 | P1 Deterministic core | |
| 16:40–17:20 | P2 Guardrails + fallback + adapter | **Safety net**: valid rules-only product. Push to GitHub now. |
| 17:20–18:30 | P3 LLM client + Architecture A | |
| 18:30–19:10 | P4 Architecture B | |
| 19:10–20:00 | P5 Evaluation harness | Start the 3-trial run in the background |
| 20:00–21:20 | P6 Web app | |
| 21:20–22:15 | P7 Docs + clean-environment test (Claude Code drafts) | |
| **22:15** | **CODE FREEZE** | No new features after this; only fixes for failing checks |
| 22:15–23:15 | P8 Proofread, PDF (if needed), submit (human) | **Form submitted by 23:15** |
| 23:15–24:00 | Buffer (45 min) | |

If you start later than 15:00, shift every row by the same amount and apply the cut lines at the bottom earlier. **Never move the code freeze past 22:30.**
**Rule:** each phase ends with checks plus a STOP. The human reviews the diff and the outputs, then says "continue".
Specs: `docs/SPEC_FUNCTIONAL.md` (F) and `docs/SPEC_TECHNICAL.md` (T). Rules: `CLAUDE.md`.

## Kickoff prompt (paste into Claude Code first)
```
Read CLAUDE.md, then docs/SPEC_FUNCTIONAL.md, docs/SPEC_TECHNICAL.md and PLAN.md.
Before writing any code:
1. Inspect the repository and summarise the current architecture in 10 lines.
2. List anything in the specs that conflicts with the code or the policy, and any spec item you find ambiguous.
3. Confirm the Phase 0 task list back to me.
Do not start Phase 0 until I reply "go".
```
After each phase, reply "continue with Phase N" or give corrections.

---

## Phase 0: Setup and scaffold fixes (≈30 min)
**Tasks**
- `git init`. Create `.venv`. Update `.gitignore`: add `runtime/` and `.env`; remove `evals/results_*.csv` (T2-F2).
- T2 fixes F1, F4 (basic version), F5, F7.
  - `run_local.py` starts the mock API and a placeholder FastAPI app that returns "ok" at `/`. The full UI comes in Phase 6.
- Delete the Streamlit `app.py`. In `requirements.txt` add `openai`, remove `streamlit`. Update `verify_setup.py` to match.
- `src/config.py` (T3): settings read at call time; `REFERENCE_DATE` and `POLICY_VERSION` parsed from the policy file.
- Write `.env.example` exactly as in T3.
- `scripts/smoke_llm.py`:
  - Print the provider, base_url and model.
  - List the available models (`client.models.list()`), and warn if `MODEL_NAME` is not among them.
  - Make one chat call that offers a dummy tool and **forces** it (T7). Print the parsed tool-call arguments, latency and usage.
  - Exit non-zero on failure, with a clear message.

**Checks:** `python verify_setup.py` → PRE-FLIGHT PASSED. The starter tests pass. `python run_local.py` starts both services.
**Human:** put the key in `.env` and run `python scripts/smoke_llm.py`. If the default model ID is wrong, set `MODEL_NAME`.
**STOP.**

## Phase 1: Deterministic core (≈75 min)
**Tasks**
- `src/normalize.py` (T4.2).
- `src/data_access.py` additions (T4.1): `EXTRA_DATA_DIR`, runtime submissions, `norm_key`, `find_vendor`, `find_employee`.
- `src/injection.py` (R10).
- Hardened `src/vendor_client.py` (T6.4).
- `src/schemas.py`: PolicyResult, VendorStatus and the T8 models.
- `src/policy_engine.py`: R0–R12 exactly as written.
- `src/tools.py`: the 6 tools, RunContext, cache, tool log and evidence corpus (T6). Agent-facing JSON schemas included.
- Tests: `test_policy_engine.py`, `test_injection.py`, `test_vendor_tool.py` (T15).
- `tests/test_golden_deterministic.py`: for **all 23** golden cases (fixtures loaded via `EXTRA_DATA_DIR`, faults simulated), assert:
  - engine approvals ⊇ golden `approvals.required` and ⊆ `required ∪ optional`,
  - the same for flags,
  - missing-information groups and `max_items`.

  Skip only:
  - aspects listed in the case's `requires_ai` (G-08 decision; G-23 approvals, flags and decision). These are the cases where only the AI can be right.
  - `llm_unavailable` flag expectations.

**Checks:** all tests pass. The T5 expected-output table is reproduced exactly.
**Human:** skim the engine's evidence strings for REQ-1007 (the conflict) and REQ-1009 (unavailable).
**STOP.**

## Phase 2: Guardrails, fallback and adapter (≈45 min) → milestone: safety net
**Tasks**
- `src/guardrails.py` (T11, including the output filter, grounding check and templates T11.1).
- `src/fallback.py` (T12).
- Trace and telemetry (T13).
- `src/solution.py`:
  - `handle_request_with_trace(request_id, architecture, mode)`.
  - `handle_request` calls it.
  - Until agents exist, both architectures use the fallback.
- Tests: `test_guardrails.py`, `test_solution_rules_only.py`.
- Commit: `"deterministic baseline: rules-only copilot"`.

**Checks:** with `LLM_SIMULATE_OUTAGE=1`, `python evals/run_public_evals.py --architecture single` → **6/6**, and the same for staged.
**STOP.** (A valid, honest, AI-less baseline now exists. Everything after this point improves it.)

## Phase 3: LLM client and Architecture A (≈75 min)
**Tasks**
- `src/llm_client.py` (T7): throttle, retries, wait accounting, simulate-outage hook.
- `src/prompts.py`: copy the T16 text verbatim.
- `src/agents/single_agent.py` (T9) with the completeness gate (T6.3). Wire it into `solution.py`.
- Debug script `scripts/run_one.py <request_id> --arch single`. It prints the final decision, the raw proposal and the guardrail events.

**Checks**
- Run REQ-1001, 1006, 1008 and 1009 through `scripts/run_one.py`. Expected:
  - 1006 → `request_clarification` plus the injection flag, with no CFO.
  - 1008 → `use_existing_tool`.
  - 1009 → unavailable is surfaced.
- Public runner for single → 6/6.

**Human:** read the 4 outputs and judge whether the recommendation and next step are useful to a reviewer. Only tune prompts if something concrete is wrong, and log each prompt change in `docs/architecture.md` under "Prompt changes".
**STOP.**

## Phase 4: Architecture B, staged (≈45 min)
**Tasks**
- `src/agents/staged_agent.py` (T10): analyst + gate + policy engine + reviewer.
- Wire it into `solution.py`.

**Checks:** the same 4 requests through `scripts/run_one.py --arch staged`. Public runner for staged → 6/6.
**STOP.**

## Phase 5: Evaluation harness (≈60 min)
**Tasks**
- `evals/run_all.py` and `evals/score.py` (T14), including:
  - the rules-only column,
  - the per-case matrix,
  - `summary.md` / `summary.json`.
- Update `evals/README.md`.
- Add the `evals/results/manual_review.md` template: 5 runs per architecture, with columns for recommendation sensible / evidence checks out / misleading anything / tone.

**Run order:**
1. `python evals/run_all.py --no-llm`
2. `python evals/run_all.py --trials 1`

Commit `evals/results/`.

**Human:** read `summary.md` and every failure line. If free-tier quota allows, start `--trials 3` in a second terminal now; it can run while Phase 6 is built. Fill in `manual_review.md` while it runs.
**STOP.**

## Phase 6: Web app (≈90 min)
**Tasks**
- `webapp/main.py` (API in T17).
- `webapp/static/*` (UI in T17): queue, request card, run bar, banners, recommendation, approvals with reasons, flags, missing / could-not-verify, evidence table, footer, action bar, confirmation modal, success state, audit history.
- Empty, loading and error states.
- Add "New request" form (US-6) only if Phases 0–5 are done by 20:30.
- Add "Compare both" (US-7) only if everything else is done.

**Checks (manual, in the browser):**
1. REQ-1006 → injection banner, clarification; the override reason is required if you choose "Send for approvals".
2. REQ-1007 → conflict shown side by side.
3. REQ-1008 → "Suggest existing tool" is highlighted.
4. Stop the mock API (Ctrl+C on that process, or start with a wrong `VENDOR_RISK_BASE_URL`), run REQ-1001 → unavailable banner, no favorable status.
5. Confirm an action → an audit log line is written.

Check XSS: submit a request whose justification is `<img src=x onerror=alert(1)>`. It must render as plain text.
**STOP.**

## Phase 7: Docs and clean-environment test (≈55 min; Claude Code drafts, you review in Phase 8)
**Tasks**
- `docs/architecture.md`:
  - both T18 diagrams,
  - a tools table (deterministic / external / authoritative),
  - the AI vs code vs human split,
  - handoff format (EvidencePack → Reviewer),
  - stop and escalation conditions,
  - what we deliberately did not build,
  - prompt changes.
- `README.md` with exactly these sections:
  1. What it does (3 lines)
  2. Setup and run (one command)
  3. Product workflow
  4. Architecture (diagram, A vs B)
  5. Tools and agents
  6. Assumptions (copy F §8)
  7. Evaluation (method + `summary.md` table + how to reproduce)
  8. Architecture comparison
  9. Ship decision
  10. Known limitations (include F §9 open questions, free-tier rate limits, small eval set, no real integrations)
- `docs/DECISION_MEMO.md`: **≤ 500 words**, following the template in `templates/architecture_decision.md`. Use only numbers from `evals/results/`.
  - Decide from the evidence. If B is not clearly better on quality, ship A, because simpler wins.
  - State the result honestly even if it is the opposite.
  - Print the word count.
- **Clean-environment test**:
  - fresh clone into a temp dir → new venv → `pip install -r requirements.txt` → `python verify_setup.py`,
  - then `python -m unittest discover -s tests`,
  - then `python evals/run_all.py --no-llm --skip-public`,
  - then `python run_local.py` and load the page.
- **Secret scan**: `git log -p | grep -iE "api[_-]?key|AIza|gsk_"` must find nothing beyond `.env.example` placeholders. Confirm `.env` is untracked.
- Push to a **public** GitHub repository.

**STOP. Code freeze at 22:15.**

## Phase 8: Proofread, PDF, submit (human; 22:15–23:15)
| Time | Task |
|---|---|
| 22:15–22:50 | **Proofread** `README.md` and `docs/DECISION_MEMO.md` in your own voice. Check every number in both against `evals/results/summary.md`. Re-count the memo words (≤ 500). Make sure the README has all 10 sections the brief asks for. |
| 22:50–23:00 | **PDF, only if the Google Form asks for a file upload.** The brief itself only requires the public repo URL; the memo lives in the repo. If a PDF is needed, generate a plain one from `DECISION_MEMO.md`: simple tables, no decoration, ≤ 2 pages. |
| 23:00–23:10 | **Final push + check.** `git status` is clean, `.env` is untracked, the last commit is on GitHub, the repo is **public** (open it in a private/incognito window), the Mermaid diagrams render, and the README run steps are correct. |
| 23:10–23:15 | **Submit** the repo URL on the Google Form. Screenshot the confirmation. |

Tip: open the Google Form at the safety-net milestone (17:20) to see exactly what it asks for. Then you'll know whether a PDF or anything else is needed, instead of finding out at 23:00.

---

## Cut lines if behind schedule (clock-triggered; apply without debate)
| If by... | ...this isn't done | Then drop |
|---|---|---|
| 17:45 | P2 safety net | Compare view (US-7), new-request form (US-6), B's reviewer repair attempt |
| 20:30 | P5 eval harness | `--trials 3` (use 1 trial; say so in the limitations) |
| 21:45 | P6 web app | All UI polish. Keep only: queue, analysis result, action + confirmation modal, audit log |
| 22:15 | anything | Stop building. Write up what exists honestly under "Known limitations" |

**Never cut:** the guardrails, the fallback, the golden eval on both architectures, the one-command start, the README sections, or the memo.

## Definition of done (maps to the brief's scoring)
| Brief item | Evidence in repo |
|---|---|
| Product + workflow (15%) | Web UI flow queue → analysis → confirm → audit log; README workflow section |
| End-to-end product (20%) | `python run_local.py`; clean-environment test passed |
| Agent + tool design (20%) | 6 tools (5 deterministic, 1 external), A and B agents, T5 rules, `docs/architecture.md` |
| Reliability + human controls (15%) | guardrails, fallback, gate, injection handling, approval UX with reason, audit log, fault cases |
| Evaluation + comparison (20%) | `evals/run_all.py`, golden 23 cases (2 need the AI by design), rules-only baseline, raw vs final metrics, `summary.md` |
| Engineering + communication (10%) | tests, README, ≤ 500-word memo, no secrets |
