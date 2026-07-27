const state = {
  opportunities: [],
  selectedId: null,
  category: "all",
};

const money = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 0 });
const categoryTabs = [
  ["all", "All"],
  ["state_opportunities", "State opportunities"],
  ["federal_opportunities", "Federal opportunities"],
  ["grants", "Grants"],
  ["competitor_signals", "Competitor signals"],
  ["contract_expirations", "Contract expirations"],
];

const els = {
  metrics: document.querySelector("#metrics"),
  rows: document.querySelector("#opportunityRows"),
  detail: document.querySelector("#detailPanel"),
  count: document.querySelector("#resultCount"),
  search: document.querySelector("#searchInput"),
  status: document.querySelector("#statusFilter"),
  score: document.querySelector("#scoreFilter"),
  sources: document.querySelector("#sourcesList"),
  rules: document.querySelector("#rulesList"),
  tabs: document.querySelector("#categoryTabs"),
  sourceCount: document.querySelector("#sourceCount"),
  ruleCount: document.querySelector("#ruleCount"),
};

async function api(path, options = {}) {
  const response = await fetch(path, {
    cache: "no-store",
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({ error: response.statusText }));
    throw new Error(error.error || "Request failed");
  }
  return response.json();
}

function formatDate(value) {
  if (!value) return "Unknown";
  return new Date(`${value}T00:00:00`).toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
}

function formatDateTime(value) {
  if (!value) return "Unknown";
  return new Date(value).toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#039;",
  })[char]);
}

function scoreClass(score) {
  if (score >= 80) return "score-high";
  if (score >= 60) return "score-mid";
  return "score-low";
}

function statusClass(status) {
  if (status === "Pursue") return "status-pursue";
  if (status === "Decline") return "status-decline";
  return "status";
}

function refreshClass(label) {
  return `refresh-${String(label || "").toLowerCase().replace(/\s+/g, "-")}`;
}

function matchesToolbarFilters(opportunity) {
  const query = els.search.value.trim().toLowerCase();
  const status = els.status.value;
  const minScore = Number(els.score.value || 0);
  const haystack = [
    opportunity.title,
    opportunity.state,
    opportunity.agency,
    opportunity.program_focus.join(" "),
    opportunity.keywords_matched.join(" "),
  ].join(" ").toLowerCase();
  return (!query || haystack.includes(query))
    && (!status || opportunity.status === status)
    && opportunity.fit_score >= minScore;
}

function filteredOpportunities() {
  return state.opportunities.filter((opportunity) =>
    matchesToolbarFilters(opportunity)
      && (state.category === "all" || opportunity.categories.includes(state.category))
  );
}

function renderCategoryTabs() {
  const toolbarMatches = state.opportunities.filter(matchesToolbarFilters);
  els.tabs.innerHTML = categoryTabs.map(([value, label]) => {
    const count = value === "all"
      ? toolbarMatches.length
      : toolbarMatches.filter((item) => item.categories.includes(value)).length;
    return `
      <button
        type="button"
        class="category-tab ${state.category === value ? "active" : ""}"
        data-category="${value}"
        aria-pressed="${state.category === value}"
      >
        ${label}<span>${count}</span>
      </button>
    `;
  }).join("");
}

function renderMetrics() {
  const total = state.opportunities.length;
  const pursue = state.opportunities.filter((item) => item.status === "Pursue").length;
  const monitor = state.opportunities.filter((item) => item.status === "Monitor").length;
  const avgScore = total ? Math.round(state.opportunities.reduce((sum, item) => sum + item.fit_score, 0) / total) : 0;

  els.metrics.innerHTML = [
    ["Total opportunities", total],
    ["Pursue", pursue],
    ["Monitor", monitor],
    ["Average fit", avgScore],
  ].map(([label, value]) => `<article class="metric"><span>${label}</span><strong>${value}</strong></article>`).join("");
}

