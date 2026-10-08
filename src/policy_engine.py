"""Deterministic policy engine (T5, rules R0-R12).

The evidence functions (`request_details`, `budget_check`, `catalog_search`, `vendor_status`)
back the agent tools. `evaluate()` combines them, through the RunContext cache, into the
authoritative PolicyResult. Nothing here depends on request IDs, vendor or product names.
"""
from __future__ import annotations

import re
from datetime import date, timedelta

from src import data_access as da
from src.config import get_policy_version, get_reference_date
from src.injection import scan_fields
from src.normalize import NormalizedRequest, normalize_request
from src.schemas import (
    FLAG_ORDER, SPECIALIST_FLAG, EvidenceRecord, PolicyResult, Reason, RoleApproval, order_flags, order_roles,
)
from src.vendor_client import get_vendor_risk_classified

REVIEW_VALID_DAYS = 365

# ---------------------------------------------------------------- R1 labels
LBL_REQUESTER = "requester (unknown employee ID)"
LBL_DEPARTMENT = "department"
LBL_PRODUCT = "product or vendor name"
LBL_COST = "annual cost (USD)"
LBL_USERS = "number of users/licenses"
LBL_PURPOSE = "business purpose"
LBL_DATA = "data access level"
LBL_INTEGRATIONS = "required integrations"
GAP_VENDOR_UNAVAILABLE = "vendor security assessment (vendor-risk service unavailable)"
GAP_RESIDENCY = "vendor data-residency status (unverified)"
GAP_NOT_FOUND = "vendor-risk record (not found)"
GAP_NO_BUDGET = "department software budget (no budget record)"


def money(value: float | None) -> str:
    if value is None:
        return "not provided"
    return f"${value:,.2f}" if value != int(value) else f"${int(value):,}"


# ---------------------------------------------------------------- R4 financial tier
def tier_roles(cost: float | None) -> tuple[list[str], str]:
    if cost is None:
        return ["Procurement"], "undetermined (annual cost missing): Procurement triages"
    if cost <= 1000:
        return ["Manager"], "up to $1,000"
    if cost <= 10000:
        return ["Department Head", "Procurement"], "$1,000.01-$10,000"
    if cost <= 25000:
        return ["Department Head", "Finance", "Procurement"], "$10,000.01-$25,000"
    return ["Department Head", "Finance", "CFO", "Procurement"], "above $25,000"


# ---------------------------------------------------------------- R6 vendor classes
def classify_registry(status: object) -> str:
    s = str(status or "").strip().casefold()
    return {"approved": "approved", "pending": "pending", "expired": "expired"}.get(s, "unknown")


def classify_api(status: object) -> str:
    s = str(status or "").strip().casefold()
    if s == "approved":
        return "approved"
    if s == "expired":
        return "expired"
    if s in ("not_completed", "pending", "in_progress"):
        return "pending"
    return "unknown"


def parse_date(value: object) -> date | None:
    try:
        return date.fromisoformat(str(value).strip()) if value else None
    except ValueError:
        return None


def review_age(review: date | None, ref: date) -> int | None:
    return (ref - review).days if review else None


def is_current(age: int | None) -> bool:
    return age is not None and 0 <= age <= REVIEW_VALID_DAYS


