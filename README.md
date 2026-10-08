# AI Procurement Request Copilot (FDE Assessment 3)

## 1. What it does

A reviewer opens a software purchase request; the copilot gathers evidence (budget, existing catalog, vendor registry and vendor-risk service, policy) and recommends the next action with the approvals required, risk flags, missing information and a cited evidence trail.
Deterministic code computes every threshold, budget check, review date, conflict and Security/Privacy/Legal trigger; the LLM judges overlap with existing tools, reads intent from free text and writes the recommendation.
A human confirms every action (with a reason when overriding the copilot), and each decision is written to an audit log. Nothing is ever approved or purchased automatically.

## 2. Setup and run

Requires Python 3.11 or 3.12 (tested on 3.12.15, macOS).

```bash
python -m venv .venv
source .venv/bin/activate            # Windows: .\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
cp .env.example .env                 # Windows: Copy-Item .env.example .env ; then add GEMINI_API_KEY (or LLM_PROVIDER=groq + GROQ_API_KEY)
python verify_setup.py               # pre-flight: PRE-FLIGHT PASSED
python run_local.py                  # ONE command: mock vendor-risk API :8001 + web app http://127.0.0.1:8000
```

Without an API key the app still works: every request gets the rule-based result with an "AI analysis unavailable" banner.

Other commands:

```bash
python -m unittest discover -s tests -v          # unit tests (no network)
python scripts/smoke_llm.py                      # provider/model check + forced tool-call round trip
python scripts/run_one.py REQ-1008 --arch single # one request: decision, raw proposal, guardrail events
python evals/run_all.py --trials 1               # public runner + 23 golden cases, both architectures + rules-only
python evals/run_all.py --no-llm                 # rules-only baseline (no key needed)
```

## 3. Product workflow

```
Employee submits request (form, or data/requests.json)
  -> Reviewer opens it from the queue (status: New / Analysed / Action recorded)
  -> Copilot gathers evidence and runs deterministic checks
  -> Reviewer sees: recommendation + next step, banners for anything unverified/conflicting/injected,
     approvals (click a role for the rule and policy section), risk flags, missing / could-not-verify,
     evidence table (source, finding, reference, Rule/Tool/AI), cost footer
  -> Reviewer chooses: Send for approvals | Request clarification | Suggest existing tool | Hold for manual review
     The confirmation states the consequence; a reason is required when overriding the copilot
  -> runtime/audit_log.jsonl: time, request, run ID, architecture, copilot decision, action, override, reason
```

Routing is simulated: actions are recorded in the audit log only.

## 4. Architecture

```mermaid
flowchart LR
  E[Employee request] --> Q[Review queue]
  Q --> H[handle_request]
  H -->|single| A1[Procurement Agent<br/>LLM + 6 tools]
  H -->|staged| B1[Analyst agent<br/>LLM + 4 evidence tools]
  B1 --> PE[Policy engine<br/>deterministic code]
  PE --> B2[Policy & Risk Reviewer<br/>LLM, no tools]
  A1 --> G[Guardrails + assembler<br/>code]
  B2 --> G
  G --> D[ProcurementDecision]
  D --> R[Reviewer: evidence, approvals, flags]
  R --> C[Confirm action + reason]
  C --> L[(audit_log.jsonl)]
```

| | A: single agent | B: staged (2 agents) |
|---|---|---|
| LLM roles | one agent with all 6 tools | Analyst (evidence tools + policy text) → Policy & Risk Reviewer (no tools) |
| Policy engine | a tool the agent calls (code runs it if skipped) | called by code between the stages |
| Handoff | none | `EvidencePack` + the raw tool results + the policy-engine result |
| Idea being tested | simplest design | an independent reviewer that checks the analyst against raw tool results improves grounding and judgement |

Both end in the same code: completeness gate → policy engine → guardrails → `ProcurementDecision` (`src/solution.py::handle_request`). Any LLM failure falls back to the rule-based result with the flag `llm_unavailable`. More detail, including every deviation from the spec: [`docs/architecture.md`](docs/architecture.md).

## 5. Tools and agents

| Tool | Type | What it returns |
|---|---|---|
| `get_request_details` | deterministic | normalised request, requester and reporting line, required-field check, injection scan |
| `check_budget` | deterministic | cost vs the department's available software budget |
| `search_software_catalog` | deterministic | existing licensed products in the same category/vendor/product, purchase history, which raise the overlap flag |
| `get_vendor_status` | deterministic logic over an **external** service (mock vendor-risk API) | registry row + API outcome (ok / not found / unavailable, one retry), review age vs the policy reference date, expiry, conflict, new vendor, legal terms |
| `evaluate_policy_rules` | deterministic, **authoritative** | approvals with reasons, flags, missing information, tier, data classes |
| `lookup_policy_section` | deterministic | exact text of one policy section |

