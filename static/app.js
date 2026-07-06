const state = {
  opportunities: [],
  selectedId: null,
};

const money = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 0 });

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
  refresh: document.querySelector("#refreshButton"),
};

async function api(path, options = {}) {
  const response = await fetch(path, {
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

function filteredOpportunities() {
  const query = els.search.value.trim().toLowerCase();
  const status = els.status.value;
  const minScore = Number(els.score.value || 0);

  return state.opportunities.filter((opportunity) => {
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
  });
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
  els.rows.innerHTML = rows.map((opportunity) => `
    <tr data-id="${escapeHtml(opportunity.id)}" class="${opportunity.id === state.selectedId ? "selected" : ""}">
      <td>
        <div class="title-cell">
          <strong>${escapeHtml(opportunity.title)}</strong>
          <span>${escapeHtml(opportunity.agency)} | ${escapeHtml(opportunity.document_type)}</span>
        </div>
      </td>
      <td>${escapeHtml(opportunity.state)}</td>
      <td>${formatDate(opportunity.due_date)}</td>
      <td><span class="badge ${scoreClass(opportunity.fit_score)}">${opportunity.fit_score}</span></td>
      <td><span class="badge ${statusClass(opportunity.status)}">${escapeHtml(opportunity.status)}</span></td>
    </tr>
  `).join("");
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
      <h2>${escapeHtml(opportunity.title)}</h2>
      <div class="detail-meta">
        <span>${escapeHtml(opportunity.document_type)}</span>
        <span>${escapeHtml(opportunity.state)}</span>
        <span>${escapeHtml(opportunity.agency)}</span>
      </div>
    </div>

    <div class="action-row">
      ${["Pursue", "Monitor", "Decline"].map((status) => `
        <button type="button" data-status="${status}" ${opportunity.status === status ? "disabled" : ""}>${status}</button>
      `).join("")}
    </div>

    <p class="summary">${escapeHtml(opportunity.summary)}</p>

    <div class="field-grid">
      <div class="field"><span>Due date</span>${formatDate(opportunity.due_date)}</div>
      <div class="field"><span>Budget</span>${money.format(opportunity.budget_estimate)}</div>
      <div class="field"><span>Eligibility</span>${escapeHtml(opportunity.eligibility)}</div>
      <div class="field"><span>Recommendation</span>${escapeHtml(opportunity.ai_recommendation)}</div>
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
  els.sources.innerHTML = sources.map((source) => `
    <div class="stack-item">
      <strong>${escapeHtml(source.name)}</strong>
      <p>${escapeHtml(source.state)} | ${escapeHtml(source.type)} | ${escapeHtml(source.status)}</p>
      <p>${source.opportunities_found} found, checked ${formatDateTime(source.last_checked_at)}</p>
    </div>
  `).join("");

  els.rules.innerHTML = rules.map((rule) => `
    <div class="stack-item">
      <strong>${escapeHtml(rule.category)} (${rule.weight})</strong>
      <p>${escapeHtml(rule.description)}</p>
    </div>
  `).join("");
}

async function refreshSources() {
  els.refresh.disabled = true;
  els.refresh.textContent = "Checking...";
  try {
    const result = await api("/api/refresh", { method: "POST", body: "{}" });
    alert(result.message);
  } finally {
    els.refresh.disabled = false;
    els.refresh.textContent = "Refresh sources";
  }
}

async function init() {
  state.opportunities = await api("/api/opportunities");
  state.selectedId = state.opportunities[0]?.id || null;
  renderMetrics();
  renderTable();
  await renderSourcesAndRules();
  if (state.selectedId) await selectOpportunity(state.selectedId);
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

[els.search, els.status, els.score].forEach((input) => input.addEventListener("input", renderTable));
els.refresh.addEventListener("click", refreshSources);

init().catch((error) => {
  document.body.innerHTML = `<main><section class="panel"><h1>Unable to load dashboard</h1><p>${escapeHtml(error.message)}</p></section></main>`;
});
