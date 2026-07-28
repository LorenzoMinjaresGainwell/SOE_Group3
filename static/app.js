"use strict";

const FAMILIES = {
  opportunities: { endpoint: "/api/opportunities", title: "New opportunities", kicker: "Pipeline" },
  contracts: { endpoint: "/api/contracts", title: "Contracts & recompetes", kicker: "Market intelligence" },
  updates: { endpoint: "/api/updates", title: "Updates", kicker: "Policy & program signals" },
};

const STATE_NAMES = {
  AL: "Alabama", AK: "Alaska", AZ: "Arizona", AR: "Arkansas", CA: "California", CO: "Colorado",
  CT: "Connecticut", DE: "Delaware", DC: "District of Columbia", FL: "Florida", GA: "Georgia",
  HI: "Hawaii", ID: "Idaho", IL: "Illinois", IN: "Indiana", IA: "Iowa", KS: "Kansas",
  KY: "Kentucky", LA: "Louisiana", ME: "Maine", MD: "Maryland", MA: "Massachusetts",
  MI: "Michigan", MN: "Minnesota", MS: "Mississippi", MO: "Missouri", MT: "Montana",
  NE: "Nebraska", NV: "Nevada", NH: "New Hampshire", NJ: "New Jersey", NM: "New Mexico",
  NY: "New York", NC: "North Carolina", ND: "North Dakota", OH: "Ohio", OK: "Oklahoma",
  OR: "Oregon", PA: "Pennsylvania", RI: "Rhode Island", SC: "South Carolina", SD: "South Dakota",
  TN: "Tennessee", TX: "Texas", UT: "Utah", VT: "Vermont", VA: "Virginia", WA: "Washington",
  WV: "West Virginia", WI: "Wisconsin", WY: "Wyoming", AS: "American Samoa", GU: "Guam",
  MP: "Northern Mariana Islands", PR: "Puerto Rico", VI: "U.S. Virgin Islands",
};

const state = {
  activeFamily: "opportunities",
  cache: { opportunities: null, contracts: null, updates: null },
  errors: { opportunities: null, contracts: null, updates: null },
  requests: { opportunities: null, contracts: null, updates: null },
  selected: { opportunities: null, contracts: null, updates: null },
  filters: {
    opportunities: { status: "", score: "0" },
    contracts: { lifecycle: "current", score: "0" },
    updates: { type: "", rht: "", action: "" },
  },
  switchToken: 0,
  detailToken: 0,
  activeView: "records",
  focusToken: 0,
  focusCache: { rht: null, competitors: null },
  focusRequests: { rht: null, competitors: null },
  competitorSelection: "gainwell",
  competitorSelections: [],
  competitorQuery: "",
  rhtFamily: "opportunities",
  focusSelected: { rht: null, competitors: null },
  rhtOnly: false,
};

const money = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 0 });
const number = new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 });
const els = {
  tabs: document.querySelector("#primaryTabs"),
  focusTabs: document.querySelector("#focusTabs"),
  familyView: document.querySelector("#familyView"),
  focusView: document.querySelector("#focusView"),
  metrics: document.querySelector("#metrics"),
  filters: document.querySelector("#familyFilters"),
  search: document.querySelector("#searchInput"),
  jurisdiction: document.querySelector("#jurisdictionFilter"),
  notice: document.querySelector("#pageNotice"),
  panel: document.querySelector("#tablePanel"),
  kicker: document.querySelector("#panelKicker"),
  title: document.querySelector("#tableTitle"),
  count: document.querySelector("#resultCount"),
  head: document.querySelector("#tableHead"),
  rows: document.querySelector("#dashboardRows"),
  detail: document.querySelector("#detailPanel"),
};

async function api(path, options = {}) {
  const response = await fetch(path, {
    cache: "no-store",
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    throw new Error(payload.error || `${response.status} ${response.statusText}` || "Request failed");
  }
  return response.json();
}

function recordsFrom(payload, family) {
  if (Array.isArray(payload)) return payload;
  const candidates = [payload?.items, payload?.records, payload?.results, payload?.[family]];
  return candidates.find(Array.isArray) || [];
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (character) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;",
  })[character]);
}

function safeUrl(value) {
  if (!value) return "";
  try {
    const url = new URL(String(value), window.location.origin);
    return ["http:", "https:"].includes(url.protocol) ? url.href : "";
  } catch (_) {
    return "";
  }
}

function parseDate(value) {
  if (!value) return null;
  const date = new Date(/^\d{4}-\d{2}-\d{2}$/.test(String(value)) ? `${value}T00:00:00` : value);
  return Number.isNaN(date.getTime()) ? null : date;
}

function formatDate(value, fallback = "Not provided") {
  const date = parseDate(value);
  return date ? date.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" }) : fallback;
}

function formatDateTime(value) {
  const date = parseDate(value);
  return date ? date.toLocaleString(undefined, { month: "short", day: "numeric", year: "numeric", hour: "numeric", minute: "2-digit" }) : "Not provided";
}

function normalizeList(value) {
  if (Array.isArray(value)) return value.filter(Boolean);
  if (!value) return [];
  return String(value).split(/[;,]/).map((item) => item.trim()).filter(Boolean);
}

function recordId(item, family) {
  return String(item.id || item[`${family.slice(0, -1)}_id`] || item.update_id || item.contract_id || item.source_record_id || "");
}

function itemTitle(item, family) {
  if (family === "contracts") return item.title || item.description || item.contract_number || "Untitled contract";
  return item.title || item.name || "Untitled record";
}

function priorityData(item) {
  const score = item.priority_score === "" || item.priority_score == null ? null : Number(item.priority_score);
  const confidence = item.confidence && typeof item.confidence === "object" ? item.confidence : {};
  return {
    score: Number.isFinite(score) ? score : null,
    confidence,
    action: item.recommended_action || "Review",
    dimensions: Array.isArray(item.score_breakdown) ? item.score_breakdown : [],
  };
}

function priorityScore(item) {
  return priorityData(item).score;
}

function scoreDisplay(value) {
  const numeric = Number(value);
  return value !== "" && value != null && Number.isFinite(numeric) ? numeric.toLocaleString(undefined, { maximumFractionDigits: 1 }) : "—";
}

