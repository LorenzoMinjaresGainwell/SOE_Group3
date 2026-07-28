const recordState = {
  records: [],
  selectedId: null,
  topic: "all",
  detailRequest: 0,
};

const recordTopics = [
  ["all", "All updates"],
  ["rht", "Rural health / RHT"],
  ["medicaid", "Medicaid"],
  ["medicare", "Medicare"],
  ["cms", "CMS"],
];

const recordEls = {
  metrics: document.querySelector("#recordMetrics"),
  tabs: document.querySelector("#recordTabs"),
  search: document.querySelector("#recordSearch"),
  source: document.querySelector("#recordSource"),
  type: document.querySelector("#recordType"),
  date: document.querySelector("#recordDate"),
  importance: document.querySelector("#recordImportance"),
  count: document.querySelector("#recordCount"),
  rows: document.querySelector("#recordRows"),
  detail: document.querySelector("#recordDetail"),
};

async function recordApi(path) {
  const response = await fetch(path, { cache: "no-store", headers: { Accept: "application/json" } });
  const payload = await response.json().catch(() => null);
  if (!response.ok) {
    throw new Error(payload?.error || payload?.message || `Request failed (${response.status})`);
  }
  if (payload === null) throw new Error("The federal source API returned invalid JSON.");
  return payload;
}

function escapeRecordHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#039;",
  })[char]);
}

function textValue(value, fallback = "Not available") {
  const text = String(value ?? "").trim();
  return text || fallback;
}

function listValue(value) {
  if (Array.isArray(value)) return value.map((item) => String(item).trim()).filter(Boolean);
  return String(value ?? "").split(/[;,|]/).map((item) => item.trim()).filter(Boolean);
}

function numberValue(value) {
  const number = Number(value);
  return Number.isFinite(number) ? Math.max(0, Math.min(100, number)) : 0;
}

function trueValue(value) {
  return value === true || ["true", "yes", "1"].includes(String(value ?? "").toLowerCase());
}

function safeRecordUrl(value) {
  try {
    const url = new URL(String(value ?? ""), window.location.origin);
    return ["http:", "https:"].includes(url.protocol) ? url.href : "";
  } catch (_error) {
    return "";
  }
}

function formatRecordDate(value, fallback = "Unknown") {
  if (!value) return fallback;
  const date = new Date(/^\d{4}-\d{2}-\d{2}$/.test(String(value)) ? `${value}T00:00:00Z` : value);
  if (Number.isNaN(date.getTime())) return fallback;
  return date.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric", timeZone: "UTC" });
}

function titleCase(value) {
  return textValue(value).replace(/[_-]+/g, " ").replace(/\b\w/g, (char) => char.toUpperCase());
}

function sourceLabel(record) {
  return textValue(record.source || record.source_name || (record.source_key ? titleCase(record.source_key) : ""), "Unknown source");
}

function typeLabel(record) {
  return titleCase(record.record_type || record.document_type || "Update");
}

function recordId(record) {
  return String(record.update_id || record.id || record.source_record_id || "").trim();
}

function normalizeRecord(record) {
  const normalized = record && typeof record === "object" ? { ...record } : {};
  normalized.id = recordId(normalized);
  normalized.importance = numberValue(normalized.importance_score ?? normalized.fit_score);
  normalized.topics = [...new Set([
    ...listValue(normalized.topic_keys),
    ...listValue(normalized.program_focus),
    ...listValue(normalized.keywords_matched),
    ...(trueValue(normalized.rht_flag) ? ["rht"] : []),
  ].map((topic) => topic.toLowerCase()))];
  return normalized;
}

function excludedRecord(record) {
  const source = String(record.source_key || record.source || "").toLowerCase();
  const type = String(record.record_type || record.document_type || "").toLowerCase();
  const excludedSource = /(sam[_ .-]?opportunit|grants?\.gov|federal_grants|contract|usaspending)/.test(source);
  const excludedType = /(opportunit|solicitation|procurement|contract|award|grant|sources sought|request for (information|proposal|quote)|\brfi\b|\brfp\b|\brfq\b)/.test(type);
  return excludedSource || excludedType;
}