def derive_vendor(registry: dict | None, api: dict, ref: date) -> dict:
    """Reconcile the registry row and the classified API result (R6). Pure function."""
    registered = registry is not None
    reg_class = classify_registry(registry.get("security_status")) if registered else None
    reg_date = parse_date(registry.get("security_review_date")) if registered else None
    reg_age = review_age(reg_date, ref)

    outcome = api.get("outcome", "unavailable")
    record = api.get("record") or {}
    api_class = classify_api(record.get("security_review_status")) if outcome == "ok" else None
    api_date = parse_date(record.get("last_review_date")) if outcome == "ok" else None
    api_age = review_age(api_date, ref)

    expired = bool(
        (reg_class == "approved" and reg_age is not None and reg_age > REVIEW_VALID_DAYS)
        or api_class == "expired"
        or (api_class == "approved" and api_age is not None and api_age > REVIEW_VALID_DAYS)
    )
    conflict = False
    if registered and outcome == "ok":
        one_approved = (reg_class == "approved") != (api_class == "approved")
        both_approved_dates_differ = reg_class == api_class == "approved" and reg_date != api_date
        conflict = one_approved or both_approved_dates_differ

    api_ok_for_clearance = (api_class == "approved" and is_current(api_age)) or outcome in ("unavailable", "not_found")
    cleared = bool(
        registered and reg_class == "approved" and is_current(reg_age)
        and not conflict and not expired and api_ok_for_clearance
    )
    procurement_status = registry.get("procurement_status") if registered else None
    legal_terms = (registry.get("legal_terms_status") or None) if registered else None
    return {
        "registered": registered,
        "registry_class": reg_class,
        "registry_review_date": reg_date.isoformat() if reg_date else None,
        "registry_age_days": reg_age,
        "registry_current": is_current(reg_age) if registered else False,
        "api_outcome": outcome,
        "api_class": api_class,
        "api_review_date": api_date.isoformat() if api_date else None,
        "api_age_days": api_age,
        "expired": expired,
        "conflict": conflict,
        "cleared": cleared,
        "new_vendor": (not registered) or str(procurement_status or "").strip().casefold() == "new",
        "procurement_status": procurement_status,
        "legal_terms_status": legal_terms,
        "legal_terms_approved": str(legal_terms or "").strip().casefold() == "approved",
        "stores_data_outside_region": record.get("stores_data_outside_region") if outcome == "ok" else None,
        "processes_personal_data": record.get("processes_personal_data") if outcome == "ok" else None,
    }


# ---------------------------------------------------------------- R7 data classes
LEVEL_CLASSES = {
    "none": [], "internal": [], "internal_documents": [], "internal_marketing": [], "public": [],
    "source_code": ["source_code"],
    "customer_pii": ["customer_pii"],
    "employee_pii": ["employee_pii"],
    "confidential_documents": ["confidential_documents"],
    "production_telemetry": ["production_access"],
    "production_data": ["production_access"],
    "production_access": ["production_access"],
    "credentials": ["credentials"],
    "secrets": ["credentials"],
}


def data_classes_for_level(level: str | None) -> tuple[list[str], bool]:
    """Return (classes, recognized). Unknown values use the keyword fallback (substring match,
    deliberately broad: more review, never less)."""
    if level is None:
        return [], True
    lvl = level.casefold()
    if lvl in LEVEL_CLASSES:
        return list(LEVEL_CLASSES[lvl]), True
    classes: list[str] = []
    if "pii" in lvl or "personal" in lvl:
        classes.append("employee_pii" if ("employee" in lvl or "hr" in lvl) else "customer_pii")
    if any(k in lvl for k in ("source", "code", "repo")):
        classes.append("source_code")
    if "confidential" in lvl:
        classes.append("confidential_documents")
    if "prod" in lvl or "cloud" in lvl:
        classes.append("production_access")
    if any(k in lvl for k in ("credential", "secret", "password")):
        classes.append("credentials")
    return classes, bool(classes)


INTEGRATION_RULES = [
    (re.compile(r"\b(document|documents|drive|sharepoint|dropbox)\b"), "confidential_documents"),
    (re.compile(r"\b(git|github|gitlab|bitbucket|repo|repository|repositories|source code)\b"), "source_code"),
    (re.compile(r"\b(production|prod|cloud|aws|gcp|azure|kubernetes|k8s|database)\b"), "production_access"),
    (re.compile(r"\b(hris|workday|payroll|hr system|bamboohr|employee directory)\b"), "employee_pii"),
    (re.compile(r"\b(crm|salesforce|hubspot|helpdesk|helpdeskly|zendesk|support tickets?)\b"), "customer_pii"),
    (re.compile(r"\b(vault|secrets?|credentials?|passwords?)\b"), "credentials"),
    (re.compile(r"\b(sso|okta|saml|slack|email)\b"), None),
]


def integration_class(integration: str) -> str | None:
    text = integration.casefold()
    for pattern, cls in INTEGRATION_RULES:
        if pattern.search(text):
            return cls
    return None


