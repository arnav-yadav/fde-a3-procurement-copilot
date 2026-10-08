"use strict";
// Reviewer UI. All business text is inserted as text nodes (via el()/textContent); innerHTML is never used.

const FLAG_HELP = {
  existing_tool_overlap: "A licensed catalog product shares the category or product. Check whether it already meets the need.",
  budget_insufficient: "Annual cost exceeds the department's available software budget. Finance must approve an exception.",
  budget_unverified: "No budget record exists for the department, so funding could not be checked.",
  security_review_required: "Sensitive data or access, or an incomplete vendor security assessment, requires Security review.",
  privacy_review_required: "Personal data, or sensitive data possibly stored outside the region, requires Privacy review.",
  legal_review_required: "New vendor at $10,000 or more, non-standard terms, or a cross-region issue requires Legal review.",
  vendor_review_expired: "The vendor security assessment is more than 365 days old, or marked expired.",
  conflicting_vendor_evidence: "The internal registry and the vendor-risk service disagree. Neither is trusted silently.",
  vendor_risk_unavailable: "The vendor-risk service could not be reached. No favorable status is assumed.",
  vendor_not_registered: "The vendor is not in the registry, so it is treated as new with unknown terms.",
  prompt_injection_detected: "Business data contained instructions aimed at the AI. They were ignored.",
  missing_information: "Required request fields are missing. The requester must supply them.",
  unrecognized_data_access_level: "The declared data access level is not recognised. Security must classify it.",
  llm_unavailable: "AI analysis was unavailable. The result comes from the deterministic rules only.",
};
const FLAG_SEVERITY = {
  prompt_injection_detected: "risk", conflicting_vendor_evidence: "risk", vendor_risk_unavailable: "risk",
  budget_insufficient: "risk", missing_information: "risk", vendor_review_expired: "risk", llm_unavailable: "risk",
  security_review_required: "caution", privacy_review_required: "caution", legal_review_required: "caution",
  budget_unverified: "caution", vendor_not_registered: "caution", unrecognized_data_access_level: "caution",
};
const DECISION_LABEL = {
  route_for_approval: "Route for approval",
  route_for_specialist_review: "Route for specialist review",
  request_clarification: "Request clarification",
  use_existing_tool: "Use existing tool",
};
const ACTION_LABEL = {
  send_for_approvals: "Send for approvals",
  request_clarification: "Request clarification",
  suggest_existing_tool: "Suggest existing tool",
  hold_manual_review: "Hold for manual review",
};
const SOURCE_LABEL = {
  get_request_details: "Request", check_budget: "Budget", search_software_catalog: "Catalog",
  get_vendor_status: "Vendor", evaluate_policy_rules: "Policy engine", lookup_policy_section: "Policy text",
  copilot_analysis: "Copilot",
};
const ROLE_CODE = { Manager: "M", "Department Head": "DH", Finance: "F", CFO: "CFO", Procurement: "P", Security: "S", Privacy: "Pr", Legal: "L" };
const SPECIALISTS = new Set(["Security", "Privacy", "Legal"]);
const STEPS = ["Request", "Budget", "Catalog", "Vendor", "Policy", "Recommendation"];

const state = { queue: [], selected: null, request: null, result: null, arch: "single", busy: false,
  evFilter: "All", extra: null, lastFocus: null };

// ---------------------------------------------------------------- DOM helpers
function el(tag, props, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else if (k === "disabled" || k === "checked" || k === "selected") node[k] = Boolean(v);
    else node.setAttribute(k, v === true ? "" : v);
  }
  append(node, children);
  return node;
}
function append(node, children) {
  for (const c of [children].flat(Infinity)) {
    if (c === null || c === undefined || c === false || c === "") continue;
    node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return node;
}
function setChildren(node, ...children) { node.replaceChildren(); return append(node, children); }
const $ = (id) => document.getElementById(id);
const money = (v) => (v === null || v === undefined || v === "") ? null
  : "$" + Number(v).toLocaleString("en-US", { maximumFractionDigits: 2 });
const orMissing = (v) => (v === null || v === undefined || v === "" ? el("span", { class: "missing" }, "missing") : String(v));
const fmtTime = (iso) => new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });

async function api(path, opts = {}) {
  const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined });
  let data = null;
  try { data = await res.json(); } catch (_) { /* empty body */ }
  if (!res.ok) {
    const d = data && data.detail;
    throw new Error(typeof d === "string" ? d : (d ? JSON.stringify(d) : res.statusText));
  }
  return data;
}

let toastTimer = null;
function toast(msg) {
  const t = $("toast");
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, 4000);
}

// ---------------------------------------------------------------- health
async function loadHealth() {
  try {
    const h = await api("/api/health");
    $("dot-vendor").className = "dot " + (h.vendor_api === "ok" ? "ok" : "down");
    $("health-vendor").textContent = h.vendor_api === "ok" ? "Vendor-risk service up" : "Vendor-risk service down";
    $("dot-llm").className = "dot " + (h.llm.configured ? "ok" : "warn");
    $("health-llm").textContent = h.llm.configured ? `AI: ${h.llm.model}` : "AI off: rules only";
  } catch (e) {
    $("dot-vendor").className = "dot down";
    $("health-vendor").textContent = "App not reachable";
  }
}

// ---------------------------------------------------------------- queue
async function loadQueue() {
  const body = $("queue-body");
  if (!state.queue.length) setChildren(body, el("div", { class: "state" }, "Loading requests…"));
  try {
    state.queue = await api("/api/requests");
  } catch (e) {
    setChildren(body, el("div", { class: "state error" }, `Could not load requests: ${e.message}. `,
      el("button", { class: "btn btn-small", type: "button", onclick: loadQueue }, "Retry")));
    return;
  }
  renderQueue();
}

function renderQueue() {
  const body = $("queue-body");
  const term = $("q-search").value.trim().toLowerCase();
  const status = $("q-filter").value;
  const counts = { New: 0, Analysed: 0, "Action recorded": 0 };
  state.queue.forEach((r) => { counts[r.status] = (counts[r.status] || 0) + 1; });
  setChildren($("q-counts"), `${state.queue.length} requests`, el("span", {}, `${counts.New} new`),
    el("span", {}, `${counts.Analysed} analysed`), el("span", {}, `${counts["Action recorded"]} actioned`));

  if (!state.queue.length) {
    setChildren(body, el("div", { class: "state" }, "No purchase requests awaiting review"));
    return;
  }
  const rows = state.queue.filter((r) => (!status || r.status === status) && (!term ||
    [r.request_id, r.product_name, r.vendor_name, r.requester, r.department].join(" ").toLowerCase().includes(term)));
  if (!rows.length) {
    setChildren(body, el("div", { class: "state" }, "No requests match this search or filter."));
    return;
  }
  setChildren(body, rows.map((r) => el("button", {
    class: "qrow", type: "button", "aria-current": r.request_id === state.selected ? "true" : "false",
    onclick: () => selectRequest(r.request_id),
  },
  el("span", { class: "l1" }, el("span", { class: "id" }, r.request_id),
    el("span", { class: "pill status-" + r.status.split(" ")[0] }, r.status)),
  el("span", { class: "l2" }, (r.product_name || "Product missing") + " · " + (r.vendor_name || "vendor missing")),
  el("span", { class: "l3" },
    el("span", {}, `${r.requester || "Unknown requester"} · ${r.department || "no department"}`),
    el("span", { class: "num" }, money(r.annual_cost_usd) || "cost missing")),
  (r.decision_type || ["high", "urgent"].includes((r.urgency || "").toLowerCase())) ? el("span", { class: "chips" },
    r.decision_type ? el("span", { class: "pill d-" + r.decision_type }, DECISION_LABEL[r.decision_type]) : null,
    ["high", "urgent"].includes((r.urgency || "").toLowerCase()) ? el("span", { class: "pill urgent" }, r.urgency) : null) : null,
  )));
}

