"""Reviewer web app (T17): JSON API + static UI. Run via `python run_local.py`."""
from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import requests as http
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import src  # noqa: F401  (loads .env)
from src import data_access as da
from src.config import RUNTIME_DIR, get_settings
from src.policy_engine import requester_info
from src.solution import handle_request_with_trace

STATIC = Path(__file__).resolve().parent / "static"
AUDIT_LOG = RUNTIME_DIR / "audit_log.jsonl"
TRACES = RUNTIME_DIR / "traces"

ACTIONS = ["send_for_approvals", "request_clarification", "suggest_existing_tool", "hold_manual_review"]
RECOMMENDED_ACTION = {
    "route_for_approval": "send_for_approvals",
    "route_for_specialist_review": "send_for_approvals",
    "request_clarification": "request_clarification",
    "use_existing_tool": "suggest_existing_tool",
}
FIELD_LABELS = ["requester (unknown employee ID)", "department", "product or vendor name", "annual cost (USD)",
                "number of users/licenses", "business purpose", "data access level", "required integrations"]

app = FastAPI(title="Procurement Request Copilot")
app.mount("/static", StaticFiles(directory=STATIC), name="static")

_lock = threading.Lock()
_latest: dict[str, dict] = {}   # request_id -> latest analysis response
_runs: dict[str, dict] = {}     # run_id -> analysis response


# ---------------------------------------------------------------- helpers
def _audit_entries(request_id: str | None = None) -> list[dict]:
    if not AUDIT_LOG.is_file():
        return []
    out = []
    for line in AUDIT_LOG.read_text(encoding="utf-8").splitlines():
        if line.strip():
            entry = json.loads(line)
            if request_id is None or entry.get("request_id") == request_id:
                out.append(entry)
    return out


def _status(request_id: str, audited: set[str]) -> str:
    if request_id in audited:
        return "Action recorded"
    if request_id in _latest:
        return "Analysed"
    return "New"


def _evidence_kind(source: str, kind: str) -> str:
    if kind == "ai":
        return "AI analysis"
    return "Rule" if source == "evaluate_policy_rules" else "Tool"