# ---------------------------------------------------------------- R2 requester
def _employees_by_id() -> dict[str, dict]:
    return {r["employee_id"]: r for r in da.load_csv_records("employees.csv")}


def requester_info(requester_id: str | None) -> dict | None:
    emp = da.find_employee(requester_id)
    if emp is None:
        return None
    by_id = _employees_by_id()
    manager = by_id.get(emp.get("manager_id") or "")
    dept = emp.get("department") or None
    head = next(
        (e for e in by_id.values()
         if da.norm_key(e.get("department", "")) == da.norm_key(dept or "") and e.get("level") == "Director"),
        None,
    )
    if head is None:  # walk up the reporting line to the nearest Director/VP
        cur, seen = manager, set()
        while cur and cur["employee_id"] not in seen:
            seen.add(cur["employee_id"])
            if cur.get("level") in ("Director", "VP"):
                head = cur
                break
            cur = by_id.get(cur.get("manager_id") or "")
    return {
        "id": emp["employee_id"],
        "name": emp.get("name"),
        "department": dept,
        "level": emp.get("level"),
        "manager_hint": manager.get("name") if manager else None,
        "department_head_hint": f"{head['name']} ({head.get('level')}, {head.get('department')})" if head else None,
    }


# ---------------------------------------------------------------- R1 completeness
def missing_request_fields(req: NormalizedRequest, requester: dict | None) -> list[str]:
    missing = []
    if requester is None:
        missing.append(LBL_REQUESTER)
    if requester is None or not requester.get("department"):
        missing.append(LBL_DEPARTMENT)
    if not req.product_name or not req.vendor_name:
        missing.append(LBL_PRODUCT)
    if req.annual_cost_usd is None:
        missing.append(LBL_COST)
    if req.user_count is None:
        missing.append(LBL_USERS)
    if not req.business_justification:
        missing.append(LBL_PURPOSE)
    if req.data_access_level is None:
        missing.append(LBL_DATA)
    if req.requested_integrations is None:
        missing.append(LBL_INTEGRATIONS)
    return missing


# ================================================================ tool backends
def request_details(request_id: str) -> dict:
    raw = da.get_request(request_id)
    req = normalize_request(raw)
    requester = requester_info(req.requester_id)
    missing = missing_request_fields(req, requester)
    return {
        "status": "ok",
        "request_id": req.request_id,
        "facts": {
            "requester_id": req.requester_id,
            "annual_cost_usd": req.annual_cost_usd,
            "user_count": req.user_count,
            "data_access_level": req.data_access_level,
            "integrations_count": None if req.requested_integrations is None else len(req.requested_integrations),
            "urgency_informational_only": req.urgency,
        },
        "untrusted_text": {
            "product_name": req.product_name,
            "vendor_name": req.vendor_name,
            "category": req.category,
            "business_justification": req.business_justification,
            "requested_integrations": req.requested_integrations,
            "raw_data_access_level": raw.get("data_access_level"),
        },
        "requester": requester,
        "field_completeness": {"complete": not missing, "missing": missing},
        "normalization_notes": req.notes,
        "injection_scan": scan_fields({k: v for k, v in raw.items()}),
    }


def budget_check(request_id: str) -> dict:
    raw = da.get_request(request_id)
    req = normalize_request(raw)
    requester = requester_info(req.requester_id)
    dept = requester.get("department") if requester else None
    out = {
        "status": "not_checked", "department": dept, "annual_cost": req.annual_cost_usd,
        "available_usd": None, "committed_usd": None, "annual_budget_usd": None,
        "within_budget": None, "remaining_after_usd": None, "reference": None,
    }
    if dept is None or req.annual_cost_usd is None:
        out["reason"] = "department unknown" if dept is None else "annual cost missing"
        return out
    row = da.find_budget(dept)
    if row is None:
        out.update(status="unverified", reason="no budget record for department")
        return out
    available = float(row["available_usd"])
    within = req.annual_cost_usd <= available
    out.update(
        status="ok" if within else "insufficient",
        available_usd=available,
        committed_usd=float(row["committed_usd"]),
        annual_budget_usd=float(row["annual_software_budget_usd"]),
        within_budget=within,
        remaining_after_usd=available - req.annual_cost_usd,
        reference=f"department_budgets.csv:{row['department']}",
    )
    return out


