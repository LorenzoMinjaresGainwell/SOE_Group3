# Presentation handoff: backend data story

> **Audience:** Gainwell presentation team  
> **Use:** slide-ready talking points; nontechnical overview

## Slide 1 — What the solution does

**Official public sources → collectors → normalized CSV families → backend → filters/dashboard**

- Collectors check public procurement, award, policy, Medicaid, CMS, and rural-health sources.
- They translate different source formats into consistent, presentation-friendly records.
- The backend combines selected records and supplies them to dashboard views.
- The current dashboard can search and filter by category, status, and minimum fit score.

**Key message:** this reduces scanning effort and helps teams find items worth reviewing. It does not replace source verification or human judgment.

---

## Slide 2 — The three data families

### 1. Opportunities — “What can we pursue?”
Meaningful fields: title, state, agency, notice type, posted/due dates, estimated amount, matched themes, relevance/fit, summary, and official links.

### 2. Contracts and recompete — “What may be coming back to market?”
Meaningful fields: incumbent/vendor, agency, contract purpose, value, start/end dates, lifecycle status, recompete signal or watch-window start, health-program themes, and official links.

### 3. Updates — “What may shape future demand?”
Meaningful fields: update type, title, agency, program/topic, publication and action dates, public-comment requirement, RHT flag, importance, summary, and official links.

Internal IDs support matching and deduplication, but they are not presentation content.

---

## Slide 3 — Real examples from the current CSV snapshot

These examples are sanitized: no internal/raw payloads or contact details.

### RHT opportunity
- **Tennessee:** *Rural Health Transformation Program (RHTP) Marketing and Advertising Services*
- **Agency/source:** Tennessee Central Procurement Office public RFP page
- **Recorded dates:** posted July 22, 2026; due August 26, 2026
- **Why it matters:** an explicit RHT procurement, not merely a broad rural-health keyword match.

### Vendor contract and recompete signal
- **Company named by the official record:** Gainwell Technologies LLC
- **Contract:** New Jersey Medicaid Management Information System (MMIS)
- **Agency/source:** New Jersey Division of Purchase and Property / NJSTART Active Contracts
- **Recorded term/value:** May 1, 2015–April 30, 2027; $702,398,107
- **Signal:** **Expiring soon**
- **Presentation wording:** “A public contract record identifies Gainwell as the vendor and provides an end date that creates a recompete-monitoring signal.” Do not say a new solicitation is confirmed.

### Explicit recompete window example
- **Company named by the official record:** Maximus Federal Services, Inc.
- **Contract:** Part C Qualified Independent Contractor award
- **Agency:** U.S. Department of Health and Human Services
- **Recorded lifecycle:** near expiry; potential end July 31, 2026
- **Calculated watch-window start:** July 31, 2023
- **Why it matters:** the date supports early monitoring; the window is an analytical planning aid, not a procurement announcement.

### RHT policy/funding signal
- **South Dakota:** *SPA 26-0006: Primary Accountable Care Transformation (PACT) Quality Payment*
- **Agency/source:** South Dakota Department of Social Services public State Plan Amendment page
- **Recorded dates:** posted July 20, 2026; public-comment due August 19, 2026
- **Why it matters:** the update says the quality payment is funded under the Rural Health Transformation plan.

---

## Slide 4 — Why RHT is prominent

- Explicit **Rural Health Transformation / RHT / RHTP** language is elevated.
- Related signals include rural hospitals, critical access hospitals, telehealth, workforce/provider shortages, grants, quality, and Medicaid transformation.
- RHT can surface across all three families: a live procurement, an incumbent contract, or an earlier policy/funding signal.
- The RHT flag is keyword/rule-based. A reviewer must read the summary and official document to confirm context.

---

## Slide 5 — How to describe coverage

Use these terms consistently:

- **Verified records:** an official public source was successfully checked and produced qualifying normalized records.
- **Verified zero:** the official source was successfully checked, but no qualifying records were found for that search and time window. This does **not** mean the state has no activity.
- **Blocked:** the source could not be verified because of access controls, login requirements, CAPTCHA/bot protection, network failure, or the absence of a stable public feed. Blocked is **unknown**, not zero.

**Registry rule:** a collector is integrated only when it appears in the active family registry—not merely because a collector file exists. In the current code snapshot, the registries contain **48 opportunity**, **34 contract**, and **47 update** state/territory adapters; blocked collectors are kept out of active registries, and family coverage differs. The legacy `sources.csv` list is not a complete inventory of those state adapters.

---

## Slide 6 — Backend reality for the demo

- The current opportunity API combines the dashboard CSVs for opportunities and contracts, normalizes their labels, and returns sortable records.
- Contract records are presented as competitor/recompete intelligence; only **Expiring soon** and **Recompete watch** qualify as expiration categories.
- The dashboard’s visible search, category, status, and minimum-score filters are applied in the browser after the backend returns records.
- Status and pin changes are local review aids, not updates to any government system.
- Richer family files—including federal opportunity, federal contract-lifecycle, state update, and federal update catalogs—exist, but are **not yet all connected to the current backend/dashboard APIs**.
- **Frontend integration is deferred**; this handoff does not claim a completed three-family user experience.

---

## Slide 7 — Guardrails and known demo limitations

- **Assistive, not authoritative:** scores, categories, recommendations, RHT flags, and recompete windows prioritize review; they do not establish eligibility, contract certainty, or bid/no-bid decisions.
- Always confirm dates, values, amendments, eligibility, and status in the linked official document before acting.
- A blank or `0` amount can mean “not published,” not a zero-dollar opportunity or contract.
- Recompete windows are calculated planning signals; extensions, options, bridge actions, and agency plans may change timing.
- Public portals can change format, block automated access, or publish incomplete fields.
- CSVs are snapshots. The current examples contain July/August 2026 dates; refresh or clearly label the snapshot date before presenting.
- Keyword matching can produce broad RHT or Medicaid associations, and source terminology is not uniform.
- The current backend loads local CSVs in memory; it is a focused demo implementation, not yet an enterprise data platform.

### Recommended closing line

> “The solution creates an explainable early-warning view from official public information, so Gainwell teams can focus human review on the opportunities, recompetes, and RHT signals most likely to matter.”
