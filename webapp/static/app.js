"use strict";
// Reviewer UI. All business text is inserted with textContent (via el()); innerHTML is never used.

const FLAG_HELP = {
  existing_tool_overlap: "A licensed catalog product shares the category or product; check whether it already meets the need.",
  budget_insufficient: "Annual cost exceeds the department's available software budget; Finance must approve an exception.",
  budget_unverified: "No budget record exists for the department, so funding could not be checked.",
  security_review_required: "Sensitive data/access or an incomplete vendor security assessment requires Security review.",
  privacy_review_required: "Personal data, or sensitive data possibly stored outside the region, requires Privacy review.",
  legal_review_required: "New vendor at $10,000 or more, non-standard terms, or a cross-region issue requires Legal review.",
  vendor_review_expired: "The vendor security assessment is more than 365 days old (or marked expired).",
  conflicting_vendor_evidence: "The internal registry and the vendor-risk service disagree; neither is trusted silently.",
  vendor_risk_unavailable: "The vendor-risk service could not be reached; nothing favorable is assumed.",
  vendor_not_registered: "The vendor is not in the registry: treated as a new vendor with unknown terms.",
  prompt_injection_detected: "Business data contained instructions aimed at the AI; they were ignored.",
  missing_information: "Required request fields are missing; the requester must supply them.",
  unrecognized_data_access_level: "The declared data access level is not recognised; Security must classify it.",
  llm_unavailable: "AI analysis was unavailable; the result comes from deterministic rules only.",
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
const SPECIALISTS = new Set(["Security", "Privacy", "Legal"]);

const state = { queue: [], selected: null, request: null, result: null, arch: "single", busy: false };

// ---------------------------------------------------------------- DOM helpers
function el(tag, props, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else if (k === "disabled" || k === "checked" || k === "selected") node[k] = Boolean(v);
    else node.setAttribute(k, v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return node;
}
const $ = (id) => document.getElementById(id);
const money = (v) => (v === null || v === undefined) ? null : "$" + Number(v).toLocaleString("en-US", { maximumFractionDigits: 2 });
const orMissing = (v) => (v === null || v === undefined || v === "" ? el("span", { class: "missing" }, "— missing") : String(v));

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  let data = null;
  try { data = await res.json(); } catch (_) { /* empty body */ }
  if (!res.ok) {
    const detail = data && data.detail ? (typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail)) : res.statusText;
    throw new Error(detail);
  }
  return data;
}

// ---------------------------------------------------------------- health
async function loadHealth() {
  try {
    const h = await api("/api/health");
    $("dot-vendor").className = "dot " + (h.vendor_api === "ok" ? "ok" : "down");
    $("health-vendor").textContent = "Vendor-risk API " + (h.vendor_api === "ok" ? "up" : "down");
    $("dot-llm").className = "dot " + (h.llm.configured ? "ok" : "warn");
    $("health-llm").textContent = `${h.llm.provider} / ${h.llm.model}` + (h.llm.configured ? "" : " (not configured: rules only)");
  } catch (e) {
    $("dot-vendor").className = "dot down";
    $("health-vendor").textContent = "App health unavailable";
  }
}

// ---------------------------------------------------------------- queue
async function loadQueue() {
  const body = $("queue-body");
  body.replaceChildren(el("div", { class: "state" }, "Loading requests…"));
  try {
    state.queue = await api("/api/requests");
  } catch (e) {
    body.replaceChildren(el("div", { class: "state error" }, "Could not load requests: " + e.message, el("div", {},
      el("button", { class: "btn btn-small", onclick: loadQueue }, "Retry"))));
    return;
  }
  renderQueue();
}

function renderQueue() {
  const body = $("queue-body");
  if (!state.queue.length) {
    body.replaceChildren(el("div", { class: "state" }, "No purchase requests awaiting review"));
    return;
  }
  body.replaceChildren(...state.queue.map((r) => el("div", {
    class: "qrow" + (r.request_id === state.selected ? " active" : ""),
    onclick: () => selectRequest(r.request_id),
  },
  el("div", { class: "line1" }, el("span", {}, r.request_id), el("span", { class: "chip status-" + r.status.split(" ")[0] }, r.status)),
  el("div", { class: "line2" }, r.product_name || "— product missing", " · ", r.vendor_name || "— vendor missing"),
  el("div", { class: "line3" }, r.requester || "unknown requester", " · ", r.department || "—", " · ",
    money(r.annual_cost_usd) || "cost missing", r.urgency ? " · " + r.urgency : ""),
  )));
}