function jurisdictionCode(item, family) {
  const raw = String(item.jurisdiction_code || item.state_code || item.state || item.jurisdiction || "").trim();
  if (/^federal$/i.test(raw)) return "Federal";
  const upper = raw.toUpperCase();
  if (STATE_NAMES[upper]) return upper;
  const named = Object.entries(STATE_NAMES).find(([, name]) => name.toLowerCase() === raw.toLowerCase());
  if (named) return named[0];
  if (!raw && (family !== "opportunities" || /federal|sam|usaspending/i.test(`${item.source || ""} ${item.agency || ""}`))) return "Federal";
  return raw;
}

function jurisdictionLabel(item, family) {
  const code = jurisdictionCode(item, family);
  return STATE_NAMES[code] || code || "Nationwide";
}

function isTrue(value) {
  return value === true || String(value).toLowerCase() === "true" || String(value) === "1";
}

function lifecycleOf(item) {
  const explicit = String(item.lifecycle_status || item.recompete_signal || "").trim();
  if (explicit) return explicit;
  const end = parseDate(item.potential_end_date || item.end_date || item.period_end_date);
  if (!end) return "Unknown timing";
  return end < new Date() ? "Expired" : "Active";
}

function isExpired(item) {
  if (isTrue(item.expired)) return true;
  const lifecycle = lifecycleOf(item).toLowerCase();
  if (/expired|past award|cancel/.test(lifecycle)) return true;
  if (/unknown|open-ended|placeholder/.test(lifecycle)) return false;
  const end = parseDate(item.potential_end_date || item.end_date || item.period_end_date);
  return Boolean(end && end < new Date());
}

function isUnknownTiming(item) {
  const lifecycle = lifecycleOf(item).toLowerCase();
  return /unknown|open-ended|placeholder/.test(lifecycle) || (!item.potential_end_date && !item.end_date && !item.period_end_date);
}

function badgeClass(value) {
  const text = String(value || "").toLowerCase();
  if (/pursue|near.expiry|expiring soon|action required|high/.test(text)) return "badge-positive";
  if (/decline|expired|past award|cancel/.test(text)) return "badge-negative";
  if (/recompete|monitor|upcoming|medium/.test(text)) return "badge-warning";
  return "badge-neutral";
}

function scoreBadge(score, label = "") {
  const tone = score >= 80 ? "badge-positive" : score >= 60 ? "badge-warning" : "badge-neutral";
  return `<span class="badge ${tone}">${escapeHtml(label || score)}</span>`;
}

function showNotice(message = "", kind = "") {
  els.notice.hidden = !message;
  els.notice.className = `notice ${kind}`.trim();
  els.notice.textContent = message;
}

function renderFamilyFilters() {
  const family = state.activeFamily;
  const filter = state.filters[family];
  if (family === "opportunities") {
    els.filters.innerHTML = `
      <label>Status<select data-filter="status">
        <option value="">All statuses</option>${["Pursue", "Monitor", "Decline", "Unreviewed"].map((value) => `<option ${filter.status === value ? "selected" : ""}>${value}</option>`).join("")}
      </select></label>
      <label>Minimum priority<input data-filter="score" type="number" min="0" max="100" value="${escapeHtml(filter.score)}"></label>`;
  } else if (family === "contracts") {
    els.filters.innerHTML = `
      <label>Lifecycle<select data-filter="lifecycle">
        <option value="current" ${filter.lifecycle === "current" ? "selected" : ""}>Current + unknown (default)</option>
        <option value="actionable" ${filter.lifecycle === "actionable" ? "selected" : ""}>Recompete / near expiry</option>
        <option value="unknown" ${filter.lifecycle === "unknown" ? "selected" : ""}>Unknown timing</option>
        <option value="expired" ${filter.lifecycle === "expired" ? "selected" : ""}>Expired only</option>
        <option value="all" ${filter.lifecycle === "all" ? "selected" : ""}>All, including expired</option>
      </select></label>
      <label>Minimum priority<input data-filter="score" type="number" min="0" max="100" value="${escapeHtml(filter.score)}"></label>`;
  } else {
    const types = [...new Set((state.cache.updates || []).map((item) => item.record_type || item.update_type || item.type).filter(Boolean))].sort();
    els.filters.innerHTML = `
      <label>Update type<select data-filter="type"><option value="">All types</option>${types.map((value) => `<option value="${escapeHtml(value)}" ${filter.type === value ? "selected" : ""}>${escapeHtml(humanize(value))}</option>`).join("")}</select></label>
      <label>RHT relevance<select data-filter="rht"><option value="">All updates</option><option value="yes" ${filter.rht === "yes" ? "selected" : ""}>RHT signals</option><option value="no" ${filter.rht === "no" ? "selected" : ""}>Other updates</option></select></label>
      <label>Action<select data-filter="action"><option value="">Any action status</option><option value="required" ${filter.action === "required" ? "selected" : ""}>Action required</option><option value="dated" ${filter.action === "dated" ? "selected" : ""}>Has action date</option></select></label>`;
  }
}