def _banners(decision: dict, policy: dict | None, trace: dict) -> list[dict]:
    flags = set(decision["risk_flags"])
    vendor = (policy or {}).get("vendor") or {}
    derived = vendor.get("derived") or {}
    reg = vendor.get("registry") or {}
    api = vendor.get("api") or {}
    out = []
    if "llm_unavailable" in flags:
        out.append({"level": "warn", "title": "AI analysis unavailable: showing rule-based result",
                    "detail": "Deterministic policy checks ran normally; recommendation wording uses templates."})
    if "vendor_risk_unavailable" in flags:
        out.append({"level": "warn",
                    "title": "Vendor-risk service unavailable: security status could not be verified",
                    "detail": "No favorable vendor status is assumed. " + (api.get("error") or "")})
    if "conflicting_vendor_evidence" in flags:
        out.append({"level": "warn", "title": "Vendor evidence conflicts: registry and vendor-risk service disagree",
                    "detail": "Neither source is trusted silently; Security must resolve it.",
                    "compare": {
                        "Registry (vendors.csv)": f"{reg.get('security_status') or 'blank'} · review "
                                                  f"{reg.get('security_review_date') or 'none'}",
                        "Vendor-risk API": f"{(api.get('record') or {}).get('security_review_status') or '-'} · review "
                                           f"{(api.get('record') or {}).get('last_review_date') or 'none'}",
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


def _response(decision, trace: dict) -> dict:
    d = decision.model_dump()
    policy = trace.get("policy")
    kinds = trace.get("evidence_kinds") or []
    evidence = [{**e, "kind": _evidence_kind(e["source"], kinds[i] if i < len(kinds) else "rule")}
                for i, e in enumerate(d["evidence"])]
    missing_fields = [m for m in d["missing_information"] if m in FIELD_LABELS]
    could_not_verify = [m for m in d["missing_information"] if m not in FIELD_LABELS]
    vendor = (policy or {}).get("vendor") or {}
    return {
        "decision": d,
        "decision_type": trace["decision_type"],
        "recommended_action": RECOMMENDED_ACTION.get(trace["decision_type"], "hold_manual_review"),
        "approvals": trace.get("approvals") or [],
        "flag_reasons": trace.get("flag_reasons") or {},
        "evidence": evidence,
        "missing_fields": missing_fields,
        "could_not_verify": could_not_verify,
        "questions_for_reviewer": trace.get("questions_for_reviewer") or [],
        "banners": _banners(d, policy, trace),
        "requester": (policy or {}).get("requester"),
        "vendor": {"registry": vendor.get("registry"), "api": vendor.get("api"), "derived": vendor.get("derived")},
        "overlap_candidates": (policy or {}).get("overlap_flag_candidates") or [],
        "trace_summary": {k: trace.get(k) for k in (
            "run_id", "architecture", "mode", "path", "provider", "model", "llm_calls", "tool_calls",
            "latency_active_ms", "latency_total_ms", "llm_wait_ms", "tokens", "event_counts", "error")},
    }


def _analyse(request_id: str, architecture: str, mode: str) -> dict:
    try:
        da.get_request(request_id)
    except KeyError:
        raise HTTPException(404, f"Unknown request {request_id}")
    decision, trace = handle_request_with_trace(request_id, architecture, mode=mode)
    TRACES.mkdir(parents=True, exist_ok=True)
    (TRACES / f"{trace['run_id']}.json").write_text(
        json.dumps({"decision": decision.model_dump(), "trace": trace}, indent=2, default=str), encoding="utf-8")
    resp = _response(decision, trace)
    with _lock:
        _latest[request_id] = resp
        _runs[trace["run_id"]] = resp
    return resp


def _load_run(run_id: str) -> dict | None:
    if run_id in _runs:
        return _runs[run_id]
    if not re.fullmatch(r"[0-9A-Za-z-]+", run_id or ""):
        return None
    path = TRACES / f"{run_id}.json"
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return _response(_Decision(data["decision"]), data["trace"])


class _Decision:
    def __init__(self, d: dict):
        self._d = d

    def model_dump(self) -> dict:
        return self._d


# ---------------------------------------------------------------- routes
@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/api/health")
def health() -> dict:
    s = get_settings()
    try:
        vendor_ok = http.get(f"{s.vendor_risk_base_url}/health", timeout=1).ok
    except http.RequestException:
        vendor_ok = False
    return {"app": "ok", "vendor_api": "ok" if vendor_ok else "down",
            "llm": {"provider": s.llm_provider, "model": s.model_name,
                    "configured": s.llm_configured and not s.llm_simulate_outage}}


@app.get("/api/requests")
def list_requests() -> list[dict]:
    audited = {e["request_id"] for e in _audit_entries()}
    rows = []
    for r in da.load_requests(include_submitted=True):
        rid = str(r.get("request_id"))
        emp = da.find_employee(r.get("requester_id"))
        cost = r.get("annual_cost_usd")
        rows.append({
            "request_id": rid,
            "product_name": r.get("product_name"),
            "vendor_name": r.get("vendor_name"),
            "requester": emp.get("name") if emp else None,
            "department": emp.get("department") if emp else None,
            "annual_cost_usd": cost if isinstance(cost, (int, float)) and not isinstance(cost, bool) else None,
            "urgency": r.get("urgency"),
            "status": _status(rid, audited),
            "decision_type": (_latest.get(rid) or {}).get("decision_type"),
        })
    return rows


@app.get("/api/requests/{request_id}")
def get_request(request_id: str) -> dict:
    try:
        raw = da.get_request(request_id)
    except KeyError:
        raise HTTPException(404, f"Unknown request {request_id}")
    return {"request": raw, "requester": requester_info(raw.get("requester_id")),
            "latest_analysis": _latest.get(request_id)}


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


@app.post("/api/requests")
def submit_request(body: NewRequest) -> dict:
    with _lock:
        rows = da.load_submitted_requests()
        numbers = [int(m.group(1)) for r in rows if (m := re.fullmatch(r"REQ-U(\d+)", str(r.get("request_id"))))]
        rid = f"REQ-U{(max(numbers) + 1) if numbers else 1:03d}"
        record = {"request_id": rid, **{k: (v.strip() if isinstance(v, str) else v)
                                         for k, v in body.model_dump().items()}}
        for k, v in list(record.items()):
            if isinstance(v, str) and not v:
                record[k] = None
        rows.append(record)
        da.save_submitted_requests(rows)
    return record


class AnalyzeBody(BaseModel):
    request_id: str
    architecture: Literal["single", "staged"] = "single"
    mode: Literal["llm", "rules_only"] = "llm"


@app.post("/api/analyze")
def analyze(body: AnalyzeBody) -> dict:  # sync def -> FastAPI runs it in a threadpool
    return _analyse(body.request_id, body.architecture, body.mode)


@app.post("/api/compare")
def compare(body: AnalyzeBody) -> dict:
    return {"single": _analyse(body.request_id, "single", body.mode),
            "staged": _analyse(body.request_id, "staged", body.mode)}


class ActionBody(BaseModel):
    request_id: str
    run_id: str
    action: Literal["send_for_approvals", "request_clarification", "suggest_existing_tool", "hold_manual_review"]
    reason: str | None = Field(default=None, max_length=2000)


@app.post("/api/actions")
def record_action(body: ActionBody) -> dict:
    run = _load_run(body.run_id)
    if run is None or run["decision"]["request_id"] != body.request_id:
        raise HTTPException(400, "Unknown run for this request: analyse the request first.")
    recommended = run["recommended_action"]
    override = body.action != recommended
    reason = (body.reason or "").strip()
    if override and not reason:
        raise HTTPException(400, "A reason is required when the action differs from the copilot recommendation.")
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "request_id": body.request_id,
        "run_id": body.run_id,
        "architecture": run["trace_summary"]["architecture"],
        "copilot_decision_type": run["decision_type"],
        "recommended_action": recommended,
        "action": body.action,
        "override": override,
        "reason": reason or None,
        "approvers": run["decision"]["required_approvals"] if body.action == "send_for_approvals" else [],
        "outcome": "pending",
    }
    AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
    with _lock, AUDIT_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")
    return entry


@app.get("/api/audit")
def audit(request_id: str | None = Query(default=None)) -> list[dict]:
    return _audit_entries(request_id)