// ---------------------------------------------------------------- request detail
async function selectRequest(id) {
  state.selected = id;
  state.result = null;
  renderQueue();
  const detail = $("detail");
  detail.replaceChildren(el("div", { class: "state" }, "Loading request…"));
  try {
    const data = await api("/api/requests/" + encodeURIComponent(id));
    state.request = data;
    state.result = data.latest_analysis || null;
  } catch (e) {
    detail.replaceChildren(el("div", { class: "state error" }, "Could not load request: " + e.message));
    return;
  }
  renderDetail();
}

function requestCard() {
  const r = state.request.request;
  const who = state.request.requester;
  const integ = r.requested_integrations;
  const integText = integ === null || integ === undefined ? null : (integ.length ? integ.join(", ") : "none");
  const rows = [
    ["Request", r.request_id],
    ["Requester", who ? `${who.name} (${who.id}, ${who.department}, ${who.level})` : (r.requester_id ? `${r.requester_id} (not found)` : null)],
    ["Manager / Dept head", who ? `${who.manager_hint || "—"} / ${who.department_head_hint || "—"}` : null],
    ["Product", r.product_name],
    ["Vendor", r.vendor_name],
    ["Category", r.category],
    ["Annual cost", money(r.annual_cost_usd)],
    ["Users / licenses", r.user_count],
    ["Data access level", r.data_access_level && String(r.data_access_level).toLowerCase() !== "unknown" ? r.data_access_level : null],
    ["Integrations", integText],
    ["Urgency", r.urgency ? `${r.urgency} (informational only)` : null],
  ];
  return el("div", { class: "card" },
    el("h3", {}, "Purchase request"),
    el("div", { class: "grid2" }, rows.map(([k, v]) => [el("div", { class: "k" }, k), el("div", {}, orMissing(v))])),
    el("div", { class: "quote-label" }, "Requester text (untrusted)"),
    el("div", { class: "quote" }, r.business_justification ? r.business_justification : el("span", { class: "missing" }, "— missing")),
  );
}

function runBar() {
  const radio = (value, label) => el("label", {},
    el("input", { type: "radio", name: "arch", value, checked: state.arch === value, onchange: () => { state.arch = value; } }), label);
  return el("div", { class: "runbar" },
    el("strong", {}, "Architecture:"),
    radio("single", "Single agent"),
    radio("staged", "Staged (analyst + reviewer)"),
    el("button", { class: "btn btn-primary", id: "btn-analyse", disabled: state.busy, onclick: analyse },
      state.result ? "Re-analyse" : "Analyse"),
    el("button", { class: "linkbtn", disabled: state.busy, onclick: compareBoth }, "Compare both"),
  );
}

function renderDetail(extra) {
  const detail = $("detail");
  const parts = [requestCard(), runBar()];
  if (extra) parts.push(extra);
  if (state.result) parts.push(...resultView(state.result));
  detail.replaceChildren(...parts);
}

async function analyse() {
  if (state.busy) return;
  state.busy = true;
  renderDetail(el("div", { class: "state" }, "Gathering evidence: budget · catalog · vendor · policy…"));
  try {
    state.result = await api("/api/analyze", { method: "POST", body: { request_id: state.selected, architecture: state.arch } });
    state.busy = false;
    renderDetail();
    loadQueue();
  } catch (e) {
    state.busy = false;
    renderDetail(el("div", { class: "state error" }, "Analysis failed: " + e.message,
      el("div", {}, el("button", { class: "btn btn-small", onclick: analyse }, "Retry"))));
  }
}

// ---------------------------------------------------------------- result
function banner(b) {
  return el("div", { class: "banner " + b.level },
    el("div", { class: "t" }, b.title),
    b.detail ? el("div", { class: "d" }, b.detail) : null,
    b.compare ? el("div", { class: "compare" }, Object.entries(b.compare).map(([k, v]) =>
      el("div", {}, el("div", { class: "k" }, k), v))) : null,
    (b.excerpts || []).map((x) => el("div", { class: "excerpt" }, x)),
  );
}