function renderTable() {
  const rows = filteredOpportunities();
  els.count.textContent = `${rows.length} shown`;
  els.rows.innerHTML = rows.length ? rows.map((opportunity) => `
    <tr data-id="${escapeHtml(opportunity.id)}" class="${opportunity.id === state.selectedId ? "selected" : ""}">
      <td>
        <div class="title-cell">
          <strong>${escapeHtml(opportunity.title)}</strong>
          <span>${escapeHtml(opportunity.agency)} | ${escapeHtml(opportunity.source)}</span>
        </div>
      </td>
      <td>
        <span class="badge category-badge">${escapeHtml(opportunity.category_label)}</span>
        ${opportunity.refresh_label && opportunity.refresh_label !== "Current"
          ? `<span class="badge refresh-badge ${refreshClass(opportunity.refresh_label)}">${escapeHtml(opportunity.refresh_label)}</span>`
          : ""}
      </td>
      <td>${escapeHtml(opportunity.state)}</td>
      <td>${formatDate(opportunity.due_date)}</td>
      <td><span class="badge ${scoreClass(opportunity.fit_score)}">${opportunity.fit_score}</span></td>
      <td><span class="badge ${statusClass(opportunity.status)}">${escapeHtml(opportunity.status)}</span></td>
    </tr>
  `).join("") : `
    <tr class="empty-row">
      <td colspan="6">
        No opportunities match this category and the current Search, Status, and Minimum fit filters.
      </td>
    </tr>
  `;
}

function renderList(title, values, emptyText) {
  if (!values.length) return `<p class="muted">${emptyText}</p>`;
  return `<div class="tag-list">${values.map((value) => `<span>${escapeHtml(value)}</span>`).join("")}</div>`;
}

async function selectOpportunity(id) {
  state.selectedId = id;
  renderTable();
  els.detail.innerHTML = `<p class="muted">Loading opportunity detail...</p>`;
  const opportunity = await api(`/api/opportunities/${encodeURIComponent(id)}`);
  renderDetail(opportunity);
}

function renderDetail(opportunity) {
  const history = opportunity.status_history || [];
  const breakdown = opportunity.fit_breakdown || [];
  const evidence = opportunity.analysis?.evidence || [];

  els.detail.innerHTML = `
    <div class="detail-header">
      <span class="badge ${scoreClass(opportunity.fit_score)}">${opportunity.fit_score} fit</span>
      ${opportunity.refresh_label ? `<span class="badge refresh-badge ${refreshClass(opportunity.refresh_label)}">${escapeHtml(opportunity.refresh_label)}</span>` : ""}
      <h2>${escapeHtml(opportunity.title)}</h2>
      <div class="detail-meta">
        <span>${escapeHtml(opportunity.document_type)}</span>
        <span>${escapeHtml(opportunity.state)}</span>
        <span>${escapeHtml(opportunity.agency)}</span>
      </div>
    </div>

    ${opportunity.reviewable ? `<div class="action-row">
      ${["Pursue", "Monitor", "Decline"].map((status) => `
        <button type="button" data-status="${status}" ${opportunity.status === status ? "disabled" : ""}>${status}</button>
      `).join("")}
    </div>` : `<p class="read-only-note">API-sourced market intelligence is read-only. Review the official source before acting.</p>`}

    <p class="summary">${escapeHtml(opportunity.summary)}</p>

    <div class="field-grid">
      <div class="field"><span>Due date</span>${formatDate(opportunity.due_date)}</div>
      <div class="field"><span>Budget</span>${money.format(opportunity.budget_estimate)}</div>
      <div class="field"><span>Eligibility</span>${escapeHtml(opportunity.eligibility)}</div>
      <div class="field"><span>Recommendation</span>${escapeHtml(opportunity.ai_recommendation)}</div>
      <div class="field"><span>Source category</span>${escapeHtml(opportunity.category_label)}</div>
      <div class="field"><span>API comparison</span>${escapeHtml(opportunity.refresh_label || "Not checked yet")}${opportunity.refresh_changed_fields?.length ? `<br><small>Changed: ${escapeHtml(opportunity.refresh_changed_fields.join(", "))}</small>` : ""}</div>
      <div class="field"><span>Official record</span>${opportunity.document_url ? `<a href="${escapeHtml(opportunity.document_url)}" target="_blank" rel="noopener">Open source</a>` : "Not available"}</div>
    </div>

    <section class="section-block">
      <h3>Eligibility explanation</h3>
      <p>${escapeHtml(opportunity.eligibility_reason)}</p>
    </section>

    <section class="section-block">
      <h3>Fit score breakdown</h3>
      <div class="stack-list">
        ${breakdown.map((item) => `
          <div class="stack-item">
            <strong>${escapeHtml(item.label)}: ${item.score}/${item.max}</strong>
          </div>
        `).join("")}
      </div>
    </section>

    <section class="section-block">
      <h3>Program focus</h3>
      ${renderList("Program focus", opportunity.program_focus, "No program focus terms captured.")}
    </section>

    <section class="section-block">
      <h3>Keyword matches</h3>
      ${renderList("Keywords", opportunity.keywords_matched, "No matching keywords captured.")}
    </section>

    <section class="section-block">
      <h3>Risks</h3>
      ${renderList("Risks", opportunity.risks, "No major risks captured.")}
    </section>

    <section class="section-block">
      <h3>Evidence</h3>
      <div class="stack-list">
        ${evidence.map((item) => `
          <div class="stack-item">
            <strong>${escapeHtml(item.claim)}</strong>
            <p>${escapeHtml(item.source_text)}</p>
          </div>
        `).join("")}
      </div>
    </section>

    <section class="section-block">
      <h3>Status history</h3>
      <div class="stack-list">
        ${history.length ? history.map((event) => `
          <div class="stack-item">
            <strong>${escapeHtml(event.from)} to ${escapeHtml(event.to)}</strong>
            <p>${formatDateTime(event.changed_at)} by ${escapeHtml(event.changed_by)}</p>
            <p>${escapeHtml(event.note)}</p>
          </div>
        `).join("") : `<p class="muted">No status changes yet.</p>`}
      </div>
    </section>
  `;
}

