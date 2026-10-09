"""Procurement Request Copilot: reviewer UI (Streamlit). Start with `python run_local.py`.

Safety rule for this file: business text (request fields, vendor notes, API text, LLM wording) is untrusted.
It is rendered only through `esc()` (Markdown-escaped, no HTML), `st.text`, `st.code` or `st.dataframe`,
so it can never become a link, image, formula or markup.
"""
from __future__ import annotations

import re
from datetime import datetime

import pandas as pd
import streamlit as st

import src  # noqa: F401  (loads .env)
from src import review_service as svc

st.set_page_config(page_title="Procurement Request Copilot", layout="wide")

FLAG_HELP = {
    "existing_tool_overlap": "A licensed catalog product shares the category or product. Check whether it already meets the need.",
    "budget_insufficient": "Annual cost exceeds the department's available software budget. Finance must approve an exception.",
    "budget_unverified": "No budget record exists for the department, so funding could not be checked.",
    "security_review_required": "Sensitive data or access, or an incomplete vendor security assessment, requires Security review.",
    "privacy_review_required": "Personal data, or sensitive data possibly stored outside the region, requires Privacy review.",
    "legal_review_required": "New vendor at $10,000 or more, non-standard terms, or a cross-region issue requires Legal review.",
    "vendor_review_expired": "The vendor security assessment is more than 365 days old, or marked expired.",
    "conflicting_vendor_evidence": "The internal registry and the vendor-risk service disagree. Neither is trusted silently.",
    "vendor_risk_unavailable": "The vendor-risk service could not be reached. No favorable status is assumed.",
    "vendor_not_registered": "The vendor is not in the registry, so it is treated as new with unknown terms.",
    "prompt_injection_detected": "Business data contained instructions aimed at the AI. They were ignored.",
    "missing_information": "Required request fields are missing. The requester must supply them.",
    "unrecognized_data_access_level": "The declared data access level is not recognised. Security must classify it.",
    "llm_unavailable": "AI analysis was unavailable. The result comes from the deterministic rules only.",
}
FLAG_COLOR = {
    "prompt_injection_detected": "red", "conflicting_vendor_evidence": "red", "vendor_risk_unavailable": "red",
    "budget_insufficient": "red", "missing_information": "red", "vendor_review_expired": "red", "llm_unavailable": "red",
    "security_review_required": "orange", "privacy_review_required": "orange", "legal_review_required": "orange",
    "budget_unverified": "orange", "vendor_not_registered": "orange", "unrecognized_data_access_level": "orange",
}
DECISION_COLOR = {"route_for_approval": "green", "route_for_specialist_review": "orange",
                  "request_clarification": "red", "use_existing_tool": "violet"}
SOURCE_LABEL = {"get_request_details": "Request", "check_budget": "Budget", "search_software_catalog": "Catalog",
                "get_vendor_status": "Vendor", "evaluate_policy_rules": "Policy engine",
                "lookup_policy_section": "Policy text", "copilot_analysis": "Copilot"}
ARCH_LABEL = {"workflow": "Workflow + 1 LLM", "single": "Single agent", "staged": "Staged: analyst + reviewer"}
ARCHS = list(ARCH_LABEL)
DEFAULT_ARCH = "single"  # the configuration chosen in docs/DECISION_MEMO.md

_MD_SPECIAL = re.compile(r"([\\`*_{}\[\]()#+\-.!|~<>$:=&\"'])")


def esc(value: object) -> str:
    """Escape untrusted text for st.markdown: every Markdown/LaTeX/directive character is backslash-escaped."""
    text = "" if value is None else str(value)
    return _MD_SPECIAL.sub(r"\\\1", " ".join(text.split()))


def code(value: object) -> str:
    """Text for an inline code span (rendered literally); only backticks need removing."""
    return str(value or "").replace("`", "")


def money(v) -> str | None:
    if v is None or v == "":
        return None
    return f"${float(v):,.2f}".replace(".00", "")


