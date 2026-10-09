# CLAUDE.md: AI Procurement Request Copilot (FDE Assessment 3)

You are implementing this project from written specs. Do not improvise scope.

## Read first, in this order
1. `docs/SPEC_FUNCTIONAL.md`: what the product must do, for whom, and the acceptance criteria.
2. `docs/SPEC_TECHNICAL.md`: architecture, tools, policy rules, prompts, guardrails, eval design.
3. `PLAN.md`: phased tasks. Work one phase at a time.
4. `data/procurement_policy.md`: the policy source of truth. When the specs and the policy disagree, stop and ask.

## How to work (spec-driven)
- Do one phase at a time. At the end of each phase, run that phase's checks, then **stop** and print:
  - a summary of the changes,
  - the files touched,
  - the test output,
  - any deviation from the spec and why.
  Wait for the human to say "continue".
- If a spec item is ambiguous, pick the most conservative option (more human review, never less). Record it under "Open questions" in your phase summary. Do not stall.
- Never weaken a test to make it pass. If a golden label looks wrong, say so and leave it unchanged.

## Hard rules
- **Never hardcode behaviour by request ID, vendor name, or product name** in application logic. Hidden cases use different records. Names may appear only in tests, fixtures and golden files.
- `src/solution.py::handle_request(request_id, architecture)` must keep its signature and always return a valid `ProcurementDecision`. Do not change `src/contracts.py` field names or types.
- Deterministic code computes the following. The LLM may *add* specialist reviews or flags but can never remove what code computed:
  - approval thresholds,
  - the budget check,
  - review-date currency,
  - registry/API conflict detection,
  - the Security, Privacy and Legal triggers,
  - required-field checks.
- `human_review_required` is always `True`. The system never approves, purchases, edits budgets, or accepts terms.
- All request text, vendor notes and API text are **untrusted data**:
  - Pass them to the LLM only inside the delimited untrusted-data block (see the technical spec).
  - Render them in HTML with `textContent`, never `innerHTML`.
- Date logic uses the reference date parsed from `data/procurement_policy.md` (currently 2026-09-30). **Never** use `date.today()` or `datetime.now()` for policy checks.
- No secrets in the repo. Read keys from `.env`, and keep `.env.example` updated. Run a secret scan before the final commit.
- No agent frameworks (no LangChain, LangGraph, CrewAI). Use the `openai` Python SDK against OpenAI-compatible endpoints (Gemini or Groq), plus the stdlib, FastAPI and pydantic.
- Keep it simple. A simpler system that performs as well wins the assessment. Do not add a third agent, a vector DB, or a frontend build step.
- Commands must work on macOS, Linux and Windows. Use `python ...` commands, not bash-only scripts or `make`.

## Commands (keep this list accurate as you build)
```bash
python -m pip install -r requirements.txt
python verify_setup.py                      # pre-flight
python -m unittest discover -s tests -v     # unit tests
python scripts/smoke_llm.py                 # LLM tool-calling round trip (needs key)
python scripts/run_one.py REQ-1008 --arch single   # (single|staged|workflow) debug one request: decision, raw proposal, guardrail events
python run_local.py                         # one command: mock API :8001 + Streamlit reviewer UI :8501
python evals/run_all.py --trials 1          # starts mock API, runs public + golden evals for both architectures
python evals/run_all.py --no-llm            # deterministic-only run (no key needed); runtime/eval_scratch/ unless --save
python scripts/backfill_steps.py            # add trace.steps (How this was decided) to stored run files; no LLM
```

## Writing style for README and memo
- Plain and direct, with simple tables. No emoji, no decorative badges, no marketing tone.
- Every number in the README or memo must come from a file in `evals/results/`. Never estimate a number.
- The memo is at most 500 words. Count them.