function approvalsCard(res) {
  const box = el("div", {});
  const reasonsBox = el("div", {});
  box.append(...res.approvals.map((a) => {
    const chip = el("span", { class: "chip role" + (SPECIALISTS.has(a.role) ? " specialist" : ""), title: "Show why" }, a.role);
    chip.addEventListener("click", () => {
      const open = chip.classList.toggle("open");
      box.querySelectorAll(".chip.role").forEach((c) => { if (c !== chip) c.classList.remove("open"); });
      reasonsBox.replaceChildren();
      if (open) {
        reasonsBox.append(el("div", { class: "reasons" },
          el("strong", {}, a.role), a.name_hint ? el("span", { class: "muted" }, " · " + a.name_hint) : null,
          el("ul", {}, a.reasons.map((r) => el("li", {},
            el("span", { class: "muted" }, (r.rule === "AI" ? "AI-added" : r.rule) + (r.policy_ref ? " " + r.policy_ref : "") + ": "),
            r.detail)))));
      }
    });
    return chip;
  }));
  return el("div", { class: "card" }, el("h3", {}, `Approvals required (${res.approvals.length})`), box,
    el("div", { class: "muted", style: "font-size:12px" }, "Click a role to see the rule and policy section."), reasonsBox);
}

function flagsCard(res) {
  const flags = res.decision.risk_flags;
  return el("div", { class: "card" }, el("h3", {}, "Risk flags"),
    flags.length ? el("ul", { class: "flaglist" }, flags.map((f) => el("li", {},
      el("span", { class: "chip flag" }, f), " ", FLAG_HELP[f] || ""))) : el("div", { class: "muted" }, "No risk flags."));
}

function missingCard(res) {
  const items = [];
  if (res.missing_fields.length) items.push(el("div", {}, el("strong", {}, "Missing from the request"), el("ul", { class: "plain" }, res.missing_fields.map((m) => el("li", {}, m)))));
  if (res.could_not_verify.length) items.push(el("div", {}, el("strong", {}, "Could not verify"), el("ul", { class: "plain" }, res.could_not_verify.map((m) => el("li", {}, m)))));
  if (res.questions_for_reviewer.length) items.push(el("div", {}, el("strong", {}, "Questions for the reviewer (AI)"), el("ul", { class: "plain" }, res.questions_for_reviewer.map((m) => el("li", {}, m)))));
  return el("div", { class: "card" }, el("h3", {}, "Missing information / could not verify"),
    items.length ? items : el("div", { class: "muted" }, "Nothing missing or unverifiable."));
}

function evidenceCard(res) {
  return el("div", { class: "card" }, el("h3", {}, `Evidence (${res.evidence.length})`),
    el("table", { class: "evidence" },
      el("thead", {}, el("tr", {}, el("th", {}, "Source"), el("th", {}, "Finding"), el("th", {}, "Reference"), el("th", {}, "Kind"))),
      el("tbody", {}, res.evidence.map((e) => el("tr", {},
        el("td", {}, e.source), el("td", {}, e.finding), el("td", {}, e.reference || "—"),
        el("td", {}, el("span", { class: "kind " + e.kind.split(" ")[0] }, e.kind)))))));
}

function resultView(res) {
  const t = res.trace_summary;
  const d = res.decision;
  const [label, ...rest] = d.recommendation.split(":");
  const sentence = rest.join(":").trim();
  const out = [];
  out.push(...res.banners.map(banner));
  out.push(el("div", { class: "card rec" },
    el("span", { class: "badge " + res.decision_type }, DECISION_LABEL[res.decision_type] || label),
    el("span", { class: "muted" }, "Copilot recommendation · human review required"),
    el("div", { class: "sentence" }, sentence || d.recommendation),
    el("div", { class: "next" }, el("b", {}, "Next step: "), d.next_step)));
  out.push(approvalsCard(res), flagsCard(res), missingCard(res), evidenceCard(res));
  out.push(el("div", { class: "footer-line" },
    `${t.architecture} · ${t.path === "llm" ? t.model : (t.path === "fallback" ? "rules (LLM unavailable)" : t.path)} · `
    + `${t.llm_calls} LLM calls · ${t.tool_calls} tool calls · ${Math.round(t.latency_active_ms)} ms active`
    + (t.llm_wait_ms ? ` (+${Math.round(t.llm_wait_ms)} ms rate-limit wait)` : "") + ` · run ${t.run_id}`));
  out.push(actionCard(res));
  return out;
}