| Decided by | What |
|---|---|
| Code | financial tier, budget, review currency (365 days), registry/API conflict, Security/Privacy/Legal triggers, required fields, injection scan, final merge |
| LLM | does an existing tool already meet the need; data classes implied only by free text (it may add Security/Privacy/Legal with a reason, never remove); recommendation and next-step wording |
| Human | every routing action, approval and exception |

Guardrails (`src/guardrails.py`): code-computed approvals and flags are always kept; LLM-proposed roles other than Security/Privacy/Legal are dropped; LLM evidence is kept only if its source tool ran and every number, date and ID in it appears in the tool results; wording that claims approval or purchase is replaced by a template; `human_review_required` is always true.

Prompt injection: request text, vendor notes and API text reach the LLM only inside tool results marked as untrusted data, a deterministic scanner flags embedded instructions independently of the LLM, and the UI renders all business text as plain text.

## 6. Assumptions

1. **Annual amount** = `annual_cost_usd` as given. One-time costs are treated as the annual amount.
2. **`requested_integrations: []` means "none"**. `null` or an absent key means missing.
3. **Missing data access**: `data_access_level` of `null`, `""` or `"unknown"` counts as missing.
4. **New vendor** = registry `procurement_status == "New"`, or a vendor absent from the registry.
5. **Standard legal terms** = registry `legal_terms_status == "Approved"`. Anything else (Draft, Unknown, blank, not registered) triggers Legal.
6. **Review currency**: a security review is current if `0 ≤ (reference_date − review_date) ≤ 365 days`. The reference date (2026-09-30) is parsed from `data/procurement_policy.md`; the machine clock is never used.
7. **Conflict**: the registry and the API disagree when one says approved and the other does not (expired, not_completed, pending), or when both say approved with different review dates. A conflict is surfaced and routed to Security; neither source wins silently.
8. **Cross-region**: vendor stores data outside the region **and** the request involves PII or confidential documents → material cross-region issue → Privacy and Legal.
9. **Unknown data residency**: vendor-risk API unavailable and the request involves PII or confidential documents → treated as possibly out of region → Privacy.
10. **SSO is not a PII integration.** HR/HRIS/payroll → employee PII; CRM/helpdesk → customer PII; Git/repositories → source code; production/cloud/infrastructure → production access.
11. **Overlap flag**: raised when a catalog product has the same category or an identical product name. Whether it actually *meets the need* is the AI's judgement and drives "use existing tool". Same-vendor-only matches are evidence, not a flag.
12. **Cost missing**: the tier cannot be determined, so the only required approval is Procurement (triage owner) until cost is provided.
13. **Vendor-risk API unavailable, registry review current, data not sensitive**: flag `vendor_risk_unavailable` but do not add Security (the gap is not material). Otherwise an unavailable API adds Security.
14. **Department Head** is shown as a role; the named person is a hint only (the Director of the requester's department, else the nearest Director/VP up the reporting line). Sales and Customer Success report to the Go To Market Director, who has no budget row.

## 7. Evaluation

_Filled from `evals/results/summary.md` after the final run._

## 8. Architecture comparison

_Filled from `evals/results/summary.md` after the final run._

## 9. Ship decision

_See `docs/DECISION_MEMO.md`._

## 10. Known limitations

- **Open questions for the client** (we chose the conservative option for each):
  - Should a budget shortfall block routing, or travel alongside it as an exception? We route with Finance added.
  - Is SSO considered employee-PII processing by Privacy? We assumed not.
  - Who is the Department Head for departments without a Director? We show the nearest Director/VP as a hint.
  - Can AI tools approved for "limited use" be expanded to new data classes without a new assessment? We assumed no (policy §8).
- **Free-tier LLM limits.** The spec's default models are retired. `gemini-3.5-flash` allows 20 requests/day on the free tier; the evaluation uses `gemini-3.5-flash-lite`. Groq's free tier (8,000 tokens/minute) makes each run wait for its token budget. Latency is reported both with and without rate-limit waits.
- **Small evaluation set.** 23 hand-labelled cases (10 official, 11 fixtures, 2 fault injections) and a limited number of trials; differences of one or two cases between architectures are within run-to-run noise.
- **No real integrations.** The vendor-risk service is the provided mock; approvals, notifications and purchasing are simulated through the audit log; there is no authentication or multi-user state.
- **Model judgement is imperfect.** The agent sometimes over-escalates (adds a review the policy does not require) or misses a data class implied only by free text. Code guarantees nothing is removed, but over-escalation still reaches the reviewer.