// ---------------------------------------------------------------- request detail
async function selectRequest(id) {
  state.selected = id;
  state.result = null;
  state.extra = null;
  state.evFilter = "All";
  if (location.hash.slice(1) !== id) history.replaceState(null, "", "#" + encodeURIComponent(id));
  renderQueue();
  setChildren($("detail"), el("div", { class: "panel state" }, "Loading request…"));
  try {
    const data = await api("/api/requests/" + encodeURIComponent(id));
    state.request = data;
    state.result = data.latest_analysis || null;
  } catch (e) {
    setChildren($("detail"), el("div", { class: "panel state error" }, `Could not load ${id}: ${e.message}`));
    return;
  }
  renderDetail();
  if (matchMedia("(max-width: 900px)").matches) $("detail").scrollIntoView({ block: "start" });
}

function caseHeader() {
  const r = state.request.request;
  const who = state.request.requester;
  const qrow = state.queue.find((q) => q.request_id === r.request_id);
  const fact = (k, v) => el("div", { class: "fact" }, el("span", { class: "label" }, k), el("span", { class: "v" }, orMissing(v)));
  const seg = el("div", { class: "seg", role: "group", "aria-label": "Architecture" },
    [["single", "Single agent"], ["staged", "Staged: analyst + reviewer"]].map(([v, label]) =>
      el("button", { type: "button", "aria-pressed": state.arch === v ? "true" : "false", disabled: state.busy,
        onclick: () => { state.arch = v; renderDetail(); } }, label)));
  return el("div", { class: "panel" },
    el("div", { class: "casehead" },
      el("div", { class: "title" },
        el("div", { class: "id" }, `${r.request_id}${qrow ? " · " + qrow.status : ""}`),
        el("h2", {}, r.product_name || "Untitled request"),
        el("div", { class: "muted" }, `${r.vendor_name || "Vendor missing"}${r.category ? " · " + r.category : ""}`)),
      el("div", { class: "facts" },
        fact("Requester", who ? `${who.name} (${who.id})` : (r.requester_id ? `${r.requester_id}, not found` : null)),
        fact("Department", who && who.department),
        fact("Annual cost", money(r.annual_cost_usd)),
        fact("Users", r.user_count),
        fact("Urgency", r.urgency))),
    el("div", { class: "runbar" },
      seg,
      el("button", { class: "btn btn-primary", type: "button", disabled: state.busy, onclick: analyse },
        state.result ? "Re-analyse" : "Analyse request"),
      el("button", { class: "linkbtn", type: "button", disabled: state.busy, onclick: compareBoth }, "Compare both architectures"),
      el("span", { class: "hint" }, state.result
        ? `Showing ${state.result.trace_summary.architecture} result from run ${state.result.trace_summary.run_id}`
        : "Not analysed yet")));
}

function requestPanel() {
  const r = state.request.request;
  const who = state.request.requester;
  const integ = r.requested_integrations;
  const integText = integ === null || integ === undefined ? null : (integ.length ? integ.join(", ") : "None");
  const level = r.data_access_level && String(r.data_access_level).toLowerCase() !== "unknown" ? r.data_access_level : null;
  const rows = [
    ["Manager", who && who.manager_hint],
    ["Department head", who && who.department_head_hint],
    ["Data access level", level],
    ["Integrations", integText],
    ["Users / licences", r.user_count],
  ];
  return el("div", { class: "panel" },
    el("span", { class: "label" }, "Request details"),
    el("div", { class: "kv" }, rows.map(([k, v]) => [el("div", { class: "k" }, k), el("div", { class: "v" }, orMissing(v))])),
    el("div", { class: "quote" },
      el("span", { class: "label" }, "Requester text (untrusted)"),
      el("p", {}, r.business_justification || el("span", { class: "missing" }, "missing"))));
}