def tools_text(tools: list[str], cached: int = 0) -> str:
    counts = {}
    for name in tools:
        counts[name] = counts.get(name, 0) + 1
    text = ", ".join(f"{n} ×{k}" if k > 1 else n for n, k in counts.items()) or "none"
    return text + (f" (+{cached} cached)" if cached else "")


def duration(ms: float | None) -> str:
    if ms is None:
        return "—"
    return f"{ms / 1000:.1f} s" if ms >= 1000 else f"{ms:.0f} ms"


def missing(v) -> str:
    return "missing" if v is None or v == "" else str(v)


# ---------------------------------------------------------------- state
ss = st.session_state
ss.setdefault("arch", DEFAULT_ARCH)
ss.setdefault("compare", {})
ss.setdefault("flash", None)
if "selected" not in ss:
    ss.selected = st.query_params.get("request")


# ---------------------------------------------------------------- dialogs
@st.dialog("Confirm action")
def confirm_action(action: str, view: dict, requester_name: str | None) -> None:
    override = action != view["recommended_action"]
    st.markdown(f"**{svc.ACTION_LABEL[action]}**")
    if override:
        st.warning(f"Override: the copilot recommends \"{svc.ACTION_LABEL[view['recommended_action']]}\".")
    with st.container(border=True):
        st.markdown(esc(svc.consequence(action, view, requester_name)))
    reason = st.text_area("Reason (required)" if override else "Reason (optional)", key="action_reason",
                          placeholder="Why are you choosing a different action?" if override
                          else "Optional note for the audit log")
    st.caption("Routing is simulated: actions are recorded in the audit log only.")
    c1, c2 = st.columns(2)
    if c1.button("Cancel", width="stretch"):
        st.rerun()
    if c2.button(f"Confirm: {svc.ACTION_LABEL[action]}", type="primary", width="stretch"):
        try:
            entry = svc.record_action(view["decision"]["request_id"], view["trace_summary"]["run_id"], action, reason)
        except svc.ActionRejected as exc:
            st.error(str(exc))
            return
        ss.flash = entry
        st.rerun()


@st.dialog("New purchase request", width="large")
def new_request() -> None:
    st.caption("Every field may be left blank. The copilot will ask the requester for whatever is missing.")
    with st.form("new_request_form", border=False):
        requester_id = st.text_input("Requester employee ID (e.g. E001)")
        c1, c2 = st.columns(2)
        product = c1.text_input("Product")
        vendor = c2.text_input("Vendor")
        c1, c2 = st.columns(2)
        category = c1.text_input("Category")
        cost = c2.number_input("Annual cost (USD)", min_value=0.0, value=None, step=100.0)
        c1, c2 = st.columns(2)
        users = c1.number_input("Users / licences", min_value=1, value=None, step=1)
        urgency = c2.selectbox("Urgency", ["", "low", "normal", "high", "urgent"], format_func=lambda u: u or "Not set")
        justification = st.text_area("Business justification")
        level = st.selectbox("Data access level", ["", "none", "internal", "internal_documents", "internal_marketing",
                                                   "public", "confidential_documents", "customer_pii", "employee_pii",
                                                   "source_code", "production_telemetry", "credentials"],
                             format_func=lambda v: v or "Not provided")
        integrations = st.text_input("Integrations (comma-separated)")
        no_integrations = st.checkbox("No integrations needed")
        submitted = st.form_submit_button("Submit request", type="primary")
    if submitted:
        fields = {
            "requester_id": requester_id, "product_name": product, "vendor_name": vendor, "category": category,
            "annual_cost_usd": cost, "user_count": int(users) if users else None,
            "business_justification": justification, "data_access_level": level or None, "urgency": urgency or None,
            "requested_integrations": [] if no_integrations else
            ([s.strip() for s in integrations.split(",") if s.strip()] or None),
        }
        try:
            record = svc.submit_request(fields)
        except Exception as exc:  # pydantic validation
            st.error(f"Could not submit: {exc}")
            return
        ss.selected = record["request_id"]
        st.toast(f"{record['request_id']} added to the queue")
        st.rerun()