async function updateStatus(status) {
  if (!state.selectedId) return;
  const opportunity = await api(`/api/opportunities/${encodeURIComponent(state.selectedId)}/status`, {
    method: "POST",
    body: JSON.stringify({ status }),
  });
  state.opportunities = state.opportunities.map((item) => item.id === opportunity.id ? {
    ...item,
    status: opportunity.status,
    last_updated_at: opportunity.last_updated_at,
  } : item);
  renderMetrics();
  renderTable();
  renderDetail(opportunity);
}

async function renderSourcesAndRules() {
  const [sources, rules] = await Promise.all([api("/api/sources"), api("/api/scoring-rules")]);
  els.sourceCount.textContent = sources.length;
  els.ruleCount.textContent = rules.length;
  els.sources.innerHTML = sources.map((source) => `
    <div class="footer-item">
      <strong>${escapeHtml(source.name)}</strong>
      <span>${escapeHtml(source.state)} · ${escapeHtml(source.status)} · ${source.opportunities_found} found · ${formatDateTime(source.last_checked_at)}</span>
    </div>
  `).join("");

  els.rules.innerHTML = rules.map((rule) => `
    <div class="footer-item">
      <strong>${escapeHtml(rule.category)} · ${rule.weight}</strong>
      <span>${escapeHtml(rule.description)}</span>
    </div>
  `).join("");
}

async function init() {
  await startAutoRefresh();
  state.opportunities = await api("/api/opportunities");
  state.selectedId = state.opportunities[0]?.id || null;
  renderCategoryTabs();
  renderMetrics();
  renderTable();
  await renderSourcesAndRules();
  if (state.selectedId) await selectOpportunity(state.selectedId);
}

async function startAutoRefresh() {
  const status = await api("/api/refresh", {
    method: "POST",
    body: JSON.stringify({ force: true }),
  });
  if (status.running || status.started) pollRefresh();
}

function pollRefresh() {
  const timer = setInterval(async () => {
    try {
      const status = await api("/api/refresh/status");
      if (!status.running) {
        clearInterval(timer);
        state.opportunities = await api("/api/opportunities");
        renderCategoryTabs();
        renderMetrics();
        renderTable();
        await renderSourcesAndRules();
        if (state.selectedId) await selectOpportunity(state.selectedId);
      }
    } catch (error) {
      clearInterval(timer);
      console.warn("Automatic data refresh status check failed:", error);
    }
  }, 3000);
}

els.rows.addEventListener("click", (event) => {
  const row = event.target.closest("tr[data-id]");
  if (row) selectOpportunity(row.dataset.id).catch((error) => {
    els.detail.innerHTML = `<p class="muted">${escapeHtml(error.message)}</p>`;
  });
});

els.detail.addEventListener("click", (event) => {
  const button = event.target.closest("button[data-status]");
  if (button) updateStatus(button.dataset.status).catch((error) => alert(error.message));
});

els.tabs.addEventListener("click", (event) => {
  const tab = event.target.closest("button[data-category]");
  if (!tab) return;
  state.category = tab.dataset.category;
  renderCategoryTabs();
  renderTable();
  document.querySelector(".table-wrap").scrollTop = 0;
});

function renderFilteredView() {
  renderCategoryTabs();
  renderTable();
}

els.search.addEventListener("input", renderFilteredView);
els.score.addEventListener("input", renderFilteredView);
els.status.addEventListener("change", renderFilteredView);

init().catch((error) => {
  document.body.innerHTML = `<main><section class="panel"><h1>Unable to load dashboard</h1><p>${escapeHtml(error.message)}</p></section></main>`;
});