function progressPanel() {
  const steps = el("div", { class: "steps" }, STEPS.map((s, i) => el("span", { class: "step" + (i === 0 ? " on" : ""), "data-i": i }, s)));
  let i = 0;
  const timer = setInterval(() => {
    if (!state.busy || !steps.isConnected) { clearInterval(timer); return; }
    i = Math.min(i + 1, STEPS.length - 1);
    steps.querySelectorAll(".step").forEach((s, k) => s.classList.toggle("on", k <= i));
  }, 2500);
  return el("div", { class: "panel progress" },
    el("strong", {}, "Gathering evidence: budget · catalog · vendor · policy…"),
    el("div", { class: "bar" }, el("i", {})), steps,
    el("span", { class: "muted" }, "Usually 5–30 seconds. Free-tier rate limits can add waiting time."));
}

function renderDetail() {
  if (!state.request) return;
  const parts = [caseHeader()];
  if (state.busy) parts.push(progressPanel());
  if (state.extra) parts.push(state.extra);
  if (state.result && !state.busy) parts.push(...resultView(state.result));
  else if (!state.busy) parts.push(requestPanel());
  setChildren($("detail"), parts);
}

async function analyse() {
  if (state.busy) return;
  state.busy = true;
  state.extra = null;
  renderDetail();
  try {
    state.result = await api("/api/analyze", { method: "POST", body: { request_id: state.selected, architecture: state.arch } });
  } catch (e) {
    state.extra = el("div", { class: "panel state error" }, `Analysis failed: ${e.message}. `,
      el("button", { class: "btn btn-small", type: "button", onclick: analyse }, "Retry"));
  }
  state.busy = false;
  renderDetail();
  loadQueue();
}

// ---------------------------------------------------------------- result
function banner(b) {
  const kind = { warn: "Check", danger: "Alert", info: "Note" }[b.level] || "Note";
  return el("div", { class: "banner " + b.level, role: b.level === "danger" ? "alert" : null },
    el("div", { class: "t" }, el("span", { class: "kind" }, kind), b.title),
    b.detail ? el("div", {}, b.detail) : null,
    b.compare ? el("div", { class: "compare" }, Object.entries(b.compare).map(([k, v]) =>
      el("div", {}, el("span", { class: "label" }, k), el("div", { class: "v" }, v)))) : null,
    (b.excerpts || []).map((x) => el("div", { class: "excerpt" }, x)));
}