// ---------------------------------------------------------------- actions + audit
function consequence(action, res) {
  const who = (state.request.requester && state.request.requester.name) || "the requester";
  const roles = res.decision.required_approvals;
  if (action === "send_for_approvals") {
    return `Sends the evidence pack to ${roles.length} approvers: ${roles.join(", ")}. Nothing is approved or purchased; each approver decides.`;
  }
  if (action === "request_clarification") {
    const items = res.missing_fields.length ? res.missing_fields.join("; ") : "the information you specify in the reason";
    return `Returns the request to ${who}, asking for: ${items}. It leaves the review queue until they reply.`;
  }
  if (action === "suggest_existing_tool") {
    const ids = res.overlap_candidates.length ? res.overlap_candidates.join(", ") : "an existing licensed tool";
    return `Tells ${who} that ${ids} may already cover this need. No new purchase is started.`;
  }
  return "Keeps the request open for a manual procurement review. No one is notified.";
}

function actionCard(res) {
  const wrap = el("div", { class: "card" }, el("h3", {}, "Decide"));
  wrap.append(el("div", { class: "actionbar" }, Object.keys(ACTION_LABEL).map((a) =>
    el("button", { class: "btn" + (a === res.recommended_action ? " recommended" : ""), onclick: () => openActionModal(a, res) },
      ACTION_LABEL[a] + (a === res.recommended_action ? " (recommended)" : "")))));
  const audit = el("div", { class: "audit", id: "audit-box" });
  wrap.append(audit);
  loadAudit(audit);
  return wrap;
}

async function loadAudit(box, success) {
  try {
    const entries = await api("/api/audit?request_id=" + encodeURIComponent(state.selected));
    box.replaceChildren(
      success ? el("div", { class: "success" }, `Recorded at ${new Date(success.timestamp).toLocaleString()} · outcome pending`) : null,
      el("h3", { style: "margin-top:12px" }, "Audit history"),
      entries.length ? el("ul", { class: "plain" }, entries.slice().reverse().map((e) => el("li", {},
        `${new Date(e.timestamp).toLocaleString()} · ${ACTION_LABEL[e.action]} · copilot: ${DECISION_LABEL[e.copilot_decision_type] || e.copilot_decision_type}`
        + ` (${e.architecture}) · ${e.override ? "override" : "followed recommendation"}` + (e.reason ? ` · reason: ${e.reason}` : "")
        + ` · run ${e.run_id}`))) : el("div", { class: "muted" }, "No actions recorded yet."));
  } catch (e) {
    box.replaceChildren(el("div", { class: "state error" }, "Could not load audit history: " + e.message));
  }
}

function closeModal() { $("modal-backdrop").classList.add("hidden"); $("modal").replaceChildren(); }
function openModal(...children) { $("modal").replaceChildren(...children); $("modal-backdrop").classList.remove("hidden"); }

function openActionModal(action, res) {
  const override = action !== res.recommended_action;
  const reason = el("textarea", { placeholder: override ? "Required: why are you overriding the copilot recommendation?" : "Optional note" });
  const err = el("div", { class: "err" });
  const confirm = el("button", { class: "btn btn-primary" }, "Confirm");
  confirm.addEventListener("click", async () => {
    if (override && !reason.value.trim()) { err.textContent = "A reason is required when you override the recommendation."; return; }
    confirm.disabled = true;
    try {
      const entry = await api("/api/actions", { method: "POST", body: {
        request_id: state.selected, run_id: res.trace_summary.run_id, action, reason: reason.value } });
      closeModal();
      loadAudit($("audit-box"), entry);
      loadQueue();
    } catch (e) {
      err.textContent = e.message;
      confirm.disabled = false;
    }
  });
  openModal(
    el("h2", {}, ACTION_LABEL[action]),
    override ? el("div", { class: "banner warn" }, el("div", { class: "t" },
      `Override: the copilot recommends "${ACTION_LABEL[res.recommended_action]}".`)) : null,
    el("div", { class: "consequence" }, consequence(action, res)),
    el("div", { class: "row" }, el("label", {}, override ? "Reason (required)" : "Reason (optional)"), reason),
    err,
    el("div", { class: "foot" }, "Routing is simulated: actions are recorded in the audit log only."),
    el("div", { class: "buttons" }, el("button", { class: "btn", onclick: closeModal }, "Cancel"), confirm),
  );
}

