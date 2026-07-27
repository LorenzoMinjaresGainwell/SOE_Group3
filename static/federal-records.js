const recordState = {
  records: [],
  selectedId: null,
  category: "all",
};

const recordCategories = [
  ["all", "All records"],
  ["policy_regulatory", "Policy & regulatory"],
  ["medicaid_data", "Medicaid data"],
  ["provider_data", "Provider data"],
];

const recordEls = {
  metrics: document.querySelector("#recordMetrics"),
  tabs: document.querySelector("#recordTabs"),
  search: document.querySelector("#recordSearch"),
  date: document.querySelector("#recordDate"),
  count: document.querySelector("#recordCount"),
  rows: document.querySelector("#recordRows"),
  detail: document.querySelector("#recordDetail"),
};

async function recordApi(path) {
  const response = await fetch(path, { cache: "no-store" });
  if (!response.ok) {
    const error = await response.json().catch(() => ({ error: response.statusText }));
    throw new Error(error.error || "Request failed");
  }
  return response.json();
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

function formatRecordDate(value) {
  if (!value) return "Unknown";
  return new Date(`${value}T00:00:00`).toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
    year: "numeric",
  });
}

function recordBadgeClass(category) {
  if (category === "policy_regulatory") return "record-type-policy";
  if (category === "medicaid_data") return "record-type-medicaid";
  return "record-type-provider";
}

function matchesRecordFilters(record) {
  const query = recordEls.search.value.trim().toLowerCase();
  const since = recordEls.date.value;
  const haystack = [
    record.title,
    record.agency,
    record.source,
    record.document_type,
    record.summary,
    record.keywords_matched.join(" "),
  ].join(" ").toLowerCase();
  return (!query || haystack.includes(query))
    && (!since || (record.posted_date && record.posted_date >= since));
}

function filteredRecords() {
  return recordState.records.filter((record) =>
    matchesRecordFilters(record)
      && (recordState.category === "all" || record.record_category === recordState.category)
  );
}

function renderRecordMetrics() {
  const policy = recordState.records.filter((record) => record.record_category === "policy_regulatory").length;
  const medicaid = recordState.records.filter((record) => record.record_category === "medicaid_data").length;
  const provider = recordState.records.filter((record) => record.record_category === "provider_data").length;
  recordEls.metrics.innerHTML = [
    ["Total records", recordState.records.length],
    ["Policy & regulatory", policy],
    ["Medicaid data", medicaid],
    ["Provider data", provider],
  ].map(([label, value]) => `<article class="metric"><span>${label}</span><strong>${value}</strong></article>`).join("");
}

function renderRecordTabs() {
  const toolbarMatches = recordState.records.filter(matchesRecordFilters);
  recordEls.tabs.innerHTML = recordCategories.map(([value, label]) => {
    const count = value === "all"
      ? toolbarMatches.length
      : toolbarMatches.filter((record) => record.record_category === value).length;
    return `
      <button
        type="button"
        class="category-tab ${recordState.category === value ? "active" : ""}"
        data-record-category="${value}"
        aria-pressed="${recordState.category === value}"
      >
        ${label}<span>${count}</span>
      </button>
    `;
  }).join("");
}

function renderRecordTable() {
  const records = filteredRecords();
  recordEls.count.textContent = `${records.length} shown`;
  recordEls.rows.innerHTML = records.length ? records.map((record) => `
    <tr data-record-id="${escapeRecordHtml(record.id)}" class="${record.id === recordState.selectedId ? "selected" : ""}">
      <td>
        <div class="title-cell">
          <strong>${escapeRecordHtml(record.title)}</strong>
          <span>${escapeRecordHtml(record.agency)} | ${escapeRecordHtml(record.document_type)}</span>
        </div>
      </td>
      <td><span class="badge ${recordBadgeClass(record.record_category)}">${escapeRecordHtml(record.record_category_label)}</span></td>
      <td>${formatRecordDate(record.posted_date)}</td>
      <td><span class="badge ${record.fit_score >= 60 ? "score-mid" : "score-low"}">${record.fit_score}</span></td>
    </tr>
  `).join("") : `
    <tr class="empty-row">
      <td colspan="4">No federal records match the selected category and filters.</td>
    </tr>
  `;
}

function renderRecordDetail(record) {
  recordEls.detail.innerHTML = `
    <div class="detail-header">
      <span class="badge ${recordBadgeClass(record.record_category)}">${escapeRecordHtml(record.record_category_label)}</span>
      <h2>${escapeRecordHtml(record.title)}</h2>
      <div class="detail-meta">
        <span>${escapeRecordHtml(record.agency)}</span>
        <span>${escapeRecordHtml(record.source)}</span>
      </div>
    </div>

    <p class="read-only-note">Informational federal record—not an open procurement or grant opportunity.</p>
    <p class="summary">${escapeRecordHtml(record.summary)}</p>

    <div class="field-grid">
      <div class="field"><span>Published</span>${formatRecordDate(record.posted_date)}</div>
      <div class="field"><span>Document type</span>${escapeRecordHtml(record.document_type)}</div>
      <div class="field"><span>Relevance score</span>${record.fit_score}</div>
      <div class="field"><span>Official record</span>${record.document_url ? `<a class="record-link" href="${escapeRecordHtml(record.document_url)}" target="_blank" rel="noopener">Open source</a>` : "Not available"}</div>
    </div>

    <section class="section-block">
      <h3>Why it matters</h3>
      <p>${escapeRecordHtml(record.eligibility_reason)}</p>
    </section>

    <section class="section-block">
      <h3>Matched topics</h3>
      ${record.keywords_matched.length
        ? `<div class="tag-list">${record.keywords_matched.map((keyword) => `<span>${escapeRecordHtml(keyword)}</span>`).join("")}</div>`
        : `<p class="muted">No monitored keywords matched.</p>`}
    </section>
  `;
}

function renderRecordFilteredView() {
  renderRecordTabs();
  renderRecordTable();
}

async function initFederalRecords() {
  recordState.records = await recordApi("/api/federal-records");
  recordState.selectedId = recordState.records[0]?.id || null;
  renderRecordMetrics();
  renderRecordFilteredView();
  if (recordState.selectedId) {
    renderRecordDetail(recordState.records[0]);
  }
}

recordEls.tabs.addEventListener("click", (event) => {
  const tab = event.target.closest("button[data-record-category]");
  if (!tab) return;
  recordState.category = tab.dataset.recordCategory;
  renderRecordFilteredView();
  document.querySelector(".table-wrap").scrollTop = 0;
});

recordEls.rows.addEventListener("click", (event) => {
  const row = event.target.closest("tr[data-record-id]");
  if (!row) return;
  recordState.selectedId = row.dataset.recordId;
  renderRecordTable();
  const record = recordState.records.find((item) => item.id === recordState.selectedId);
  if (record) renderRecordDetail(record);
});

recordEls.search.addEventListener("input", renderRecordFilteredView);
recordEls.date.addEventListener("change", renderRecordFilteredView);

initFederalRecords().catch((error) => {
  recordEls.detail.innerHTML = `<p class="muted">${escapeRecordHtml(error.message)}</p>`;
});