function humanize(value) {
  return String(value || "Unknown").replace(/[_-]+/g, " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function searchText(item) {
  return [
    item.title, item.description, item.summary, item.agency, item.subagency, item.vendor_name,
    item.recipient_name, item.organization_name, item.organization_key, item.vendor_key,
    item.source, item.record_type, item.document_type, item.contract_number,
    ...normalizeList(item.program_focus), ...normalizeList(item.topic_keys), ...normalizeList(item.keywords_matched || item.matched_keywords),
  ].join(" ").toLowerCase();
}

function filteredItems() {
  const family = state.activeFamily;
  const query = els.search.value.trim().toLowerCase();
  const jurisdiction = els.jurisdiction.value;
  const filter = state.filters[family];
  return (state.cache[family] || []).filter((item) => {
    if (state.rhtOnly && !isRhtRecord(item)) return false;
    if (query && !searchText(item).includes(query)) return false;
    if (jurisdiction && jurisdictionCode(item, family) !== jurisdiction) return false;
    if (family === "opportunities") {
      return (!filter.status || item.status === filter.status) && (priorityScore(item) ?? 0) >= Number(filter.score || 0);
    }
    if (family === "contracts") {
      if ((priorityScore(item) ?? 0) < Number(filter.score || 0)) return false;
      if (filter.lifecycle === "current" && isExpired(item)) return false;
      if (filter.lifecycle === "expired" && !isExpired(item)) return false;
      if (filter.lifecycle === "unknown" && !isUnknownTiming(item)) return false;
      if (filter.lifecycle === "actionable" && !/near.expiry|expiring soon|recompete/i.test(lifecycleOf(item))) return false;
    }
    if (family === "updates") {
      const type = String(item.record_type || item.update_type || item.type || "");
      const rht = isRhtRecord(item);
      if (filter.type && type !== filter.type) return false;
      if (filter.rht === "yes" && !rht) return false;
      if (filter.rht === "no" && rht) return false;
      if (filter.action === "required" && !isTrue(item.comment_required_flag)) return false;
      if (filter.action === "dated" && !(item.action_required_by || item.due_date || item.effective_date)) return false;
    }
    return true;
  }).sort(sortItems);
}

function sortItems(left, right) {
  const family = state.activeFamily;
  const priorityDifference = (priorityScore(right) ?? -1) - (priorityScore(left) ?? -1);
  if (priorityDifference) return priorityDifference;
  if (family === "opportunities") return Number(right.pinned) - Number(left.pinned);
  if (family === "contracts") {
    const rank = (item) => /near.expiry|expiring soon|recompete/i.test(lifecycleOf(item)) ? 2 : isUnknownTiming(item) ? 0 : 1;
    return rank(right) - rank(left);
  }
  const actionDate = (item) => parseDate(item.action_required_by || item.due_date || item.effective_date)?.getTime() || Infinity;
  return Number(isRhtRecord(right)) - Number(isRhtRecord(left)) || actionDate(left) - actionDate(right);
}

function renderMetrics() {
  const family = state.activeFamily;
  const items = state.cache[family] || [];
  let metrics;
  if (family === "opportunities") {
    const scored = items.map(priorityScore).filter((score) => score != null);
    const average = scored.length ? Math.round(scored.reduce((sum, score) => sum + score, 0) / scored.length) : 0;
    metrics = [["New opportunities", items.length], ["Pursue", items.filter((item) => item.status === "Pursue").length], ["Pinned", items.filter((item) => item.pinned).length], ["Average priority", average]];
  } else if (family === "contracts") {
    const current = items.filter((item) => !isExpired(item));
    const value = current.reduce((sum, item) => sum + Number(item.predictive_value_usd || item.potential_total_value || item.current_total_value || item.amount || item.award_amount || 0), 0);
    metrics = [["Current + unknown", current.length], ["Recompete watch", current.filter((item) => /near.expiry|expiring soon|recompete/i.test(lifecycleOf(item))).length], ["Unknown timing", current.filter(isUnknownTiming).length], ["Tracked value", compactMoney(value)]];
  } else {
    const action = items.filter((item) => isTrue(item.comment_required_flag));
    metrics = [["Updates", items.length], ["RHT signals", items.filter(isRhtRecord).length], ["Action required", action.length], ["Action dated", items.filter((item) => item.action_required_by || item.due_date).length]];
  }
  els.metrics.innerHTML = metrics.map(([label, value]) => `<article class="metric"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></article>`).join("");
}

function compactMoney(value) {
  if (!Number.isFinite(value) || value === 0) return "$0";
  return new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", notation: "compact", maximumFractionDigits: 1 }).format(value);
}

function tableColumns() {
  if (state.activeFamily === "opportunities") return ["Opportunity", "Jurisdiction", "Due", "Priority score", "Status"];
  if (state.activeFamily === "contracts") return ["Contract / incumbent", "Agency", "Jurisdiction", "Lifecycle", "End date", "Value", "Priority score"];
  return ["Update", "Jurisdiction", "Type", "RHT", "Posted", "Action date", "Priority score"];
}

function renderTable() {
  const family = state.activeFamily;
  const items = filteredItems();
  els.head.innerHTML = `<tr>${tableColumns().map((column) => `<th scope="col">${escapeHtml(column)}</th>`).join("")}</tr>`;
  els.count.textContent = `${number.format(items.length)} shown`;
  const selected = state.selected[family];
  if (!items.length) {
    const error = state.errors[family];
    els.rows.innerHTML = `<tr class="empty-row"><td colspan="${tableColumns().length}">${error ? `${escapeHtml(error.message)} <button class="inline-button" type="button" data-retry>Try again</button>` : "No records match the current filters."}</td></tr>`;
    return;
  }
  els.rows.innerHTML = items.map((item) => renderRow(item, family, recordId(item, family) === selected)).join("");
}

function renderRow(item, family, selected) {
  const id = escapeHtml(recordId(item, family));
  const title = escapeHtml(itemTitle(item, family));
  const selectedClass = selected ? "selected" : "";
  const commonStart = `<tr tabindex="0" data-id="${id}" class="${selectedClass}" aria-selected="${selected}">`;
  if (family === "opportunities") return `${commonStart}
    <td><div class="title-cell"><strong>${title}</strong>${item.pinned ? '<span class="pinned-label">Pinned</span>' : ""}<span>${escapeHtml(item.agency || item.source || "Agency not provided")}</span></div></td>
    <td>${escapeHtml(jurisdictionLabel(item, family))}</td><td>${formatDate(item.due_date, "No due date")}</td>
    <td>${priorityBadge(item)}</td><td><span class="badge ${badgeClass(item.status)}">${escapeHtml(item.status || "Unreviewed")}</span></td></tr>`;
  if (family === "contracts") {
    const lifecycle = lifecycleOf(item);
    const end = item.potential_end_date || item.end_date || item.period_end_date;
    const value = Number(item.predictive_value_usd || item.potential_total_value || item.current_total_value || item.amount || item.award_amount || 0);
    return `${commonStart}<td><div class="title-cell"><strong>${title}</strong><span>${escapeHtml(item.vendor_name || item.recipient_name || "Incumbent not provided")}</span></div></td>
      <td>${escapeHtml(item.agency || item.subagency || "Not provided")}</td><td>${escapeHtml(jurisdictionLabel(item, family))}</td>
      <td><span class="badge ${badgeClass(lifecycle)}">${escapeHtml(humanize(lifecycle))}</span></td><td>${formatDate(end, "Unknown")}</td>
      <td>${value ? escapeHtml(money.format(value)) : "Not provided"}</td><td>${priorityBadge(item)}</td></tr>`;
  }
  const type = item.record_type || item.update_type || item.type || "update";
  const rht = isRhtRecord(item);
  const action = item.action_required_by || item.due_date || item.effective_date;
  return `${commonStart}<td><div class="title-cell"><strong>${title}</strong><span>${escapeHtml(item.agency || item.source_key || "Agency not provided")}</span></div></td>
    <td>${escapeHtml(jurisdictionLabel(item, family))}</td><td><span class="badge badge-type">${escapeHtml(humanize(type))}</span></td>
    <td>${rht ? '<span class="badge badge-rht">RHT</span>' : '<span class="muted">—</span>'}</td><td>${formatDate(item.posted_date || item.updated_date)}</td>
    <td class="action-date ${action ? "has-action" : ""}">${formatDate(action, "No action date")}</td><td>${priorityBadge(item)}</td></tr>`;
}

function priorityBadge(item) {
  const score = priorityScore(item);
  return score == null ? '<span class="muted">—</span>' : `<span class="priority-table-score">${escapeHtml(scoreDisplay(score))}</span>`;
}

function renderActiveFamily() {
  const family = state.activeFamily;
  const config = FAMILIES[family];
  els.kicker.textContent = config.kicker;
  els.title.textContent = config.title;
  els.panel.setAttribute("aria-busy", "false");
  renderFamilyFilters();
  renderMetrics();
  renderTable();
  ensureSelection();
}

function ensureSelection() {
  const family = state.activeFamily;
  const visible = filteredItems();
  const selected = state.selected[family];
  const item = visible.find((candidate) => recordId(candidate, family) === selected) || visible[0];
  if (!item) {
    state.selected[family] = null;
    els.detail.innerHTML = `<div class="empty-detail"><h2>No record selected</h2><p class="muted">Adjust the filters to view records.</p></div>`;
    return;
  }
  const nextId = recordId(item, family);
  if (nextId !== selected) {
    state.selected[family] = nextId;
    renderTable();
  }
  showDetail(item, family);
}

async function loadFamily(family, force = false) {
  if (!force && state.cache[family]) return state.cache[family];
  if (state.requests[family]) return state.requests[family];
  state.errors[family] = null;
  const request = api(FAMILIES[family].endpoint)
    .then((payload) => {
      state.cache[family] = recordsFrom(payload, family);
      return state.cache[family];
    })
    .catch((error) => {
      state.errors[family] = error;
      throw error;
    })
    .finally(() => { state.requests[family] = null; });
  state.requests[family] = request;
  return request;
}

async function switchFamily(family) {
  if (!FAMILIES[family]) return;
  state.activeFamily = family;
  const token = ++state.switchToken;
  state.detailToken += 1;
  showNotice();
  [...els.tabs.querySelectorAll("[data-family]")].forEach((tab) => {
    const active = tab.dataset.family === family;
    tab.classList.toggle("active", active);
    tab.setAttribute("aria-selected", String(active));
    tab.tabIndex = active ? 0 : -1;
  });
  const config = FAMILIES[family];
  els.kicker.textContent = config.kicker;
  els.title.textContent = config.title;
  renderFamilyFilters();
  if (state.cache[family]) {
    renderActiveFamily();
    return;
  }
  els.panel.setAttribute("aria-busy", "true");
  els.metrics.innerHTML = [1, 2, 3, 4].map(() => '<article class="metric skeleton" aria-hidden="true"></article>').join("");
  els.head.innerHTML = "";
  els.rows.innerHTML = '<tr class="empty-row"><td>Loading records…</td></tr>';
  els.detail.innerHTML = '<p class="muted">Loading detail…</p>';
  try {
    await loadFamily(family);
    if (token === state.switchToken && family === state.activeFamily) renderActiveFamily();
  } catch (error) {
    if (token !== state.switchToken || family !== state.activeFamily) return;
    els.panel.setAttribute("aria-busy", "false");
    els.metrics.innerHTML = "";
    renderTable();
    els.detail.innerHTML = `<div class="error-state"><h2>Unable to load ${escapeHtml(config.title.toLowerCase())}</h2><p>${escapeHtml(error.message)}</p><button type="button" data-retry>Try again</button></div>`;
    showNotice(`Could not load ${config.title.toLowerCase()}. The other dashboard sections remain available.`, "error");
  }
}

function detailField(label, value, options = {}) {
  let display = value;
  if (options.date) display = formatDate(value, options.fallback || "Not provided");
  else if (options.money) display = Number(value) ? money.format(Number(value)) : "Not provided";
  else display = value || options.fallback || "Not provided";
  return `<div class="field"><span>${escapeHtml(label)}</span>${escapeHtml(display)}</div>`;
}

function sourceLink(item) {
  const url = safeUrl(item.document_url || item.source_url || normalizeList(item.source_urls)[0]);
  return url ? `<a class="source-link" href="${escapeHtml(url)}" target="_blank" rel="noopener noreferrer">Open official source <span aria-hidden="true">↗</span></a>` : '<span class="muted">Official source link not provided</span>';
}

function renderTags(values, empty = "No topics provided.") {
  const list = normalizeList(values);
  return list.length ? `<div class="tag-list">${list.map((value) => `<span>${escapeHtml(humanize(value))}</span>`).join("")}</div>` : `<p class="muted">${escapeHtml(empty)}</p>`;
}

function showDetail(item, family) {
  const id = recordId(item, family);
  const render = family === "opportunities" ? renderOpportunityDetail : family === "contracts" ? renderContractDetail : renderUpdateDetail;
  render(item);
  if (!id) return;
  const token = ++state.detailToken;
  api(`${FAMILIES[family].endpoint}/${encodeURIComponent(id)}`).then((detail) => {
    if (token === state.detailToken && state.activeFamily === family && state.selected[family] === id) render({ ...item, ...detail });
  }).catch((error) => {
    if (token === state.detailToken && state.activeFamily === family) showNotice(`${FAMILIES[family].title} detail could not be loaded: ${error.message}`, "error");
  });
}

function detailHeader(item, family, badges = "") {
  return `<div class="detail-header">${badges}<p class="detail-type">${escapeHtml(FAMILIES[family].kicker)}</p><h2>${escapeHtml(itemTitle(item, family))}</h2><p>${escapeHtml(item.agency || item.subagency || item.source || "Agency not provided")}</p></div>`;
}

function prioritySummary(item, family) {
  const priority = priorityData(item);
  const confidenceValue = Number(priority.confidence.value);
  const confidence = priority.confidence.label
    ? humanize(priority.confidence.label)
    : Number.isFinite(confidenceValue) ? `${Math.round(confidenceValue <= 1 ? confidenceValue * 100 : confidenceValue)}%` : "Not available";
  return `<section class="priority-hero" aria-label="Priority evaluation">
    <div class="priority-score"><span>Priority score</span><strong>${escapeHtml(scoreDisplay(priority.score))}</strong></div>
    <div><span>Confidence</span><strong>${escapeHtml(confidence)}</strong></div>
    <div><span>Recommended action</span><strong>${escapeHtml(priority.action)}</strong></div>
    <p>Scroll for score breakdown and supporting evidence.</p>
  </section>`;
}

function scoreBreakdown(item) {
  const dimensions = priorityData(item).dimensions;
  const rows = dimensions.length ? dimensions.map((dimension) => {
    const evidence = (Array.isArray(dimension.evidence) ? dimension.evidence : normalizeList(dimension.evidence))
      .map((value) => typeof value === "object" ? (value.claim || value.source_text || value.note || "") : value).filter(Boolean);
    const evidenceText = evidence.length ? evidence.slice(0, 3).join(" · ") : "No supporting evidence captured.";
    const missing = normalizeList(dimension.missing_notes || dimension.missing);
    return `<article class="dimension-row">
      <div><strong>${escapeHtml(humanize(dimension.dimension || dimension.name))}</strong><b>${escapeHtml(scoreDisplay(dimension.score))}/${escapeHtml(scoreDisplay(dimension.max ?? dimension.maximum))}</b></div>
      <p>${escapeHtml(evidenceText)}</p>
      ${missing.length ? `<p class="missing-note"><strong>Missing:</strong> ${escapeHtml(missing.join("; "))}</p>` : ""}
    </article>`;
  }).join("") : '<p class="muted">Score dimensions are not available for this record.</p>';
  return `<section class="section-block score-breakdown"><h3>Priority score breakdown</h3>${rows}</section>`;
}

function renderOpportunityDetail(item) {
  const history = Array.isArray(item.status_history) ? item.status_history : [];
  els.detail.innerHTML = `${detailHeader(item, "opportunities", `<span class="badge ${badgeClass(item.status)}">${escapeHtml(item.status || "Unreviewed")}</span>`)}
    ${prioritySummary(item, "opportunities")}
    <div class="action-row" aria-label="Opportunity actions">
      <button type="button" class="secondary-button ${item.pinned ? "is-pinned" : ""}" data-pinned="${!item.pinned}">${item.pinned ? "Unpin" : "Pin"}</button>
      ${["Pursue", "Monitor", "Decline"].map((status) => `<button type="button" data-status="${status}" ${item.status === status ? "disabled" : ""}>${status}</button>`).join("")}
      ${item.status && item.status !== "Unreviewed" ? '<button type="button" class="secondary-button remove-status" data-status="Unreviewed">Remove status</button>' : ""}
    </div>
    <p class="summary">${escapeHtml(item.summary || "No summary provided.")}</p>
    <div class="field-grid">${detailField("Jurisdiction", jurisdictionLabel(item, "opportunities"))}${detailField("Due date", item.due_date, { date: true })}${detailField("Budget", item.budget_estimate, { money: true })}${detailField("Notice type", item.document_type)}${detailField("Recommended action", item.recommended_action)}${detailField("Eligibility", item.eligibility)}</div>
    <section class="section-block"><h3>Why it fits</h3><p>${escapeHtml(item.eligibility_reason || "No fit explanation provided.")}</p></section>
    <section class="section-block"><h3>Program focus</h3>${renderTags(item.program_focus)}</section>
    <section class="section-block"><h3>Risks</h3>${renderTags(item.risks, "No risks captured.")}</section>
    ${history.length ? `<section class="section-block"><h3>Status history</h3><div class="stack-list">${history.map((event) => `<div class="stack-item"><strong>${escapeHtml(event.from)} → ${escapeHtml(event.to)}</strong><p>${formatDateTime(event.changed_at)} · ${escapeHtml(event.changed_by || "Unknown user")}</p></div>`).join("")}</div></section>` : ""}
    ${scoreBreakdown(item)}
    <div class="detail-source">${sourceLink(item)}</div>`;
}

function renderContractDetail(item) {
  const lifecycle = lifecycleOf(item);
  const unknown = isUnknownTiming(item);
  const value = item.predictive_value_usd || item.potential_total_value || item.current_total_value || item.amount || item.award_amount;
  els.detail.innerHTML = `${detailHeader(item, "contracts", `<span class="badge ${badgeClass(lifecycle)}">${escapeHtml(humanize(lifecycle))}</span>`)}
    ${prioritySummary(item, "contracts")}
    <div class="timing-callout ${unknown ? "neutral" : ""}"><strong>${unknown ? "Timing not established" : "Lifecycle timing"}</strong><p>${unknown ? "No reliable end date is available. This record is retained without implying urgency." : `Potential end: ${escapeHtml(formatDate(item.potential_end_date || item.end_date || item.period_end_date))}`}</p></div>
    <div class="field-grid">${detailField("Incumbent", item.vendor_name || item.recipient_name)}${detailField("Jurisdiction", jurisdictionLabel(item, "contracts"))}${detailField("Contract number", item.contract_number || item.piid)}${detailField("Vehicle", humanize(item.contract_vehicle || item.contract_record_type || item.document_type))}${detailField("Start date", item.period_start_date || item.start_date, { date: true })}${detailField("End date", item.potential_end_date || item.end_date || item.period_end_date, { date: true, fallback: "Unknown" })}${detailField("Tracked value", value, { money: true })}${detailField("Recompete window", item.recompete_window_start, { date: true })}</div>
    <section class="section-block"><h3>Program focus</h3>${renderTags(item.program_focus || item.topic_keys)}</section>
    <section class="section-block"><h3>Contract context</h3><p>${escapeHtml(item.summary || item.description || "No additional contract context provided.")}</p></section>
    ${scoreBreakdown(item)}
    <div class="detail-source">${sourceLink(item)}</div>`;
}

function renderUpdateDetail(item) {
  const rht = isRhtRecord(item);
  const actionRequired = isTrue(item.comment_required_flag);
  const action = item.action_required_by || item.due_date || item.effective_date;
  const type = item.record_type || item.update_type || item.type || "update";
  els.detail.innerHTML = `${detailHeader(item, "updates", `<span class="badge badge-type">${escapeHtml(humanize(type))}</span>${rht ? ' <span class="badge badge-rht">RHT signal</span>' : ""}`)}
    ${prioritySummary(item, "updates")}
    <div class="update-action ${actionRequired ? "required" : ""}"><span>${actionRequired ? "Action required" : "Action timing"}</span><strong>${escapeHtml(formatDate(action, "No action date provided"))}</strong>${item.effective_date ? `<small>Effective ${escapeHtml(formatDate(item.effective_date))}</small>` : ""}</div>
    <p class="summary">${escapeHtml(item.summary || "No summary provided.")}</p>
    <div class="field-grid">${detailField("Jurisdiction", jurisdictionLabel(item, "updates"))}${detailField("Posted", item.posted_date, { date: true })}${detailField("Updated", item.updated_date, { date: true })}${detailField("Docket", item.docket_id || item.regulation_id)}${detailField("Source", humanize(item.source_key || item.source))}</div>
    <section class="section-block"><h3>Program focus</h3>${renderTags(item.program_focus || item.topic_keys)}</section>
    ${scoreBreakdown(item)}
    <div class="detail-source">${sourceLink(item)}</div>`;
}

async function updateOpportunity(path, body) {
  const id = state.selected.opportunities;
  if (!id || state.activeFamily !== "opportunities") return;
  const detail = await api(`/api/opportunities/${encodeURIComponent(id)}/${path}`, { method: "POST", body: JSON.stringify(body) });
  state.cache.opportunities = (state.cache.opportunities || []).map((item) => recordId(item, "opportunities") === id ? { ...item, ...detail } : item);
  renderActiveFamily();
}

function rerenderFiltered() {
  if (!state.cache[state.activeFamily]) return;
  renderMetrics();
  renderTable();
  ensureSelection();
}

function isRhtRecord(item) {
  return String(item.rht_strength || "none").toLowerCase() !== "none";
}

async function loadFocus(view, force = false) {
  if (!force && state.focusCache[view]) return state.focusCache[view];
  if (state.focusRequests[view]) return state.focusRequests[view];
  const query = view === "competitors" && state.competitorQuery.trim() ? `?q=${encodeURIComponent(state.competitorQuery.trim())}` : "";
  const paths = [view === "rht" ? "/api/rht-overview" : `/api/competitors${query}`];
  const request = (async () => {
    let lastError;
    for (const path of paths) {
      try { return await api(path); } catch (error) { lastError = error; }
    }
    throw lastError || new Error("Focus view unavailable");
  })().then((payload) => { state.focusCache[view] = payload || {}; return state.focusCache[view]; })
    .finally(() => { state.focusRequests[view] = null; });
  state.focusRequests[view] = request;
  return request;
}

async function switchView(view, force = false) {
  if (!["records", "rht", "competitors"].includes(view)) return;
  state.activeView = view;
  const token = ++state.focusToken;
  [...els.focusTabs.querySelectorAll("[data-view]")].forEach((button) => {
    const active = button.dataset.view === view;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", String(active));
  });
  const focusActive = view !== "records";
  [...els.tabs.querySelectorAll("[data-family]")].forEach((button) => {
    const active = !focusActive && button.dataset.family === state.activeFamily;
    button.classList.toggle("active", active);
    button.setAttribute("aria-selected", String(active));
  });
  els.familyView.hidden = focusActive;
  els.focusView.hidden = !focusActive;
  if (view === "records") return;
  showNotice();
  els.focusView.setAttribute("aria-busy", "true");
  els.focusView.innerHTML = '<div class="focus-loading"><span class="spinner" aria-hidden="true"></span>Loading focus view…</div>';
  try {
    const payload = await loadFocus(view, force);
    if (token !== state.focusToken || state.activeView !== view) return;
    if (view === "rht") renderRhtFocus(payload);
    else renderCompetitorFocus(payload);
  } catch (error) {
    if (token !== state.focusToken || state.activeView !== view) return;
    els.focusView.innerHTML = `<div class="error-state"><h2>Unable to load ${view === "rht" ? "RHT tracker" : "Competitor Intelligence"}</h2><p>${escapeHtml(error.message)}</p><button type="button" data-focus-retry="${escapeHtml(view)}">Try again</button></div>`;
  } finally {
    if (token === state.focusToken) els.focusView.setAttribute("aria-busy", "false");
  }
}

function renderOverview(overview) {
  const entries = Object.entries(overview || {}).filter(([, value]) => ["string", "number"].includes(typeof value));
  return `<div class="focus-metrics">${entries.slice(0, 6).map(([label, value]) => `<article class="metric"><span>${escapeHtml(humanize(label))}</span><strong>${escapeHtml(value)}</strong></article>`).join("")}</div>`;
}

function focusFamilyOf(item) {
  const raw = String(item.family || "").toLowerCase();
  if (FAMILIES[raw]) return raw;
  if (["opportunity", "contract", "update"].includes(raw)) return `${raw}s`;
  if (/contract|award/.test(String(item.record_type || "").toLowerCase()) || item.period_end_date || item.vendor_name) return "contracts";
  if (item.action_required_by || item.comment_required_flag || /update|policy|docket/.test(String(item.record_type || "").toLowerCase())) return "updates";
  return "opportunities";
}

function focusRecordRow(item, family, selectedId = "") {
  const id = recordId(item, family);
  const value = Number(item.predictive_value_usd || item.potential_total_value || item.current_total_value || item.award_amount || item.amount || item.budget_estimate || 0);
  const timing = item.due_date || item.potential_end_date || item.end_date || item.period_end_date || item.action_required_by;
  return `<li><button class="focus-record-link ${id === selectedId ? "selected" : ""}" type="button" data-focus-preview="true" data-focus-family="${escapeHtml(family)}" data-focus-id="${escapeHtml(id)}">
    <span><strong>${escapeHtml(itemTitle(item, family))}</strong><small>${escapeHtml(jurisdictionLabel(item, family))} · ${escapeHtml(item.agency || item.vendor_name || "Agency not provided")}</small></span>
    <span>${priorityScore(item) == null ? "" : scoreBadge(priorityScore(item), `Priority ${scoreDisplay(priorityScore(item))}`)}<small>${timing ? escapeHtml(formatDate(timing)) : (value ? escapeHtml(compactMoney(value)) : "")}</small></span>
  </button></li>`;
}

function focusRecordDetail(item, family) {
  if (!item) return '<p class="muted">Select a record to review details.</p>';
  const timing = item.due_date || item.potential_end_date || item.end_date || item.period_end_date || item.action_required_by;
  const value = item.predictive_value_usd || item.potential_total_value || item.current_total_value || item.award_amount || item.amount || item.budget_estimate;
  return `${detailHeader(item, family, priorityScore(item) == null ? "" : scoreBadge(priorityScore(item), `Priority ${scoreDisplay(priorityScore(item))}`))}
    <p class="summary">${escapeHtml(item.summary || item.description || "No summary provided.")}</p>
    <div class="field-grid">${detailField("Jurisdiction", jurisdictionLabel(item, family))}${detailField("Timing", timing, { date: true })}${detailField("Agency / incumbent", item.agency || item.vendor_name || item.recipient_name)}${detailField("Tracked value", value, { money: true })}</div>
    <div class="focus-detail-actions"><button type="button" data-open-family="${escapeHtml(family)}" data-focus-id="${escapeHtml(recordId(item, family))}">Open in Family Records</button>${sourceLink(item)}</div>`;
}

function focusWorkspace(records, view, title, description = "") {
  const selectedKey = state.focusSelected[view];
  let selected = records.find((item) => `${focusFamilyOf(item)}:${recordId(item, focusFamilyOf(item))}` === selectedKey) || records[0];
  if (selected) state.focusSelected[view] = `${focusFamilyOf(selected)}:${recordId(selected, focusFamilyOf(selected))}`;
  const family = selected ? focusFamilyOf(selected) : "opportunities";
  const selectedId = selected ? recordId(selected, family) : "";
  return `<div class="focus-workspace"><section class="focus-section focus-results"><div><div><h3>${escapeHtml(title)}</h3>${description ? `<p>${escapeHtml(description)}</p>` : ""}</div><span>${number.format(records.length)} shown</span></div><ul>${records.length ? records.map((item) => focusRecordRow(item, focusFamilyOf(item), selectedId)).join("") : '<li class="muted">No collected records match this selection.</li>'}</ul></section><aside class="focus-section focus-detail">${focusRecordDetail(selected, family)}</aside></div>`;
}

function renderRhtFocus(payload) {
  const families = ["opportunities", "contracts", "updates"];
  const counts = payload.counts || {};
  const topRecords = Array.isArray(payload.top_records) ? payload.top_records : [];
  if (!families.includes(state.rhtFamily)) state.rhtFamily = families[0];
  const activeRecords = topRecords.filter((item) => focusFamilyOf(item) === state.rhtFamily);
  els.focusView.innerHTML = `<div class="primary-tabs focus-primary-tabs" role="tablist" aria-label="RHT record families">${families.map((family) => {
      const active = family === state.rhtFamily;
      return `<button type="button" role="tab" class="primary-tab ${active ? "active" : ""}" data-rht-family="${family}" aria-selected="${active}">${escapeHtml(FAMILIES[family].title)}</button>`;
    }).join("")}</div>
    ${focusWorkspace(activeRecords.map((item) => ({ ...item, family: state.rhtFamily })), "rht", FAMILIES[state.rhtFamily].title, "Rural Health Transformation signals across opportunities, contracts, and policy updates.")}`;
}

let competitorSearchTimer;
const COMPETITOR_OPTIONS = [
  ["maximus", "MAXIMUS"], ["deloitte", "Deloitte"], ["accenture", "Accenture"], ["optum", "Optum"],
  ["conduent", "Conduent"], ["acentra", "Acentra"], ["pcg", "PCG"], ["cgi", "CGI"],
];

function competitorRecords(payload) {
  if (state.competitorSelection === "search") return Array.isArray(payload.search?.records) ? payload.search.records : [];
  const profiles = Array.isArray(payload.profiles) ? payload.profiles : [];
  const selected = state.competitorSelection === "competitors"
    ? profiles.filter((profile) => profile.organization_type === "competitor")
    : state.competitorSelection === "custom"
      ? profiles.filter((profile) => state.competitorSelections.includes(profile.organization_key))
      : profiles.filter((profile) => profile.organization_key === state.competitorSelection);
  return selected.flatMap((profile) => (profile.top_records || []).map((record) => ({
    ...record,
    organization_key: profile.organization_key,
    organization_name: profile.organization_name,
    organization_type: profile.organization_type,
  })));
}

function competitorSummary(records) {
  const total = records.reduce((sum, item) => sum + Number(item.predictive_value_usd || item.potential_total_value || item.current_total_value || item.total_value || item.award_amount || item.amount || 0), 0);
  const windows = { Expired: 0, "0–90 days": 0, "91–180 days": 0, "181–365 days": 0, "Over 365 days": 0, Unknown: 0 };
  const jurisdictions = {};
  records.forEach((item) => {
    const label = jurisdictionLabel(item, item.family || "contracts");
    jurisdictions[label] = (jurisdictions[label] || 0) + 1;
    const date = parseDate(item.potential_end_date || item.end_date || item.period_end_date || item.due_date);
    if (!date) windows.Unknown += 1;
    else { const days = Math.ceil((date - new Date()) / 86400000); const key = days < 0 ? "Expired" : days <= 90 ? "0–90 days" : days <= 180 ? "91–180 days" : days <= 365 ? "181–365 days" : "Over 365 days"; windows[key] += 1; }
  });
  return { total, windows, jurisdictions };
}

function renderCompetitorFocus(payload) {
  const records = competitorRecords(payload);
  const topSelection = ["gainwell", "competitors", "search"].includes(state.competitorSelection) ? state.competitorSelection : "";
  els.focusView.innerHTML = `
    <div class="primary-tabs focus-primary-tabs competitor-primary" role="tablist" aria-label="Intelligence scope">${[["gainwell", "Gainwell"], ["competitors", "All competitors"], ["search", "Search"]].map(([key, label]) => `<button type="button" role="tab" class="primary-tab ${key === topSelection ? "active" : ""}" data-competitor="${key}" aria-selected="${key === topSelection}">${label}</button>`).join("")}</div>
    <div class="focus-content-shell">${state.competitorSelection !== "search" ? `<div class="competitor-filter-row"><div class="competitor-controls compact-controls" role="group" aria-label="Specific competitors">${COMPETITOR_OPTIONS.map(([key, label]) => `<button type="button" class="competitor-option ${state.competitorSelections.includes(key) ? "active" : ""}" data-competitor-toggle="${key}" aria-pressed="${state.competitorSelections.includes(key)}">${escapeHtml(label)}</button>`).join("")}</div><button type="button" class="secondary-button" data-clear-competitors>Clear</button></div>` : `<div class="focus-search-row"><input type="search" aria-label="Search collected records" data-competitor-search value="${escapeHtml(state.competitorQuery)}" placeholder="Search by title, agency, vendor, or topic" autocomplete="off"><button type="button" class="secondary-button" data-clear-competitor-search>Clear</button></div>`}
    ${focusWorkspace(records, "competitors", "Matching records")}</div>`;
}

function openFamilyRecords(family, id = "", rhtOnly = false) {
  if (!FAMILIES[family]) return;
  state.rhtOnly = rhtOnly;
  if (id) state.selected[family] = id;
  switchView("records");
  switchFamily(family).then(() => {
    if (rhtOnly) showNotice(`Showing Rural Health Transformation records in ${FAMILIES[family].title}.`, "success");
  });
}

els.tabs.addEventListener("click", (event) => {
  const tab = event.target.closest("[data-family]");
  if (tab) {
    state.rhtOnly = false;
    switchView("records");
    switchFamily(tab.dataset.family);
  }
});
els.tabs.addEventListener("keydown", (event) => {
  if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
  const tabs = [...els.tabs.querySelectorAll("[data-family]")];
  const current = tabs.indexOf(event.target.closest("[data-family]"));
  if (current < 0) return;
  event.preventDefault();
  const next = event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1
    : (current + (event.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length;
  tabs[next].focus();
  state.rhtOnly = false;
  switchView("records");
  switchFamily(tabs[next].dataset.family);
});

els.filters.addEventListener("input", (event) => {
  const key = event.target.dataset.filter;
  if (!key) return;
  state.filters[state.activeFamily][key] = event.target.value;
  rerenderFiltered();
});
els.filters.addEventListener("change", (event) => {
  const key = event.target.dataset.filter;
  if (!key) return;
  state.filters[state.activeFamily][key] = event.target.value;
  rerenderFiltered();
});
els.search.addEventListener("input", () => { state.rhtOnly = false; rerenderFiltered(); });
els.jurisdiction.addEventListener("change", rerenderFiltered);

function activateRow(target) {
  const row = target.closest("tr[data-id]");
  if (!row) return;
  const family = state.activeFamily;
  state.selected[family] = row.dataset.id;
  state.detailToken += 1;
  renderTable();
  const item = (state.cache[family] || []).find((candidate) => recordId(candidate, family) === row.dataset.id);
  if (item) showDetail(item, family);
}

els.rows.addEventListener("click", (event) => {
  if (event.target.closest("[data-retry]")) {
    loadFamily(state.activeFamily, true).then(renderActiveFamily).catch(() => renderTable());
    return;
  }
  activateRow(event.target);
});
els.rows.addEventListener("keydown", (event) => {
  if (event.key === "Enter" || event.key === " ") { event.preventDefault(); activateRow(event.target); }
});
els.detail.addEventListener("click", (event) => {
  const retry = event.target.closest("[data-retry]");
  if (retry) { switchFamily(state.activeFamily); return; }
  const pin = event.target.closest("[data-pinned]");
  if (pin) { updateOpportunity("pin", { pinned: pin.dataset.pinned === "true" }).catch((error) => showNotice(error.message, "error")); return; }
  const status = event.target.closest("[data-status]");
  if (status) updateOpportunity("status", { status: status.dataset.status }).catch((error) => showNotice(error.message, "error"));
});

els.focusTabs.addEventListener("click", (event) => {
  const button = event.target.closest("[data-view]");
  if (button) switchView(button.dataset.view);
});
els.focusView.addEventListener("click", (event) => {
  const retry = event.target.closest("[data-focus-retry]");
  if (retry) { switchView(retry.dataset.focusRetry, true); return; }
  const clearSearch = event.target.closest("[data-clear-competitor-search]");
  if (clearSearch) { state.competitorQuery = ""; renderCompetitorFocus(state.focusCache.competitors || {}); els.focusView.querySelector("[data-competitor-search]")?.focus(); return; }
  const clearCompetitors = event.target.closest("[data-clear-competitors]");
  if (clearCompetitors) { state.competitorSelections = []; state.competitorSelection = "custom"; renderCompetitorFocus(state.focusCache.competitors || {}); return; }
  const competitorToggle = event.target.closest("[data-competitor-toggle]");
  if (competitorToggle) {
    const key = competitorToggle.dataset.competitorToggle;
    state.competitorSelections = state.competitorSelections.includes(key) ? state.competitorSelections.filter((item) => item !== key) : [...state.competitorSelections, key];
    state.competitorSelection = "custom";
    state.focusSelected.competitors = null;
    renderCompetitorFocus(state.focusCache.competitors || {});
    return;
  }
  const competitor = event.target.closest("[data-competitor]");
  if (competitor) { state.competitorSelection = competitor.dataset.competitor; if (competitor.dataset.competitor !== "search") state.competitorSelections = []; renderCompetitorFocus(state.focusCache.competitors || {}); return; }
  const rhtFamily = event.target.closest("[data-rht-family]");
  if (rhtFamily) { state.rhtFamily = rhtFamily.dataset.rhtFamily; state.focusSelected.rht = null; renderRhtFocus(state.focusCache.rht || {}); return; }
  const openRecord = event.target.closest("[data-open-family]");
  if (openRecord) { openFamilyRecords(openRecord.dataset.openFamily, openRecord.dataset.focusId || ""); return; }
  const preview = event.target.closest("[data-focus-preview]");
  if (preview) {
    state.focusSelected[state.activeView] = `${preview.dataset.focusFamily}:${preview.dataset.focusId}`;
    if (state.activeView === "rht") renderRhtFocus(state.focusCache.rht || {});
    else renderCompetitorFocus(state.focusCache.competitors || {});
    return;
  }
  const record = event.target.closest("[data-focus-family]");
  if (record) openFamilyRecords(record.dataset.focusFamily, record.dataset.focusId || "", record.dataset.rhtFilter === "true");
});
els.focusView.addEventListener("input", (event) => {
  if (!event.target.matches("[data-competitor-search]")) return;
  state.competitorQuery = event.target.value;
  clearTimeout(competitorSearchTimer);
  competitorSearchTimer = setTimeout(async () => {
    const query = state.competitorQuery.trim();
    try {
      const payload = await api(`/api/competitors${query ? `?q=${encodeURIComponent(query)}` : ""}`);
      if (state.activeView !== "competitors" || query !== state.competitorQuery.trim()) return;
      state.focusCache.competitors = payload;
      renderCompetitorFocus(payload);
      const input = els.focusView.querySelector("[data-competitor-search]");
      input?.focus();
      input?.setSelectionRange(state.competitorQuery.length, state.competitorQuery.length);
    } catch (error) {
      showNotice(`Competitor search failed: ${error.message}`, "error");
    }
  }, 250);
});

switchFamily("opportunities");