function topicMatches(record, topic) {
  if (topic === "all") return true;
  if (topic === "rht") return trueValue(record.rht_flag) || record.topics.some((item) => item === "rht" || item.includes("rural health"));
  return record.topics.some((item) => item === topic || item.includes(topic));
}

function searchableText(record) {
  return [
    record.title, record.agency, sourceLabel(record), typeLabel(record), record.summary,
    record.source_record_id, record.docket_id, record.regulation_id,
    record.topic_keys, record.program_focus, record.score_evidence_json,
  ].flatMap(listValue).join(" ").toLowerCase();
}

function matchesToolbarFilters(record) {
  const query = recordEls.search.value.trim().toLowerCase();
  const since = recordEls.date.value;
  const recordDate = record.posted_date || record.updated_date || record.effective_date || "";
  return (!query || searchableText(record).includes(query))
    && (!recordEls.source.value || sourceLabel(record) === recordEls.source.value)
    && (!recordEls.type.value || typeLabel(record) === recordEls.type.value)
    && (!since || (recordDate && recordDate >= since))
    && record.importance >= numberValue(recordEls.importance.value);
}

function filteredRecords() {
  return recordState.records.filter((record) => matchesToolbarFilters(record) && topicMatches(record, recordState.topic));
}

function scoreClass(score) {
  if (score >= 75) return "score-high";
  if (score >= 50) return "score-mid";
  return "score-low";
}

function recordBadgeClass(record) {
  const source = String(record.source_key || sourceLabel(record)).toLowerCase();
  if (source.includes("register") || source.includes("regulation")) return "record-type-policy";
  if (source.includes("medicaid")) return "record-type-medicaid";
  return "record-type-provider";
}

function renderRecordMetrics() {
  const sourceCount = new Set(recordState.records.map(sourceLabel)).size;
  const highImportance = recordState.records.filter((record) => record.importance >= 75).length;
  const actionNeeded = recordState.records.filter((record) => trueValue(record.comment_required_flag) || record.action_required_by).length;
  recordEls.metrics.innerHTML = [
    ["Research updates", recordState.records.length],
    ["Official sources", sourceCount],
    ["High importance", highImportance],
    ["Action / comment dates", actionNeeded],
  ].map(([label, value]) => `<article class="metric"><span>${label}</span><strong>${value}</strong></article>`).join("");
}

function renderFilterOptions() {
  const setOptions = (element, values, initialLabel) => {
    const current = element.value;
    element.innerHTML = `<option value="">${initialLabel}</option>${values.map((value) => (
      `<option value="${escapeRecordHtml(value)}">${escapeRecordHtml(value)}</option>`
    )).join("")}`;
    if (values.includes(current)) element.value = current;
  };
  setOptions(recordEls.source, [...new Set(recordState.records.map(sourceLabel))].sort(), "All sources");
  setOptions(recordEls.type, [...new Set(recordState.records.map(typeLabel))].sort(), "All types");
}

function renderRecordTabs() {
  const toolbarMatches = recordState.records.filter(matchesToolbarFilters);
  recordEls.tabs.innerHTML = recordTopics.map(([value, label]) => {
    const count = toolbarMatches.filter((record) => topicMatches(record, value)).length;
    return `<button type="button" class="category-tab ${recordState.topic === value ? "active" : ""}"
      data-record-topic="${value}" aria-pressed="${recordState.topic === value}">${label}<span>${count}</span></button>`;
  }).join("");
}