function decisionPanel(res) {
  const t = res.trace_summary;
  const d = res.decision;
  const idx = d.recommendation.indexOf(":");
  const sentence = idx > -1 ? d.recommendation.slice(idx + 1).trim() : d.recommendation;
  const engine = t.path === "llm" ? t.model : (t.path === "fallback" ? "rules only (AI unavailable)" : t.path);
  return el("div", { class: "panel decision" },
    el("div", { class: "top" },
      el("span", { class: "dbadge d-" + res.decision_type }, DECISION_LABEL[res.decision_type] || "Recommendation"),
      el("span", { class: "muted" }, `${d.required_approvals.length} approvals · ${d.risk_flags.length} flags · human review required`)),
    el("div", { class: "sentence" }, sentence),
    el("div", { class: "next" }, el("span", { class: "label" }, "Next step"), d.next_step),
    el("div", { class: "jump" }, el("button", { class: "linkbtn", type: "button",
      onclick: () => { const t = $("decide"); t.scrollIntoView({ behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" }); t.querySelector("button").focus({ preventScroll: true }); } },
      "Review the evidence below, then decide ↓")),
    el("div", { class: "meta num" },
      el("span", {}, `Architecture: ${t.architecture}`), el("span", {}, `Engine: ${engine}`),
      el("span", {}, `${t.llm_calls} LLM calls · ${t.tool_calls} tool calls`),
      el("span", {}, `${(t.latency_active_ms / 1000).toFixed(1)} s active` + (t.llm_wait_ms ? ` (+${(t.llm_wait_ms / 1000).toFixed(1)} s rate-limit wait)` : "")),
      el("span", { class: "mono" }, `run ${t.run_id}`)));
}

function roleItem(a) {
  return el("div", { class: "role" + (SPECIALISTS.has(a.role) ? " spec" : "") },
    el("span", { class: "ico", "aria-hidden": "true" }, ROLE_CODE[a.role] || a.role[0]),
    el("div", { class: "name" }, a.role, a.name_hint ? el("span", { class: "hint" }, a.name_hint) : null),
    el("ul", {}, a.reasons.map((r) => el("li", {},
      r.rule === "AI" ? el("span", { class: "ai-tag" }, "AI added") : el("span", { class: "ref" }, `${r.rule} ${r.policy_ref}`),
      " ", r.detail))));
}

function approvalsPanel(res) {
  const business = res.approvals.filter((a) => !SPECIALISTS.has(a.role));
  const specialist = res.approvals.filter((a) => SPECIALISTS.has(a.role));
  return el("div", { class: "panel" },
    el("span", { class: "label" }, `Approvals required (${res.approvals.length})`),
    el("div", { class: "roles" },
      business.length ? el("div", {}, el("div", { class: "group-title" }, "Business approvals"), business.map(roleItem)) : null,
      specialist.length ? el("div", {}, el("div", { class: "group-title" }, "Specialist reviews"), specialist.map(roleItem)) : null));
}

function flagsPanel(res) {
  const flags = res.decision.risk_flags;
  return el("div", { class: "panel" },
    el("span", { class: "label" }, `Risk flags (${flags.length})`),
    flags.length ? el("div", { class: "flags" }, flags.map((f) => el("div", { class: "flag " + (FLAG_SEVERITY[f] || "") },
      el("code", {}, f), el("span", { class: "why" }, FLAG_HELP[f] || "")))) : el("div", { class: "muted" }, "No risk flags."));
}

function missingPanel(res) {
  const block = (title, items) => items.length ? el("div", {}, el("strong", {}, title), el("ul", {}, items.map((m) => el("li", {}, m)))) : null;
  const any = res.missing_fields.length || res.could_not_verify.length || res.questions_for_reviewer.length;
  return el("div", { class: "panel" },
    el("span", { class: "label" }, "Missing or unverified"),
    any ? el("div", { class: "missing-list" },
      block("Missing from the request", res.missing_fields),
      block("Could not verify", res.could_not_verify),
      block("Questions for the reviewer (AI)", res.questions_for_reviewer))
      : el("div", { class: "muted" }, "Nothing missing or unverifiable."));
}

function evidencePanel(res) {
  const counts = { All: res.evidence.length };
  res.evidence.forEach((e) => { const k = e.kind.split(" ")[0]; counts[k] = (counts[k] || 0) + 1; });
  const wrap = el("div", { class: "panel" });
  const draw = () => {
    const rows = res.evidence.filter((e) => state.evFilter === "All" || e.kind.split(" ")[0] === state.evFilter);
    setChildren(wrap,
      el("div", { class: "ev-head" },
        el("span", { class: "label" }, `Evidence (${res.evidence.length})`),
        el("div", { class: "filters", role: "group", "aria-label": "Filter evidence" }, ["All", "Rule", "Tool", "AI"].filter((k) => counts[k]).map((k) =>
          el("button", { type: "button", "aria-pressed": state.evFilter === k ? "true" : "false",
            onclick: () => { state.evFilter = k; draw(); } }, `${k === "AI" ? "AI analysis" : k} ${counts[k]}`)))),
      el("div", { class: "table-wrap" }, el("table", { class: "evidence" },
        el("thead", {}, el("tr", {}, ["Source", "Finding", "Reference", "Kind"].map((h) => el("th", { scope: "col" }, h)))),
        el("tbody", {}, rows.map((e) => el("tr", {},
          el("td", { class: "src" }, SOURCE_LABEL[e.source] || e.source, el("span", { class: "tool" }, e.source)),
          el("td", {}, e.finding),
          el("td", { class: "ref" }, e.reference || "—"),
          el("td", {}, el("span", { class: "kind " + e.kind.split(" ")[0] }, e.kind))))))));
  };
  draw();
  return wrap;
}

function resultView(res) {
  return [
    res.banners.length ? el("div", { class: "banners" }, res.banners.map(banner)) : null,
    decisionPanel(res),
    el("div", { class: "grid2" }, approvalsPanel(res), el("div", { class: "detail" }, flagsPanel(res), missingPanel(res))),
    evidencePanel(res),
    el("div", { class: "grid2" }, decidePanel(res), requestPanel()),
  ];
}

// ---------------------------------------------------------------- actions + audit
function consequence(action, res) {
  const who = (state.request.requester && state.request.requester.name) || "the requester";
  const roles = res.decision.required_approvals;
  if (action === "send_for_approvals") {
    return `Sends the evidence pack to ${roles.length} approvers: ${roles.join(", ")}. Nothing is approved or purchased; each approver decides.`;
  }
  if (action === "request_clarification") {
    const items = res.missing_fields.length ? res.missing_fields.join("; ") : "the information you name in the reason";
    return `Returns the request to ${who}, asking for: ${items}. It leaves the review queue until they reply.`;
  }
  if (action === "suggest_existing_tool") {
    const ids = res.overlap_candidates.length ? res.overlap_candidates.join(", ") : "an existing licensed tool";
    return `Tells ${who} that ${ids} may already cover this need. No new purchase is started.`;
  }
  return "Keeps the request open for a manual procurement review. No one is notified.";
}

function decidePanel(res) {
  const audit = el("div", { class: "audit", id: "audit-box" });
  loadAudit(audit);
  return el("div", { class: "panel decide", id: "decide" },
    el("span", { class: "label" }, "Your decision"),
    el("div", { class: "actions" }, Object.keys(ACTION_LABEL).map((a) => {
      const rec = a === res.recommended_action;
      return el("button", { class: "btn" + (rec ? " recommended" : ""), type: "button", onclick: (ev) => openActionModal(a, res, ev.currentTarget) },
        ACTION_LABEL[a], rec ? el("span", { class: "rec-tag" }, "Recommended") : null);
    })),
    el("div", { class: "foot-note" }, "Choosing a different action than recommended needs a reason. Routing is simulated: actions are recorded in the audit log only."),
    audit);
}

async function loadAudit(box, success) {
  try {
    const entries = await api("/api/audit?request_id=" + encodeURIComponent(state.selected));
    setChildren(box,
      success ? el("div", { class: "success" }, `Recorded at ${fmtTime(success.timestamp)} · outcome pending`) : null,
      el("span", { class: "label" }, `Audit history (${entries.length})`),
      entries.length ? el("ol", { reversed: true }, entries.slice().reverse().map((e) => el("li", {},
        `${fmtTime(e.timestamp)} · ${ACTION_LABEL[e.action]} · copilot said ${DECISION_LABEL[e.copilot_decision_type] || e.copilot_decision_type} (${e.architecture}) · `,
        e.override ? el("span", { class: "ovr" }, "override") : "followed recommendation",
        e.reason ? ` · reason: ${e.reason}` : ""))) : el("div", { class: "muted" }, "No actions recorded yet."));
  } catch (e) {
    setChildren(box, el("div", { class: "state error" }, `Could not load the audit history: ${e.message}`));
  }
}

function closeModal() {
  $("modal-backdrop").hidden = true;
  $("modal").replaceChildren();
  if (state.lastFocus && state.lastFocus.isConnected) state.lastFocus.focus();
}
function openModal(trigger, focusEl, ...children) {
  state.lastFocus = trigger || document.activeElement;
  setChildren($("modal"), children);
  $("modal-backdrop").hidden = false;
  (focusEl || $("modal").querySelector("button, input, textarea, select")).focus();
}

function openActionModal(action, res, trigger) {
  const override = action !== res.recommended_action;
  const reason = el("textarea", { id: "action-reason", placeholder: override ? "Why are you choosing a different action?" : "Optional note for the audit log" });
  const err = el("div", { class: "err", role: "alert" });
  const confirm = el("button", { class: "btn btn-primary", type: "button" }, `Confirm: ${ACTION_LABEL[action]}`);
  confirm.addEventListener("click", async () => {
    if (override && !reason.value.trim()) { err.textContent = "Add a reason: this action differs from the copilot recommendation."; reason.focus(); return; }
    confirm.disabled = true;
    try {
      const entry = await api("/api/actions", { method: "POST", body: {
        request_id: state.selected, run_id: res.trace_summary.run_id, action, reason: reason.value } });
      closeModal();
      toast(`${ACTION_LABEL[action]} recorded`);
      loadAudit($("audit-box"), entry);
      loadQueue();
    } catch (e) {
      err.textContent = e.message;
      confirm.disabled = false;
    }
  });
  openModal(trigger, override ? reason : confirm,
    el("h2", { id: "modal-title" }, ACTION_LABEL[action]),
    override ? el("div", { class: "banner warn" }, el("div", { class: "t" }, el("span", { class: "kind" }, "Override"),
      `The copilot recommends "${ACTION_LABEL[res.recommended_action]}".`)) : null,
    el("div", { class: "consequence" }, consequence(action, res)),
    el("div", { class: "field" }, el("label", { for: "action-reason" }, override ? "Reason (required)" : "Reason (optional)"), reason),
    err,
    el("div", { class: "foot-note" }, "Routing is simulated: actions are recorded in the audit log only."),
    el("div", { class: "buttons" }, el("button", { class: "btn", type: "button", onclick: closeModal }, "Cancel"), confirm));
}

// ---------------------------------------------------------------- compare (US-7)
async function compareBoth() {
  if (state.busy) return;
  state.busy = true;
  state.extra = null;
  renderDetail();
  try {
    const both = await api("/api/compare", { method: "POST", body: { request_id: state.selected } });
    state.result = both[state.arch];
    const row = (k, f) => el("tr", {}, el("th", { scope: "row" }, k), el("td", {}, f(both.single)), el("td", {}, f(both.staged)));
    const events = (r) => Object.entries(r.trace_summary.event_counts || {})
      .filter(([k, v]) => v && !["llm_turn", "gate_filled"].includes(k) && !k.startsWith("gate_filled:")).map(([k, v]) => `${k}: ${v}`).join(", ") || "none";
    state.extra = el("div", { class: "panel" },
      el("span", { class: "label" }, "Architecture comparison on this request"),
      el("div", { class: "cmp-wrap" }, el("table", { class: "cmp-table" },
        el("thead", {}, el("tr", {}, el("th", {}, ""), el("th", { scope: "col" }, "Single agent"), el("th", { scope: "col" }, "Staged"))),
        el("tbody", {},
          row("Decision", (r) => el("span", { class: "pill d-" + r.decision_type }, DECISION_LABEL[r.decision_type])),
          row("Approvals", (r) => r.decision.required_approvals.join(", ")),
          row("Flags", (r) => r.decision.risk_flags.join(", ") || "none"),
          row("Recommendation", (r) => r.decision.recommendation),
          row("LLM / tool calls", (r) => `${r.trace_summary.llm_calls} / ${r.trace_summary.tool_calls}`),
          row("Active latency", (r) => `${(r.trace_summary.latency_active_ms / 1000).toFixed(1)} s`),
          row("Guardrail events", events)))),
      el("div", { class: "foot-note" }, `The ${state.arch} result is shown below.`));
  } catch (e) {
    state.extra = el("div", { class: "panel state error" }, `Comparison failed: ${e.message}`);
  }
  state.busy = false;
  renderDetail();
  loadQueue();
}

// ---------------------------------------------------------------- new request (US-6)
function openNewRequest(ev) {
  const f = {};
  const field = (name, label, attrs = {}) => {
    f[name] = el("input", { id: "nr-" + name, name, ...attrs });
    return el("div", { class: "field" }, el("label", { for: "nr-" + name }, label), f[name]);
  };
  f.business_justification = el("textarea", { id: "nr-justification" });
  f.no_integrations = el("input", { id: "nr-none", type: "checkbox" });
  f.urgency = el("select", { id: "nr-urgency" }, ["", "low", "normal", "high", "urgent"].map((u) => el("option", { value: u }, u || "Not set")));
  const levels = el("datalist", { id: "levels" }, ["none", "internal", "internal_documents", "internal_marketing", "public",
    "confidential_documents", "customer_pii", "employee_pii", "source_code", "production_telemetry", "credentials"].map((v) => el("option", { value: v })));
  const err = el("div", { class: "err", role: "alert" });
  const submit = el("button", { class: "btn btn-primary", type: "button" }, "Submit request");
  submit.addEventListener("click", async () => {
    const val = (k) => f[k].value.trim();
    const num = (k) => (val(k) === "" ? null : Number(val(k)));
    const integ = val("requested_integrations");
    const body = {
      requester_id: val("requester_id") || null, product_name: val("product_name") || null, vendor_name: val("vendor_name") || null,
      category: val("category") || null, annual_cost_usd: num("annual_cost_usd"), user_count: num("user_count"),
      business_justification: f.business_justification.value.trim() || null, data_access_level: val("data_access_level") || null,
      requested_integrations: f.no_integrations.checked ? [] : (integ ? integ.split(",").map((s) => s.trim()).filter(Boolean) : null),
      urgency: f.urgency.value || null,
    };
    submit.disabled = true;
    try {
      const rec = await api("/api/requests", { method: "POST", body });
      closeModal();
      toast(`${rec.request_id} added to the queue`);
      await loadQueue();
      selectRequest(rec.request_id);
    } catch (e) {
      err.textContent = `Could not submit: ${e.message}`;
      submit.disabled = false;
    }
  });
  const first = field("requester_id", "Requester employee ID (e.g. E001)");
  openModal(ev && ev.currentTarget, f.requester_id,
    el("h2", { id: "modal-title" }, "New purchase request"),
    el("div", { class: "muted" }, "Every field may be left blank. The copilot will ask the requester for whatever is missing."),
    first,
    el("div", { class: "two" }, field("product_name", "Product"), field("vendor_name", "Vendor")),
    el("div", { class: "two" }, field("category", "Category"), field("annual_cost_usd", "Annual cost (USD)", { type: "number", min: "0", step: "0.01", inputmode: "decimal" })),
    el("div", { class: "two" }, field("user_count", "Users / licences", { type: "number", min: "1", step: "1" }),
      el("div", { class: "field" }, el("label", { for: "nr-urgency" }, "Urgency"), f.urgency)),
    el("div", { class: "field" }, el("label", { for: "nr-justification" }, "Business justification"), f.business_justification),
    field("data_access_level", "Data access level", { list: "levels" }), levels,
    field("requested_integrations", "Integrations (comma-separated)"),
    el("label", { class: "check", for: "nr-none" }, f.no_integrations, "No integrations needed"),
    err,
    el("div", { class: "buttons" }, el("button", { class: "btn", type: "button", onclick: closeModal }, "Cancel"), submit));
}

// ---------------------------------------------------------------- boot
function emptyDetail() {
  setChildren($("detail"), el("div", { class: "panel empty" },
    el("h2", {}, "Select a request to review"),
    el("p", {}, "The copilot gathers budget, catalog, vendor and policy evidence, then recommends the next step. You decide what happens.")));
}
$("btn-new").addEventListener("click", openNewRequest);
$("btn-refresh").addEventListener("click", loadQueue);
$("q-search").addEventListener("input", renderQueue);
$("q-filter").addEventListener("change", renderQueue);
$("modal-backdrop").addEventListener("click", (e) => { if (e.target.id === "modal-backdrop") closeModal(); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !$("modal-backdrop").hidden) closeModal(); });
emptyDetail();
loadHealth();
loadQueue().then(() => {
  const id = decodeURIComponent(location.hash.slice(1));
  if (id && state.queue.some((r) => r.request_id === id)) selectRequest(id);
});
setInterval(loadHealth, 30000);
