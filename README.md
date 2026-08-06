# Growth Intelligence Dashboard

A proof-of-concept dashboard for reviewing public-sector growth signals across three record families:

- **New opportunities** — RFPs, RFAs, NOFOs, grants, and other procurements that may be actionable.
- **Contracts and recompetes** — existing contracts, incumbents, values, and expiration timelines that support early recompete planning.
- **Updates** — Medicaid, Medicare, CMS, policy, funding, public-comment, and Rural Health Transformation activity.

The dashboard also provides an RHT Tracker, Competitor Intelligence, and a federal source-evidence explorer.

## What it does

- Collects records from official federal, state, and territory sources.
- Reuses shared collectors when states operate on common platforms such as BSO/BuySpeed.
- Normalizes different source formats into consistent CSV snapshots.
- Combines and deduplicates state and federal records through `CsvStore`.
- Applies a deterministic, explainable Priority score tailored to each record family.
- Preserves official links for human verification.
- Stores opportunity pins and review statuses locally.

The application is discovery-assistive. Scores, RHT classifications, and recompete timing are review signals rather than authoritative procurement determinations.

## Run locally

The project uses the Python standard library and plain HTML/CSS/JavaScript. It does not require a database, Node.js, or external Python packages.

```bash
python app.py
```

Open:

```text
http://localhost:8000
```

The dashboard reads known-good local CSV snapshots. Browser filters do not call government websites.

## Data flow

```text
Operator runs a collection script
  -> federal service or registered state adapter
  -> official public source
  -> relevance filtering and normalization
  -> CSV upsert
  -> CsvStore combines and deduplicates records
  -> Model B calculates Priority
  -> local API
  -> dashboard and official-source review
```

## High-level collection commands

Use `--dry-run` first to call selected sources without changing CSV files. State callers have no default jurisdictions: pass specific tags with `--states IL,MA,NJ`, or run every configured adapter with `--states all` (`--all` is an equivalent shorthand). Active tags and adapter modules come from `data/state_collectors.json`; unknown tags are rejected before collection starts.

### Federal sources

```bash
python scripts/run_gov_search.py \
  --mode continue \
  --sources grants,federal_register,medicaid,cms_provider,usaspending \
  --max-per-source 25 \
  --dry-run
```

### State opportunities

```bash
python scripts/state_opportunities.py \
  --states IL,MA,NJ \
  --max-records 25 \
  --dry-run
```

### State contracts

```bash
python scripts/state_contracts.py \
  --states IL,MA,NJ \
  --vendor-group "Gainwell Technologies" \
  --max-per-vendor 25 \
  --dry-run
```

### State updates

```bash
python scripts/state_updates.py \
  --states IL,MI,NJ \
  --max-records 50 \
  --dry-run
```

To dry-run every configured collector in one family:

```bash
python scripts/state_updates.py --states all --max-records 50 --dry-run
```

Remove `--dry-run` to upsert normalized results into the corresponding CSV snapshot. After refreshing federal source files, rebuild the derived datasets:

```bash
python scripts/build_contract_lifecycle.py
python scripts/build_federal_update_catalog.py
```

See [`docs/runbook.md`](docs/runbook.md) for supported state tags, individual federal callers, script flags, output files, and the complete refresh sequence.

## Canonical data families

```text
Opportunities
  data/state_opportunities.csv
  data/federal_opportunities.csv
  data/federal_grants.csv

Contracts and recompetes
  data/state_contracts.csv
  data/federal_contract_lifecycle.csv

Updates
  data/state_policy_updates.csv
  data/federal_updates_catalog.csv
```

Scoring and matching configuration is checked in under `data/`, including:

```text
data/capability_rules.csv
data/strategic_jurisdictions.csv
data/competitor_aliases.csv
data/search_parameters.json
data/state_collectors.json
```

## Project structure

```text
SOE_Group3/
├── app.py                     Local HTTP server and API routes
├── data/                      CSV snapshots and checked-in configuration
├── scripts/                   Operator-facing collection and build commands
├── services/
│   ├── csv_store.py           Family assembly, normalization, and deduplication
│   ├── priority_scoring.py    Explainable Model B scoring
│   ├── search_taxonomy.py     Shared business terminology
│   ├── gov_api_client.py      Federal source orchestration
│   ├── state_opportunities/   Opportunity adapters by jurisdiction
│   ├── state_contracts/       Contract adapters by jurisdiction
│   └── state_updates/         Update adapters by jurisdiction
├── static/                    Dashboard and Federal Explorer frontend
├── tests/                     Unit, route, collector, and data-integrity tests
└── docs/                      Runbook, coverage, and presentation documentation
```

## Main dashboard APIs

```text
GET  /api/opportunities
GET  /api/contracts
GET  /api/updates
GET  /api/rht-overview
GET  /api/competitors
GET  /api/federal-records
POST /api/opportunities/<id>/status
POST /api/opportunities/<id>/pin
```

Opportunity review actions are local. They do not modify any government source.

## Validate changes

```bash
python -m unittest discover -s tests -q
python -m compileall -q app.py services scripts
node --check static/app.js
node --check static/federal-records.js
```

## Documentation

- [Collection runbook](docs/runbook.md)
- [Collector coverage](docs/collector_coverage_status.md)