// ---------------------------------------------------------------- compare (US-7)
async function compareBoth() {
  if (state.busy) return;
  state.busy = true;
  renderDetail(el("div", { class: "state" }, "Running both architectures: gathering evidence twice…"));
  try {
    const both = await api("/api/compare", { method: "POST", body: { request_id: state.selected } });
    state.busy = false;
    state.result = both[state.arch];
    const row = (k, f) => el("tr", {}, el("th", {}, k), el("td", {}, f(both.single)), el("td", {}, f(both.staged)));
    const table = el("div", { class: "card" }, el("h3", {}, "Architecture comparison (same request)"),
      el("table", { class: "cmp-table" },
        el("tr", {}, el("th", {}, ""), el("th", {}, "Single agent"), el("th", {}, "Staged")),
        row("Decision", (r) => DECISION_LABEL[r.decision_type]),
        row("Approvals", (r) => r.decision.required_approvals.join(", ")),
        row("Flags", (r) => r.decision.risk_flags.join(", ") || "—"),
        row("Recommendation", (r) => r.decision.recommendation),
        row("LLM / tool calls", (r) => `${r.trace_summary.llm_calls} / ${r.trace_summary.tool_calls}`),
        row("Active latency", (r) => `${Math.round(r.trace_summary.latency_active_ms)} ms`),
        row("Guardrail events", (r) => Object.entries(r.trace_summary.event_counts || {})
          .filter(([k]) => !k.startsWith("llm_turn") && !k.startsWith("gate_filled:")).map(([k, v]) => `${k}: ${v}`).join(", ") || "—")),
      el("div", { class: "muted", style: "font-size:12px" }, `Showing the ${state.arch} result below.`));
    renderDetail(table);
    loadQueue();
  } catch (e) {
    state.busy = false;
    renderDetail(el("div", { class: "state error" }, "Comparison failed: " + e.message));
  }
}

// ---------------------------------------------------------------- new request (US-6)
function openNewRequest() {
  const f = {};
  const input = (name, label, attrs = {}) => {
    f[name] = el("input", { name, ...attrs });
    return el("div", { class: "row" }, el("label", {}, label), f[name]);
  };
  f.business_justification = el("textarea", { name: "business_justification" });
  f.no_integrations = el("input", { type: "checkbox" });
  f.urgency = el("select", {}, ["", "low", "normal", "high", "urgent"].map((u) => el("option", { value: u }, u || "—")));
  const levels = el("datalist", { id: "levels" }, ["none", "internal", "internal_documents", "internal_marketing", "public",
    "confidential_documents", "customer_pii", "employee_pii", "source_code", "production_telemetry", "credentials"].map((v) => el("option", { value: v })));
  const err = el("div", { class: "err" });
  const submit = el("button", { class: "btn btn-primary" }, "Submit request");
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
      await loadQueue();
      selectRequest(rec.request_id);
    } catch (e) {
      err.textContent = "Could not submit: " + e.message;
      submit.disabled = false;
    }
  });
  openModal(
    el("h2", {}, "New purchase request"),
    el("div", { class: "muted", style: "margin-bottom:10px" }, "Every field may be left blank; the copilot will ask for what is missing."),
    input("requester_id", "Requester employee ID (e.g. E001)"),
    input("product_name", "Product"), input("vendor_name", "Vendor"), input("category", "Category"),
    input("annual_cost_usd", "Annual cost (USD)", { type: "number", min: "0", step: "0.01" }),
    input("user_count", "Number of users/licenses", { type: "number", min: "1", step: "1" }),
    el("div", { class: "row" }, el("label", {}, "Business justification"), f.business_justification),
    input("data_access_level", "Data access level", { list: "levels" }), levels,
    input("requested_integrations", "Integrations (comma-separated)"),
    el("div", { class: "row" }, el("label", { style: "display:flex;gap:6px;align-items:center" }, f.no_integrations, "No integrations needed")),
    el("div", { class: "row" }, el("label", {}, "Urgency"), f.urgency),
    err,
    el("div", { class: "buttons" }, el("button", { class: "btn", onclick: closeModal }, "Cancel"), submit),
  );
}

// ---------------------------------------------------------------- boot
$("btn-new").addEventListener("click", openNewRequest);
$("btn-refresh").addEventListener("click", loadQueue);
$("modal-backdrop").addEventListener("click", (e) => { if (e.target.id === "modal-backdrop") closeModal(); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeModal(); });
loadHealth();
loadQueue();
setInterval(loadHealth, 30000);
