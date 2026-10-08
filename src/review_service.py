"""Reviewer workflow logic shared by the UI and tests (T17 behaviour, UI-independent).

Queue rows, analysis view model, request submission, the override-reason rule and the audit log.
The latest analysis of each request is persisted (runtime/latest_runs.json -> runtime/traces/<run_id>.json),
so queue status and results survive an app restart.
"""
from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

import requests as http
from pydantic import BaseModel, Field

from src import data_access as da
from src.config import RUNTIME_DIR, get_settings
from src.policy_engine import requester_info
from src.solution import handle_request_with_trace

AUDIT_LOG = RUNTIME_DIR / "audit_log.jsonl"
TRACES = RUNTIME_DIR / "traces"
LATEST = RUNTIME_DIR / "latest_runs.json"

ACTIONS = ["send_for_approvals", "request_clarification", "suggest_existing_tool", "hold_manual_review"]
ACTION_LABEL = {
    "send_for_approvals": "Send for approvals",
    "request_clarification": "Request clarification",
    "suggest_existing_tool": "Suggest existing tool",
    "hold_manual_review": "Hold for manual review",
}
DECISION_LABEL = {
    "route_for_approval": "Route for approval",
    "route_for_specialist_review": "Route for specialist review",
    "request_clarification": "Request clarification",
    "use_existing_tool": "Use existing tool",
}
RECOMMENDED_ACTION = {
    "route_for_approval": "send_for_approvals",
    "route_for_specialist_review": "send_for_approvals",
    "request_clarification": "request_clarification",
    "use_existing_tool": "suggest_existing_tool",
}
FIELD_LABELS = ["requester (unknown employee ID)", "department", "product or vendor name", "annual cost (USD)",
                "number of users/licenses", "business purpose", "data access level", "required integrations"]
SPECIALISTS = {"Security", "Privacy", "Legal"}

_lock = threading.Lock()


class NotFound(KeyError):
    pass


class ActionRejected(ValueError):
    pass


# ---------------------------------------------------------------- persistence helpers
def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else default
    except (OSError, json.JSONDecodeError):
        return default


def audit_entries(request_id: str | None = None) -> list[dict]:
    if not AUDIT_LOG.is_file():
        return []
    out = []
    for line in AUDIT_LOG.read_text(encoding="utf-8").splitlines():
        if line.strip():
            entry = json.loads(line)
            if request_id is None or entry.get("request_id") == request_id:
                out.append(entry)
    return out


def _latest_map() -> dict:
    return _read_json(LATEST, {})


def load_run(run_id: str) -> dict | None:
    """Analysis view model for a saved run, or None."""
    if not re.fullmatch(r"[0-9A-Za-z-]+", run_id or ""):
        return None
    data = _read_json(TRACES / f"{run_id}.json", None)
    return view_model(data["decision"], data["trace"]) if data else None


def latest_analysis(request_id: str) -> dict | None:
    run_id = _latest_map().get(request_id)
    return load_run(run_id) if run_id else None


# ---------------------------------------------------------------- view model
def _evidence_kind(source: str, kind: str) -> str:
    if kind == "ai":
        return "AI analysis"
    return "Rule" if source == "evaluate_policy_rules" else "Tool"