def catalog_search(request_id: str, keywords: list[str] | None = None) -> dict:
    raw = da.get_request(request_id)
    req = normalize_request(raw)
    keywords = [k.strip() for k in (keywords or []) if isinstance(k, str) and k.strip()][:5]
    cat_key = da.norm_key(req.category) if req.category else None
    vendor_key = da.norm_key(req.vendor_name) if req.vendor_name else None
    product_key = da.norm_key(req.product_name) if req.product_name else None
    history = da.load_csv_records("purchase_history.csv")

    candidates, flagged = [], []
    for row in da.load_csv_records("software_catalog.csv"):
        match = []
        if cat_key and da.norm_key(row.get("category", "")) == cat_key:
            match.append("same_category")
        if vendor_key and da.norm_key(row.get("vendor_name", "")) == vendor_key:
            match.append("same_vendor")
        if product_key and da.norm_key(row.get("product_name", "")) == product_key:
            match.append("identical_product")
        hay = " ".join(da.norm_key(row.get(f, "")) for f in ("product_name", "notes", "category"))
        for kw in keywords:
            if da.norm_key(kw) and da.norm_key(kw) in hay:
                match.append(f"keyword:{kw}")
        if not match:
            continue
        flags_overlap = "same_category" in match or "identical_product" in match
        if flags_overlap:
            flagged.append(row["software_id"])
        candidates.append({
            "software_id": row["software_id"],
            "product_name": row.get("product_name"),
            "vendor_name": row.get("vendor_name"),
            "category": row.get("category"),
            "status": row.get("status"),
            "licensed_seats": int(row["licensed_seats"]) if str(row.get("licensed_seats", "")).isdigit() else None,
            "scope": row.get("scope"),
            "annual_cost_usd": float(row["annual_cost_usd"]) if row.get("annual_cost_usd") else None,
            "match": match,
            "raises_overlap_flag": flags_overlap,
            "purchase_history": [
                {k: h.get(k) for k in ("purchase_id", "purchase_date", "department", "annual_amount_usd", "status")}
                for h in history if da.norm_key(h.get("product_name", "")) == da.norm_key(row.get("product_name", ""))
            ],
            "untrusted_text": {"notes": row.get("notes")},
        })
    return {
        "status": "ok",
        "request_category": req.category,
        "keywords_used": keywords,
        "candidates": candidates,
        "overlap_flag_candidates": flagged,
        "injection_scan": scan_fields({f"catalog.{c['software_id']}.notes": c["untrusted_text"]["notes"] for c in candidates}),
    }


def vendor_status(vendor_name: str) -> dict:
    name = (vendor_name or "").strip()
    registry = da.find_vendor(name)
    query_name = registry["vendor_name"] if registry else name
    api = get_vendor_risk_classified(query_name) if query_name else {
        "outcome": "not_found", "status_code": None, "record": None, "error": "no vendor name", "attempts": 0,
        "duration_ms": 0, "endpoint": None,
    }
    derived = derive_vendor(registry, api, get_reference_date())
    record = api.get("record") or {}
    api_block = {
        "outcome": api["outcome"], "status_code": api.get("status_code"), "attempts": api.get("attempts"),
        "endpoint": api.get("endpoint"), "error": api.get("error"),
        "record": {k: v for k, v in record.items() if k != "notes"} if record else None,
    }
    registry_block = (
        {k: registry.get(k) for k in ("vendor_id", "vendor_name", "procurement_status", "security_status",
                                      "security_review_date", "legal_terms_status")}
        if registry else None
    )
    untrusted = {"registry_notes": registry.get("notes") if registry else None,
                 "api_notes": record.get("notes") if record else None}
    return {
        "status": "ok",
        "vendor_name_queried": query_name,
        "reference_date": get_reference_date().isoformat(),
        "registry": registry_block,
        "api": api_block,
        "derived": derived,
        "untrusted_text": untrusted,
        "injection_scan": scan_fields({"vendor.registry_notes": untrusted["registry_notes"],
                                       "vendor.api_notes": untrusted["api_notes"]}),
    }