function renderRecordTable() {
  const records = filteredRecords();
  recordEls.count.textContent = `${records.length} update${records.length === 1 ? "" : "s"} shown`;
  recordEls.rows.innerHTML = records.length ? records.map((record) => `
    <tr data-record-id="${escapeRecordHtml(record.id)}" class="${record.id === recordState.selectedId ? "selected" : ""}">
      <td><div class="title-cell"><strong>${escapeRecordHtml(textValue(record.title, "Untitled federal update"))}</strong>
        <span>${escapeRecordHtml(textValue(record.agency, "Agency not supplied"))}</span></div></td>
      <td><span class="badge ${recordBadgeClass(record)}">${escapeRecordHtml(sourceLabel(record))}</span><br>${escapeRecordHtml(typeLabel(record))}</td>
      <td>${formatRecordDate(record.posted_date || record.updated_date || record.effective_date)}</td>
      <td><span class="badge ${scoreClass(record.importance)}">${record.importance}</span></td>
    </tr>`).join("") : `<tr class="empty-row"><td colspan="4">No research updates match these filters. Opportunity and contract records are never shown.</td></tr>`;
}

function parseEvidence(value) {
  if (value && typeof value === "object") return value;
  try {
    const parsed = JSON.parse(String(value || ""));
    return parsed && typeof parsed === "object" ? parsed : {};
  } catch (_error) {
    return value ? { supplied_evidence: String(value) } : {};
  }
}

function displayEvidenceValue(value) {
  if (Array.isArray(value)) return value.join(", ") || "None";
  if (value && typeof value === "object") return JSON.stringify(value);
  if (typeof value === "boolean") return value ? "Yes" : "No";
  return textValue(value);
}

function renderEvidence(record) {
  const evidence = parseEvidence(record.score_evidence_json || record.score_evidence);
  const entries = Object.entries(evidence);
  if (!entries.length) return `<p class="muted">The source did not supply scoring evidence for this update.</p>`;
  return `<div class="stack-list">${entries.map(([key, value]) => `
    <div class="stack-item"><strong>${escapeRecordHtml(titleCase(key))}</strong>
      <p>${escapeRecordHtml(displayEvidenceValue(value))}</p></div>`).join("")}</div>`;
}

function linkHtml(url, label) {
  const safeUrl = safeRecordUrl(url);
  return safeUrl ? `<a class="record-link" href="${escapeRecordHtml(safeUrl)}" target="_blank" rel="noopener noreferrer">${label}</a>` : "Not available";
}

function detailFields(record) {
  const fields = [
    ["Published", formatRecordDate(record.posted_date)],
    ["Updated", formatRecordDate(record.updated_date)],
    ["Effective", formatRecordDate(record.effective_date)],
    ["Action required by", formatRecordDate(record.action_required_by || record.due_date)],
    ["Docket ID", textValue(record.docket_id)],
    ["Regulation ID", textValue(record.regulation_id)],
    ["Importance", `${record.importance} / 100`],
    ["Comment required", trueValue(record.comment_required_flag) ? "Yes" : "No"],
  ];
  return fields.map(([label, value]) => `<div class="field"><span>${label}</span>${escapeRecordHtml(value)}</div>`).join("");
}

function renderRecordDetail(record) {
  const topics = [...new Set([...record.topics, ...listValue(record.vendor_keys_mentioned)])];
  const officialUrl = record.document_url || record.official_url;
  recordEls.detail.innerHTML = `
    <div class="detail-header">
      <span class="badge ${recordBadgeClass(record)}">${escapeRecordHtml(typeLabel(record))}</span>
      <h2>${escapeRecordHtml(textValue(record.title, "Untitled federal update"))}</h2>
      <div class="detail-meta"><span>${escapeRecordHtml(textValue(record.agency, "Agency not supplied"))}</span><span>${escapeRecordHtml(sourceLabel(record))}</span></div>
    </div>
    <p class="read-only-note">Research evidence from an official federal source—not a bid, grant opportunity, procurement, contract, or award record.</p>
    <p class="summary">${escapeRecordHtml(textValue(record.summary, "No source summary was supplied. Use the official link and provenance below to continue review."))}</p>
    <div class="field-grid">${detailFields(record)}</div>
    <section class="section-block"><h3>Official research links</h3>
      <p>Document: ${linkHtml(officialUrl, "Open official document")}</p>
      <p>Source/API: ${linkHtml(record.source_url, "Open source endpoint")}</p>
    </section>
    <section class="section-block"><h3>Topics and program signals</h3>
      ${topics.length ? `<div class="tag-list">${topics.map((topic) => `<span>${escapeRecordHtml(titleCase(topic))}</span>`).join("")}</div>` : `<p class="muted">No topic signals were supplied.</p>`}
    </section>
    <section class="section-block"><h3>Importance scoring evidence</h3>${renderEvidence(record)}</section>
    <section class="section-block"><h3>Provenance</h3>
      <div class="field-grid">
        <div class="field"><span>Catalog update ID</span>${escapeRecordHtml(textValue(record.update_id || record.id))}</div>
        <div class="field"><span>Source record ID</span>${escapeRecordHtml(textValue(record.source_record_id))}</div>
        <div class="field"><span>Source key</span>${escapeRecordHtml(textValue(record.source_key))}</div>
        <div class="field"><span>Last checked</span>${escapeRecordHtml(textValue(record.last_checked_at))}</div>
      </div>
    </section>`;
}

