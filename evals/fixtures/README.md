# Eval-only fixtures

These records exist only for evaluation. They are loaded on top of `data/` when `EXTRA_DATA_DIR=evals/fixtures` (see SPEC_TECHNICAL T4.1). `evals/run_all.py` sets this automatically. The official `data/` files are never modified.

| File | Adds |
|---|---|
| `requests.json` | REQ-X101 to REQ-X111 (boundaries, casing, unregistered vendor, injection in product name and in API notes, HR integration, unknown requester, PII implied only by text) |
| `vendors.csv` | LedgerLens, TeamPulse, Formwise (registry rows) |
| `vendor_risk.json` | Matching mock vendor-risk records; LedgerLens notes contain a prompt injection |

ChurnRadar Inc (REQ-X105) is deliberately absent from both the registry and the API, so the API returns 404.