# ================================================================ evaluate (authoritative)
class _Builder:
    def __init__(self):
        self.roles: dict[str, list[Reason]] = {}
        self.flags: dict[str, list[Reason]] = {}
        self.evidence: list[EvidenceRecord] = []

    def role(self, role: str, rule: str, ref: str, detail: str):
        self.roles.setdefault(role, []).append(Reason(rule=rule, policy_ref=ref, detail=detail))

    def flag(self, flag: str, rule: str, ref: str, detail: str):
        self.flags.setdefault(flag, []).append(Reason(rule=rule, policy_ref=ref, detail=detail))

    def ev(self, source: str, finding: str, reference: str | None, kind: str = "rule"):
        self.evidence.append(EvidenceRecord(source=source, finding=finding, reference=reference, kind=kind))


def _short(text: object, n: int = 40) -> str:
    s = " ".join(str(text).split())
    return s if len(s) <= n else s[: n - 1] + "…"


def evaluate(request_id: str, ctx) -> PolicyResult:
    """Run all deterministic rules. Uses `ctx.execute` so each tool runs at most once per run."""
    ref = get_reference_date()
    details = ctx.execute("get_request_details", {"request_id": request_id}, initiator="orchestrator")
    if details.get("status") != "ok":
        raise RuntimeError(f"request details unavailable: {details.get('error')}")
    budget = ctx.execute("check_budget", {"request_id": request_id}, initiator="orchestrator")
    catalog = ctx.execute("search_software_catalog", {"request_id": request_id}, initiator="orchestrator")
    vendor_name = details["untrusted_text"]["vendor_name"]
    vendor = (ctx.execute("get_vendor_status", {"vendor_name": vendor_name}, initiator="orchestrator")
              if vendor_name else None)

    req = normalize_request(da.get_request(request_id))
    requester = details.get("requester")
    b = _Builder()
    notes: list[str] = list(details.get("normalization_notes") or [])

    # R1 required information
    missing = list(details["field_completeness"]["missing"])
    fields_missing = bool(missing)
    if fields_missing:
        b.flag("missing_information", "R1", "§1", "Required request information missing: " + ", ".join(missing))
        b.ev("get_request_details", "Required request fields missing: " + "; ".join(missing), "Policy §1")

    # R2 requester evidence
    if requester:
        b.ev("get_request_details",
             f"Requester {requester['id']} {requester['name']} ({requester['department']}, {requester['level']}); "
             f"annual cost {money(req.annual_cost_usd)}; users {req.user_count if req.user_count else 'not provided'}; "
             f"data access {req.data_access_level or 'not provided'}",
             f"employees.csv:{requester['id']}")
    for note in notes:
        b.ev("get_request_details", f"Request field could not be used: {note}", "Policy §1")

    # R4 financial tier
    tier, tier_label = tier_roles(req.annual_cost_usd)
    for role in tier:
        b.role(role, "R4", "§4", f"Financial tier {tier_label}" + (
            f" for annual cost {money(req.annual_cost_usd)}" if req.annual_cost_usd is not None else ""))
    b.ev("evaluate_policy_rules",
         f"Annual cost {money(req.annual_cost_usd)} -> tier {tier_label} -> {', '.join(tier)}", "Policy §4")

    # R3 budget
    if budget.get("status") == "unverified":
        b.role("Finance", "R3", "§2", f"No budget record for {budget['department']}; Finance must verify funding")
        b.flag("budget_unverified", "R3", "§2", f"No software budget record for {budget['department']}")
        missing.append(GAP_NO_BUDGET)
        b.ev("check_budget", f"No software budget record found for department {budget['department']}",
             "department_budgets.csv")
    elif budget.get("status") in ("ok", "insufficient"):
        avail, cost = budget["available_usd"], budget["annual_cost"]
        if budget["status"] == "insufficient":
            b.role("Finance", "R3", "§2", f"Budget exception: request {money(cost)} exceeds available {money(avail)}")
            b.flag("budget_insufficient", "R3", "§2", f"Request {money(cost)} > available {money(avail)}")
            tail = f"exceeds budget by {money(cost - avail)}"
        else:
            tail = f"within budget ({money(avail - cost)} left)"
        b.ev("check_budget",
             f"{budget['department']} available software budget {money(avail)}; request {money(cost)} -> {tail}",
             budget["reference"])

    # R5 catalog overlap
    flagged = list(catalog.get("overlap_flag_candidates") or [])
    by_id = {c["software_id"]: c for c in catalog.get("candidates", [])}
    if flagged:
        names = ", ".join(f"{sid} {by_id[sid]['product_name']}" for sid in flagged)
        b.flag("existing_tool_overlap", "R5", "§3", f"Existing catalog product(s) in the same category/product: {names}")
    for c in catalog.get("candidates", [])[:4]:
        hist = c.get("purchase_history") or []
        hist_txt = f"; last purchase {hist[-1]['purchase_id']} {money(float(hist[-1]['annual_amount_usd']))}" if hist else ""
        b.ev("search_software_catalog",
             f"{c['software_id']} {c['product_name']} ({c['category']}, {c['scope']}, {c['licensed_seats']} seats, "
             f"{c['status']}) matches by {', '.join(c['match'])}"
             + (" -> overlap" if c["raises_overlap_flag"] else " -> evidence only, no overlap flag") + hist_txt,
             f"software_catalog.csv:{c['software_id']}")

    # R6 vendor evidence
    api_unavailable = False
    d = vendor["derived"] if vendor else None
    if vendor is None:
        b.role("Security", "R6", "§5", "Vendor unknown: no vendor security assessment can be checked")
    else:
        reg, api = vendor["registry"], vendor["api"]
        if reg:
            b.ev("get_vendor_status",
                 f"Registry {reg['vendor_id']} {reg['vendor_name']}: procurement {reg['procurement_status']}, "
                 f"security {reg['security_status'] or 'blank'} (review {reg['security_review_date'] or 'none'}"
                 + (f", {d['registry_age_days']} days before {ref.isoformat()}" if d["registry_age_days"] is not None else "")
                 + f"), legal terms {reg['legal_terms_status'] or 'blank'}",
                 f"vendors.csv:{reg['vendor_id']}")
        else:
            b.flag("vendor_not_registered", "R6", "§7", f"Vendor '{_short(vendor_name)}' is not in the vendor registry")
            b.ev("get_vendor_status", f"Vendor '{_short(vendor_name)}' not found in vendor registry: treated as new vendor with unknown terms",
                 "vendors.csv")
        if api["outcome"] == "ok":
            rec = api["record"] or {}
            b.ev("get_vendor_status",
                 f"Vendor-risk API: security_review_status {rec.get('security_review_status')}, "
                 f"last review {rec.get('last_review_date') or 'none'}, stores data outside region "
                 f"{'yes' if rec.get('stores_data_outside_region') else 'no'}, processes personal data "
                 f"{'yes' if rec.get('processes_personal_data') else 'no'}",
                 f"GET /vendor-risk/{vendor['vendor_name_queried']}")
        elif api["outcome"] == "not_found":
            missing.append(GAP_NOT_FOUND)
            b.ev("get_vendor_status", "Vendor-risk API has no record for this vendor (HTTP 404): no external assessment exists",
                 f"GET /vendor-risk/{vendor['vendor_name_queried']}")
        else:
            missing.append(GAP_VENDOR_UNAVAILABLE)
            b.flag("vendor_risk_unavailable", "R6", "§10",
                   "Vendor-risk service unavailable: security status could not be verified")
            b.ev("get_vendor_status",
                 f"Vendor-risk service unavailable ({_short(api.get('error') or 'error', 80)}; {api.get('attempts')} attempts): "
                 "security status and data residency could not be verified",
                 f"GET /vendor-risk/{vendor['vendor_name_queried']}")
            api_unavailable = True
        if d["expired"]:
            parts = []
            if d["registry_class"] == "approved" and (d["registry_age_days"] or 0) > REVIEW_VALID_DAYS:
                exp = parse_date(d["registry_review_date"]) + timedelta(days=REVIEW_VALID_DAYS)
                parts.append(f"registry review {d['registry_review_date']} expired on {exp.isoformat()} "
                             f"({d['registry_age_days']} days old)")
            if d["api_class"] == "expired":
                parts.append(f"vendor-risk API status expired (review {d['api_review_date'] or 'none'})")
            if d["api_class"] == "approved" and (d["api_age_days"] or 0) > REVIEW_VALID_DAYS:
                exp = parse_date(d["api_review_date"]) + timedelta(days=REVIEW_VALID_DAYS)
                parts.append(f"API review {d['api_review_date']} expired on {exp.isoformat()}")
            b.flag("vendor_review_expired", "R6", "§5", "; ".join(parts))
            b.ev("get_vendor_status", "Security review expired: " + "; ".join(parts), "Policy §5")
        if d["conflict"]:
            detail = (f"registry says {reg['security_status']} ({reg['security_review_date'] or 'no date'}) vs "
                      f"vendor-risk API says {api['record'].get('security_review_status')} "
                      f"({api['record'].get('last_review_date') or 'no date'})")
            b.flag("conflicting_vendor_evidence", "R6", "§5", detail)
            b.ev("get_vendor_status", "Conflicting vendor evidence: " + detail, "Policy §5")
        if not d["cleared"]:
            why = []
            if not d["registered"]:
                why.append("vendor not registered")
            elif d["registry_class"] != "approved":
                why.append(f"registry security status {reg['security_status'] or 'blank'}")
            elif not d["registry_current"] and not d["expired"]:
                why.append("registry review date missing or not current")
            if d["expired"]:
                why.append("review expired")
            if d["conflict"]:
                why.append("registry and vendor-risk API conflict")
            if api["outcome"] == "ok" and not (d["api_class"] == "approved" and is_current(d["api_age_days"])):
                why.append(f"vendor-risk API status {api['record'].get('security_review_status')}")
            b.role("Security", "R6", "§5",
                   "Vendor security assessment missing, expired, not completed or conflicting: " + "; ".join(why))

    # R7 data classes
    classes: dict[str, list[str]] = {}
    level_classes, recognized = data_classes_for_level(req.data_access_level)
    for c in level_classes:
        classes.setdefault(c, []).append(f"data_access_level '{_short(req.data_access_level)}'")
    if not recognized:
        b.flag("unrecognized_data_access_level", "R7", "§5",
               f"Data access level '{_short(req.data_access_level)}' is not recognised")
        b.role("Security", "R7", "§5", f"Unrecognised data access level '{_short(req.data_access_level)}'")
    for integ in req.requested_integrations or []:
        c = integration_class(integ)
        if c:
            classes.setdefault(c, []).append(f"integration '{_short(integ)}'")
    class_names = [c for c in ("source_code", "production_access", "confidential_documents", "employee_pii",
                               "customer_pii", "credentials") if c in classes]
    if class_names:
        desc = "; ".join(f"{c} ({', '.join(classes[c])})" for c in class_names)
        b.role("Security", "R7", "§5", f"Sensitive data/access: {desc}")
        b.ev("evaluate_policy_rules", f"Data classes: {desc}", "Policy §5, §6")
    pii = [c for c in class_names if c in ("employee_pii", "customer_pii")]
    if pii:
        b.role("Privacy", "R7", "§6", f"Processes {' and '.join(pii)}")

    # R8 cross-region
    sensitive = bool(pii) or "confidential_documents" in class_names
    cross_region = False
    if vendor and sensitive:
        if vendor["api"]["outcome"] == "ok" and d["stores_data_outside_region"] is True:
            cross_region = True
            b.role("Privacy", "R8", "§6", "Vendor stores data outside the region and the request involves sensitive data")
            b.role("Legal", "R8", "§7", "Material cross-region data issue (sensitive data stored outside the region)")
            b.ev("evaluate_policy_rules", "Material cross-region issue: vendor stores data outside region; request involves "
                 + ", ".join(c for c in class_names if c in ("employee_pii", "customer_pii", "confidential_documents")),
                 "Policy §6, §7")
        elif api_unavailable:
            b.role("Privacy", "R8", "§6", "Data residency unverified (vendor-risk service unavailable) with sensitive data")
    if api_unavailable:
        missing.append(GAP_RESIDENCY)

    # R9 legal
    if vendor:
        if d["new_vendor"] and req.annual_cost_usd is not None and req.annual_cost_usd >= 10000:
            b.role("Legal", "R9", "§7", f"New vendor with annual spend {money(req.annual_cost_usd)} (>= $10,000)")
        if not d["legal_terms_approved"]:
            b.role("Legal", "R9", "§7", f"Legal terms not approved/standard ({d['legal_terms_status'] or 'not registered'})")
    else:
        b.role("Legal", "R9", "§7", "Vendor unknown: legal terms cannot be confirmed")

    # R10 injection (never changes any other output)
    hits = list(details.get("injection_scan", {}).get("hits", []))
    if vendor:
        hits += vendor.get("injection_scan", {}).get("hits", [])
    hits += catalog.get("injection_scan", {}).get("hits", [])
    if hits:
        b.flag("prompt_injection_detected", "R10", "§9",
               "Embedded instructions found in business data; ignored")
        for h in hits[:3]:
            b.ev("evaluate_policy_rules", f"Possible prompt injection in {h['field']} (ignored): {h['excerpt']}", "Policy §9")

    # R11 assembly: specialist flags follow roles
    for role, flag in SPECIALIST_FLAG.items():
        if role in b.roles:
            b.flag(flag, "R11", {"Security": "§5", "Privacy": "§6", "Legal": "§7"}[role],
                   "; ".join(r.detail for r in b.roles[role]))
    for role, reasons in b.roles.items():
        if role in ("Security", "Privacy", "Legal", "Finance") and role not in tier:
            b.ev("evaluate_policy_rules", f"{role} review required: " + "; ".join(r.detail for r in reasons),
                 "Policy " + ", ".join(dict.fromkeys(r.policy_ref for r in reasons)))

    hints = {
        "Manager": requester.get("manager_hint") if requester else None,
        "Department Head": requester.get("department_head_hint") if requester else None,
    }
    approvals = [RoleApproval(role=r, reasons=b.roles[r], name_hint=hints.get(r)) for r in order_roles(b.roles)]
    return PolicyResult(
        request_id=request_id,
        reference_date=ref.isoformat(),
        policy_version=get_policy_version(),
        approvals=approvals,
        flags=order_flags(b.flags),
        flag_reasons=b.flags,
        missing_information=list(dict.fromkeys(missing)),
        request_fields_missing=fields_missing,
        tier=tier_label,
        data_classes=class_names,
        data_class_sources=classes,
        overlap_flag_candidates=flagged,
        requester=requester,
        budget=budget,
        vendor=vendor or {},
        injection={"detected": bool(hits), "hits": hits},
        evidence=b.evidence,
        notes=notes,
    )