function renderRecordFilteredView() {
  renderRecordTabs();
  renderRecordTable();
}

async function selectRecord(id) {
  const fallback = recordState.records.find((record) => record.id === id);
  if (!fallback) return;
  recordState.selectedId = id;
  renderRecordTable();
  recordEls.detail.innerHTML = `<p class="muted">Loading source evidence…</p>`;
  const request = ++recordState.detailRequest;
  try {
    const payload = await recordApi(`/api/federal-records/${encodeURIComponent(id)}`);
    if (request !== recordState.detailRequest) return;
    const detail = normalizeRecord(payload.record || payload.update || payload.data || payload);
    if (excludedRecord(detail)) throw new Error("This item is an opportunity or contract record and cannot be displayed here.");
    renderRecordDetail({ ...fallback, ...detail, id: detail.id || fallback.id });
  } catch (error) {
    if (request !== recordState.detailRequest) return;
    renderRecordDetail(fallback);
    recordEls.detail.insertAdjacentHTML("afterbegin", `<p class="read-only-note">Full detail could not be loaded: ${escapeRecordHtml(error.message)}</p>`);
  }
}

async function initFederalRecords() {
  recordEls.rows.innerHTML = `<tr class="empty-row"><td colspan="4">Loading federal research updates…</td></tr>`;
  const payload = await recordApi("/api/federal-records");
  const rows = Array.isArray(payload) ? payload : (payload.records || payload.updates || payload.items || payload.data || []);
  if (!Array.isArray(rows)) throw new Error("The federal source API returned an unexpected response.");
  recordState.records = rows.map(normalizeRecord).filter((record) => record.id && !excludedRecord(record));
  renderFilterOptions();
  renderRecordMetrics();
  renderRecordFilteredView();
  if (recordState.records[0]) await selectRecord(recordState.records[0].id);
  else recordEls.detail.innerHTML = `<p class="muted">No non-procurement federal research updates are currently available.</p>`;
}

recordEls.tabs.addEventListener("click", (event) => {
  const tab = event.target.closest("button[data-record-topic]");
  if (!tab) return;
  recordState.topic = tab.dataset.recordTopic;
  renderRecordFilteredView();
  document.querySelector(".table-wrap").scrollTop = 0;
});

recordEls.rows.addEventListener("click", (event) => {
  const row = event.target.closest("tr[data-record-id]");
  if (row) selectRecord(row.dataset.recordId);
});

recordEls.search.addEventListener("input", renderRecordFilteredView);
recordEls.source.addEventListener("change", renderRecordFilteredView);
recordEls.type.addEventListener("change", renderRecordFilteredView);
recordEls.date.addEventListener("change", renderRecordFilteredView);
recordEls.importance.addEventListener("input", renderRecordFilteredView);

initFederalRecords().catch((error) => {
  recordEls.rows.innerHTML = `<tr class="empty-row"><td colspan="4">Federal updates could not be loaded.</td></tr>`;
  recordEls.count.textContent = "Unavailable";
  recordEls.detail.innerHTML = `<p class="read-only-note">${escapeRecordHtml(error.message)}</p><p class="muted">Try again later or return to the dashboard.</p>`;
});