def banners(decision: dict, policy: dict | None, trace: dict) -> list[dict]:
    flags = set(decision["risk_flags"])
    vendor = (policy or {}).get("vendor") or {}
    reg = vendor.get("registry") or {}
    api = vendor.get("api") or {}
    rec = api.get("record") or {}
    out = []
    if "llm_unavailable" in flags:
        out.append({"level": "warn", "title": "AI analysis unavailable: showing rule-based result",
                    "detail": "Deterministic policy checks ran normally; recommendation wording uses templates."})
    if "vendor_risk_unavailable" in flags:
        out.append({"level": "warn", "title": "Vendor-risk service unavailable: security status could not be verified",
                    "detail": "No favorable vendor status is assumed. " + (api.get("error") or "")})
    if "conflicting_vendor_evidence" in flags:
        out.append({"level": "warn", "title": "Vendor evidence conflicts: registry and vendor-risk service disagree",
                    "detail": "Neither source is trusted silently; Security must resolve it.",
                    "compare": {
                        "Registry (vendors.csv)": f"{reg.get('security_status') or 'blank'} · review "
                                                  f"{reg.get('security_review_date') or 'none'}",
                        "Vendor-risk API": f"{rec.get('security_review_status') or '-'} · review "
                                           f"{rec.get('last_review_date') or 'none'}",
                    }})
    if "vendor_review_expired" in flags:
        reasons = [r["detail"] for r in (trace.get("flag_reasons") or {}).get("vendor_review_expired", [])]
        out.append({"level": "warn", "title": "Vendor security review expired", "detail": "; ".join(reasons)})
    if "prompt_injection_detected" in flags:
        hits = (policy or {}).get("injection", {}).get("hits", [])
        out.append({"level": "danger", "title": "Possible prompt injection in business data: embedded instructions were ignored",
                    "detail": "The quoted text is shown as data only. It changed no approvals, flags or decision.",
                    "excerpts": [f"{h['field']}: {h['excerpt']}" for h in hits]})
    if "vendor_not_registered" in flags:
        out.append({"level": "info", "title": "Vendor is not in the vendor registry",
                    "detail": "Treated as a new vendor with unknown legal terms."})
    if trace.get("path") == "error":
        out.append({"level": "danger", "title": "Analysis error", "detail": trace.get("error") or ""})
    return out


def view_model(decision: dict, trace: dict) -> dict:
    policy = trace.get("policy")
    kinds = trace.get("evidence_kinds") or []
    evidence = [{**e, "kind": _evidence_kind(e["source"], kinds[i] if i < len(kinds) else "rule")}
                for i, e in enumerate(decision["evidence"])]
    vendor = (policy or {}).get("vendor") or {}
    return {
        "decision": decision,
        "decision_type": trace["decision_type"],
        "recommended_action": RECOMMENDED_ACTION.get(trace["decision_type"], "hold_manual_review"),
        "approvals": trace.get("approvals") or [],
        "flag_reasons": trace.get("flag_reasons") or {},
        "evidence": evidence,
        "missing_fields": [m for m in decision["missing_information"] if m in FIELD_LABELS],
        "could_not_verify": [m for m in decision["missing_information"] if m not in FIELD_LABELS],
        "questions_for_reviewer": trace.get("questions_for_reviewer") or [],
        "banners": banners(decision, policy, trace),
        "requester": (policy or {}).get("requester"),
        "vendor": {"registry": vendor.get("registry"), "api": vendor.get("api"), "derived": vendor.get("derived")},
        "overlap_candidates": (policy or {}).get("overlap_flag_candidates") or [],
        "trace_summary": {k: trace.get(k) for k in (
            "run_id", "architecture", "mode", "path", "provider", "model", "llm_calls", "tool_calls",
            "latency_active_ms", "latency_total_ms", "llm_wait_ms", "tokens", "event_counts", "error")},
    }


# ---------------------------------------------------------------- operations
def health() -> dict:
    s = get_settings()
    try:  # a status probe must never break the page
        vendor_ok = http.get(f"{s.vendor_risk_base_url}/health", timeout=1).status_code == 200
    except Exception:
        vendor_ok = False
    return {"vendor_api": "ok" if vendor_ok else "down",
            "llm": {"provider": s.llm_provider, "model": s.model_name,
                    "configured": s.llm_configured and not s.llm_simulate_outage}}


def queue() -> list[dict]:
    audited = {e["request_id"] for e in audit_entries()}
    latest = _latest_map()
    rows = []
    for r in da.load_requests(include_submitted=True):
        rid = str(r.get("request_id"))
        emp = da.find_employee(r.get("requester_id"))
        cost = r.get("annual_cost_usd")
        status = "Action recorded" if rid in audited else ("Analysed" if rid in latest else "New")
        run = load_run(latest[rid]) if rid in latest else None
        rows.append({
            "request_id": rid,
            "product_name": r.get("product_name"),
            "vendor_name": r.get("vendor_name"),
            "requester": emp.get("name") if emp else None,
            "department": emp.get("department") if emp else None,
            "annual_cost_usd": cost if isinstance(cost, (int, float)) and not isinstance(cost, bool) else None,
            "urgency": r.get("urgency"),
            "status": status,
            "decision_type": run["decision_type"] if run else None,
        })
    return rows