# ================================================================ R12 decision precedence
def decide(policy: PolicyResult, final_roles: list[str], final_flags: list[str],
           overlap_assessment: list | None = None, agent_decision: str | None = None) -> str:
    """R12. Deviation (docs/architecture.md C1): use_existing_tool also requires the agent's own
    decision to be use_existing_tool, so an inconsistent overlap entry cannot override a decision
    the agent did not make."""
    if policy.request_fields_missing:
        return "request_clarification"
    if agent_decision is not None and agent_decision != "use_existing_tool":
        overlap_assessment = None
    for a in overlap_assessment or []:
        sid = a.software_id if hasattr(a, "software_id") else a.get("software_id")
        covers = a.covers_stated_need if hasattr(a, "covers_stated_need") else a.get("covers_stated_need")
        if sid in policy.overlap_flag_candidates and covers is True:
            return "use_existing_tool"
    if ({"Security", "Privacy", "Legal"} & set(final_roles)
            or {"budget_insufficient", "budget_unverified", "conflicting_vendor_evidence"} & set(final_flags)):
        return "route_for_specialist_review"
    return "route_for_approval"


__all__ = ["evaluate", "decide", "tier_roles", "derive_vendor", "classify_registry", "classify_api",
           "data_classes_for_level", "integration_class", "request_details", "budget_check", "catalog_search",
           "vendor_status", "FLAG_ORDER"]