# ---------------------------------------------------------------- header
health = svc.health()
h1, h2 = st.columns([3, 2], vertical_alignment="center")
with h1:
    st.title("Procurement Request Copilot")
    st.caption("Recommends the next step for each software purchase request. People make every approval.")
with h2:
    vendor_ok = health["vendor_api"] == "ok"
    llm = health["llm"]
    st.markdown(
        (":green[●] Vendor-risk service up" if vendor_ok else ":red[●] Vendor-risk service down") + " · "
        + (f":green[●] AI: {esc(llm['model'])}" if llm["configured"] else ":orange[●] AI off: rules only"))
    if st.button("New request"):
        new_request()

queue_col, case_col = st.columns([3, 7], gap="medium")

# ---------------------------------------------------------------- queue
with queue_col:
    st.subheader("Review queue")
    rows = svc.queue()
    if not rows:
        st.info("No purchase requests awaiting review")
    else:
        c1, c2 = st.columns([3, 2])
        term = c1.text_input("Search", placeholder="ID, product, vendor, person", label_visibility="collapsed")
        status = c2.selectbox("Status", ["All", "New", "Analysed", "Action recorded"], label_visibility="collapsed")
        counts = {s: sum(r["status"] == s for r in rows) for s in ("New", "Analysed", "Action recorded")}
        st.caption(f"{len(rows)} requests · {counts['New']} new · {counts['Analysed']} analysed · "
                   f"{counts['Action recorded']} actioned")
        shown = [r for r in rows if (status == "All" or r["status"] == status) and (
            not term or term.lower() in " ".join(str(r[k] or "") for k in
                                                 ("request_id", "product_name", "vendor_name", "requester", "department")).lower())]
        if not shown:
            st.info("No requests match this search or filter.")
        else:
            df = pd.DataFrame([{
                "Request": r["request_id"],
                "Product": r["product_name"] or "missing",
                "Status": r["status"],
            } for r in shown])
            event = st.dataframe(df, hide_index=True, width="stretch", on_select="rerun",
                                 selection_mode="single-row", key="queue_table",
                                 height=min(38 * len(df) + 40, 640))
            picked = event.selection.rows if event and event.selection else []
            if picked:
                new_sel = shown[picked[0]]["request_id"]
                if new_sel != ss.selected:
                    ss.selected = new_sel
                    st.query_params["request"] = new_sel
        st.caption("Select a row to open the request.")