def get_request(request_id: str) -> dict:
    try:
        raw = da.get_request(request_id)
    except KeyError as exc:
        raise NotFound(request_id) from exc
    return {"request": raw, "requester": requester_info(raw.get("requester_id"))}


def analyse(request_id: str, architecture: str = "single", mode: str = "llm") -> dict:
    get_request(request_id)  # raises NotFound
    decision, trace = handle_request_with_trace(request_id, architecture, mode=mode)
    d = decision.model_dump()
    TRACES.mkdir(parents=True, exist_ok=True)
    (TRACES / f"{trace['run_id']}.json").write_text(
        json.dumps({"decision": d, "trace": trace}, indent=2, default=str), encoding="utf-8")
    with _lock:
        latest = _latest_map()
        latest[request_id] = trace["run_id"]
        LATEST.parent.mkdir(parents=True, exist_ok=True)
        LATEST.write_text(json.dumps(latest, indent=1), encoding="utf-8")
    return view_model(d, trace)


class NewRequest(BaseModel):
    requester_id: str | None = None
    product_name: str | None = None
    vendor_name: str | None = None
    category: str | None = None
    annual_cost_usd: float | None = Field(default=None, ge=0)
    user_count: int | None = Field(default=None, gt=0)
    business_justification: str | None = Field(default=None, max_length=4000)
    data_access_level: str | None = None
    requested_integrations: list[str] | None = None
    urgency: str | None = None


def submit_request(fields: dict) -> dict:
    """Validate and append a request; assigns REQ-U###. Blank strings become None (missing)."""
    body = NewRequest.model_validate(fields)
    with _lock:
        rows = da.load_submitted_requests()
        numbers = [int(m.group(1)) for r in rows if (m := re.fullmatch(r"REQ-U(\d+)", str(r.get("request_id"))))]
        rid = f"REQ-U{(max(numbers) + 1) if numbers else 1:03d}"
        record = {"request_id": rid}
        for k, v in body.model_dump().items():
            v = v.strip() if isinstance(v, str) else v
            record[k] = None if v == "" else v
        rows.append(record)
        da.save_submitted_requests(rows)
    return record


def consequence(action: str, view: dict, requester_name: str | None) -> str:
    who = requester_name or "the requester"
    roles = view["decision"]["required_approvals"]
    if action == "send_for_approvals":
        noun = "approver" if len(roles) == 1 else "approvers"
        return (f"Sends the evidence pack to {len(roles)} {noun}: {', '.join(roles)}. "
                "Nothing is approved or purchased; each approver decides.")
    if action == "request_clarification":
        items = "; ".join(view["missing_fields"]) or "the information you name in the reason"
        return f"Returns the request to {who}, asking for: {items}. It leaves the review queue until they reply."
    if action == "suggest_existing_tool":
        ids = ", ".join(view["overlap_candidates"]) or "an existing licensed tool"
        return f"Tells {who} that {ids} may already cover this need. No new purchase is started."
    return "Keeps the request open for a manual procurement review. No one is notified."


def record_action(request_id: str, run_id: str, action: str, reason: str | None) -> dict:
    """Append an audit entry. A reason is required when the action differs from the recommendation (AC-5.3)."""
    if action not in ACTIONS:
        raise ActionRejected(f"Unknown action '{action}'.")
    run = load_run(run_id)
    if run is None or run["decision"]["request_id"] != request_id:
        raise ActionRejected("Unknown run for this request: analyse the request first.")
    recommended = run["recommended_action"]
    override = action != recommended
    reason = (reason or "").strip()[:2000]
    if override and not reason:
        raise ActionRejected("A reason is required when the action differs from the copilot recommendation.")
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "request_id": request_id,
        "run_id": run_id,
        "architecture": run["trace_summary"]["architecture"],
        "copilot_decision_type": run["decision_type"],
        "recommended_action": recommended,
        "action": action,
        "override": override,
        "reason": reason or None,
        "approvers": run["decision"]["required_approvals"] if action == "send_for_approvals" else [],
        "outcome": "pending",
    }
    with _lock:
        AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
        with AUDIT_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    return entry