# ---------------------------------------------------------------- case
with case_col:
    if not ss.selected:
        with st.container(border=True):
            st.subheader("Select a request to review")
            st.write("The copilot gathers budget, catalog, vendor and policy evidence, then recommends the next "
                     "step. You decide what happens.")
        st.stop()
    try:
        req = svc.get_request(ss.selected)
    except svc.NotFound:
        st.error("This request no longer exists. Select another one from the queue.")
        st.stop()
    r, who = req["request"], req["requester"]
    view = svc.latest_analysis(ss.selected)
    qstatus = next((q["status"] for q in rows if q["request_id"] == ss.selected), "New")

    # header + run bar
    with st.container(border=True):
        st.caption(esc(f"{r['request_id']} · {qstatus}"))
        st.markdown(f"### {esc(r.get('product_name') or 'Untitled request')}")
        st.markdown(esc(f"{r.get('vendor_name') or 'Vendor missing'}" + (f" · {r['category']}" if r.get("category") else "")))
        facts = {
            "REQUESTER": f"{who['name']} ({who['id']})" if who else
                         (f"{r['requester_id']}, not found" if r.get("requester_id") else None),
            "DEPARTMENT": who and who.get("department"),
            "ANNUAL COST": money(r.get("annual_cost_usd")),
            "USERS": r.get("user_count"),
            "URGENCY": r.get("urgency"),
        }
        for col, (k, v) in zip(st.columns(len(facts)), facts.items()):
            col.caption(k)
            col.markdown(f"**{esc(v)}**" if v not in (None, "") else ":red[*missing*]")

        b = st.columns([3, 2, 2], vertical_alignment="bottom")
        ss.arch = b[0].radio("Architecture", ARCHS, format_func=ARCH_LABEL.get, horizontal=True,
                             index=ARCHS.index(ss.arch) if ss.arch in ARCHS else 0)
        do_analyse = b[1].button("Re-analyse" if view else "Analyse request", type="primary", width="stretch")
        do_compare = b[2].button("Compare architectures", width="stretch",
                                 help="Runs all three configurations on this request (about 7 LLM calls).")

    if do_analyse:
        try:
            with st.spinner("Gathering evidence: budget · catalog · vendor · policy…"):
                view = svc.analyse(ss.selected, ss.arch)
        except Exception as exc:  # never show a traceback to the reviewer
            st.error(f"Analysis could not be completed ({type(exc).__name__}). Try again; if it keeps failing, "
                     "check that `python run_local.py` is running and see the terminal output.")
        else:
            ss.compare.pop(ss.selected, None)
            st.rerun()
    if do_compare:
        try:
            with st.spinner("Running all three configurations on this request…"):
                order = [a for a in ARCHS if a != ss.arch] + [ss.arch]  # selected one runs last = shown below
                both = {a: svc.analyse(ss.selected, a) for a in order}
        except Exception as exc:
            st.error(f"Comparison could not be completed ({type(exc).__name__}). Try again.")
        else:
            ss.compare[ss.selected] = both
            st.rerun()

    if ss.flash and ss.flash.get("request_id") == ss.selected:
        ts = datetime.fromisoformat(ss.flash["timestamp"]).astimezone().strftime("%d %b %Y, %H:%M")
        st.success(f"Recorded at {ts} · outcome pending")
        st.toast(f"{svc.ACTION_LABEL[ss.flash['action']]} recorded")
        ss.flash = None

    if not view:
        st.info("Not analysed yet. Choose an architecture and select **Analyse request**.")

    if ss.selected in ss.compare:
        both = ss.compare[ss.selected]
        with st.container(border=True):
            st.markdown("**Architecture comparison on this request**")
            st.dataframe(pd.DataFrame({
                "": ["Decision", "Approvals", "Flags", "LLM / tool calls", "Active latency"],
                **{ARCH_LABEL[a]: [svc.DECISION_LABEL[both[a]["decision_type"]],
                                   ", ".join(both[a]["decision"]["required_approvals"]),
                                   ", ".join(both[a]["decision"]["risk_flags"]) or "none",
                                   f"{both[a]['trace_summary']['llm_calls']} / {both[a]['trace_summary']['tool_calls']}",
                                   f"{both[a]['trace_summary']['latency_active_ms'] / 1000:.1f} s"]
                   for a in ARCHS if a in both},
            }), hide_index=True, width="stretch")
            st.caption("The most recent run is shown below.")

    if view:
        d, t = view["decision"], view["trace_summary"]

        # banners
        for b_ in view["banners"]:
            box = {"danger": st.error, "warn": st.warning}.get(b_["level"], st.info)
            box(f"**{b_['title']}**" + (f"  \n{esc(b_['detail'])}" if b_.get("detail") else ""))
            if b_.get("compare"):
                cc = st.columns(len(b_["compare"]))
                for col, (k, v) in zip(cc, b_["compare"].items()):
                    with col.container(border=True):
                        st.caption(k)
                        st.markdown(f"**{esc(v)}**")
            for x in b_.get("excerpts", []):
                st.code(x, language=None, wrap_lines=True)

        # decision summary
        with st.container(border=True):
            color = DECISION_COLOR.get(view["decision_type"], "gray")
            st.markdown(f":{color}-background[**{svc.DECISION_LABEL[view['decision_type']]}**] "
                        f"{len(d['required_approvals'])} approvals · {len(d['risk_flags'])} flags · human review required")
            sentence = d["recommendation"].split(":", 1)[1].strip() if ":" in d["recommendation"] else d["recommendation"]
            st.markdown(f"#### {esc(sentence)}")
            st.caption("NEXT STEP")
            st.markdown(esc(d["next_step"]))
            engine = t["model"] if t["path"] == "llm" else ("rules only (AI unavailable)" if t["path"] == "fallback" else t["path"])
            wait = f" (+{t['llm_wait_ms'] / 1000:.1f} s rate-limit wait)" if t.get("llm_wait_ms") else ""
            st.caption(esc(f"Architecture: {t['architecture']} · Engine: {engine} · {t['llm_calls']} LLM calls · "
                           f"{t['tool_calls']} tool calls · {t['latency_active_ms'] / 1000:.1f} s active{wait} · "
                           f"run {t['run_id']}"))

        # approvals | flags + missing
        left, right = st.columns(2, gap="medium")
        with left, st.container(border=True):
            st.markdown(f"**Approvals required ({len(view['approvals'])})**")
            for title, group in (("Business approvals", [a for a in view["approvals"] if a["role"] not in svc.SPECIALISTS]),
                                 ("Specialist reviews", [a for a in view["approvals"] if a["role"] in svc.SPECIALISTS])):
                if not group:
                    continue
                st.caption(title.upper())
                for a in group:
                    hint = f" · {esc(a['name_hint'])}" if a.get("name_hint") else ""
                    lines = [f"**{esc(a['role'])}**{hint}"]
                    for reason in a["reasons"]:
                        tag = (":violet-background[AI added]" if reason["rule"] == "AI"
                               else f"`{code(reason['rule'])} {code(reason['policy_ref'])}`")
                        lines.append(f"- {tag} {esc(reason['detail'])}")
                    st.markdown("\n".join(lines))
        with right:
            with st.container(border=True):
                st.markdown(f"**Risk flags ({len(d['risk_flags'])})**")
                if not d["risk_flags"]:
                    st.caption("No risk flags.")
                for flag in d["risk_flags"]:
                    color = FLAG_COLOR.get(flag)
                    name = f":{color}-background[{esc(flag)}]" if color else f":gray-background[{esc(flag)}]"
                    st.markdown(f"{name} {esc(FLAG_HELP.get(flag, ''))}")
            with st.container(border=True):
                st.markdown("**Missing or unverified**")
                any_items = False
                for title, items in (("Missing from the request", view["missing_fields"]),
                                     ("Could not verify", view["could_not_verify"]),
                                     ("Questions for the reviewer (AI)", view["questions_for_reviewer"])):
                    if items:
                        any_items = True
                        st.markdown(f"*{title}*\n" + "\n".join(f"- {esc(m)}" for m in items))
                if not any_items:
                    st.caption("Nothing missing or unverifiable.")

        # evidence
        with st.container(border=True):
            st.markdown(f"**Evidence ({len(d['evidence'])})**")
            kinds = ["All"] + [k for k in ("Rule", "Tool", "AI analysis") if any(e["kind"] == k for e in view["evidence"])]
            pick = st.radio("Show", kinds, horizontal=True, label_visibility="collapsed", key=f"ev_{t['run_id']}")
            ev = [e for e in view["evidence"] if pick == "All" or e["kind"] == pick]
            st.dataframe(pd.DataFrame([{
                "Source": SOURCE_LABEL.get(e["source"], e["source"]), "Finding": e["finding"],
                "Reference": e.get("reference") or "—", "Kind": e["kind"],
            } for e in ev]), hide_index=True, width="stretch",
                column_config={"Source": st.column_config.TextColumn(width="small"),
                               "Finding": st.column_config.TextColumn(width="large"),
                               "Reference": st.column_config.TextColumn(width="medium"),
                               "Kind": st.column_config.TextColumn(width="small")})
            st.caption("Hover or double-click a cell to read a long finding in full.")

        # how this was decided (C9): steps, guardrail corrections, AI proposal vs final
        with st.expander("How this was decided", expanded=False):
            st.dataframe(pd.DataFrame([{
                "Step": i, "Who": s_["actor"], "Why": s_["why"],
                "Tools": tools_text(s_["tools"], s_["cached_lookups"]),
                "LLM calls": str(s_["llm_calls"]), "Tokens": "—" if s_["tokens"] is None else f"{s_['tokens']:,}",
                "Time": duration(s_["ms"]), "Note": s_["note"] or "",
            } for i, s_ in enumerate(view["steps"], 1)]), hide_index=True, width="stretch",
                column_config={"Why": st.column_config.TextColumn(width="large"),
                               "Tools": st.column_config.TextColumn(width="medium")})
            st.caption(esc(f"Run total: {t['llm_calls']} LLM calls · {(t.get('tokens') or {}).get('total_tokens', 0):,} "
                           f"tokens · {t['tool_calls']} tool calls. A dash means the run did not record that stage "
                           "separately."))
            st.markdown(f"**Guardrail corrections ({len(view['corrections'])})**")
            if view["corrections"]:
                st.dataframe(pd.DataFrame([{"Correction": c["label"], "Detail": c["detail"]}
                                           for c in view["corrections"]]), hide_index=True, width="stretch")
            else:
                st.caption("None: code accepted the AI proposal as it was, or no AI was involved.")
            rvf = view["raw_vs_final"]
            if rvf:
                st.markdown("**AI proposal vs final result**")
                st.dataframe(pd.DataFrame({
                    "": ["Decision", "Approvals", "AI-reported data classes"],
                    "AI proposed": [svc.DECISION_LABEL.get(rvf["ai_decision"], rvf["ai_decision"]),
                                    ", ".join(rvf["ai_approvals"]) or "none",
                                    ", ".join(rvf["ai_implied_data_classes"]) or "none"],
                    "Final (after code)": [svc.DECISION_LABEL.get(rvf["final_decision"], rvf["final_decision"]),
                                           ", ".join(rvf["final_approvals"]) or "none", "—"],
                }), hide_index=True, width="stretch")

        # decide + request details
        dleft, dright = st.columns(2, gap="medium")
        with dleft, st.container(border=True):
            st.markdown("**Your decision**")
            for action in svc.ACTIONS:
                rec = action == view["recommended_action"]
                label = svc.ACTION_LABEL[action] + (" (recommended)" if rec else "")
                if st.button(label, key=f"act_{action}", type="primary" if rec else "secondary", width="stretch"):
                    ss.pop("action_reason", None)
                    confirm_action(action, view, who["name"] if who else None)
            st.caption("Choosing a different action than recommended needs a reason. "
                       "Routing is simulated: actions are recorded in the audit log only.")
            entries = svc.audit_entries(ss.selected)
            st.markdown(f"**Audit history ({len(entries)})**")
            if entries:
                st.dataframe(pd.DataFrame([{
                    "Time": datetime.fromisoformat(e["timestamp"]).astimezone().strftime("%d %b %H:%M"),
                    "Action": svc.ACTION_LABEL.get(e["action"], e["action"]),
                    "Copilot said": svc.DECISION_LABEL.get(e["copilot_decision_type"], e["copilot_decision_type"]),
                    "Arch.": e["architecture"],
                    "Override": "yes" if e["override"] else "no",
                    "Reason": e.get("reason") or "",
                } for e in reversed(entries)]), hide_index=True, width="stretch")
            else:
                st.caption("No actions recorded yet.")

    # request details (always available)
    container = dright if view else st
    with container.container(border=True):
        st.markdown("**Request details**")
        integ = r.get("requested_integrations")
        level = r.get("data_access_level")
        details = {
            "Manager": who and who.get("manager_hint"),
            "Department head": who and who.get("department_head_hint"),
            "Data access level": level if level and str(level).lower() != "unknown" else None,
            "Integrations": None if integ is None else (", ".join(integ) if integ else "None"),
            "Users / licences": r.get("user_count"),
        }
        st.dataframe(pd.DataFrame({"Field": list(details), "Value": [missing(v) for v in details.values()]}),
                     hide_index=True, width="stretch")
        st.caption("REQUESTER TEXT (UNTRUSTED)")
        st.text(r.get("business_justification") or "missing")
