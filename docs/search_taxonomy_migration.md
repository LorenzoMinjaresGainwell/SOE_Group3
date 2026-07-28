# Step 8 search taxonomy migration

## Centralized

- `services/search_taxonomy.py` validates `data/search_parameters.json`, capability rules, and competitor aliases.
- Canonical groups: explicit/direct/related RHT, health programs (Medicaid/Medicare/CMS), Gainwell capabilities, competitor aliases, and negative/noise terms.
- Matching uses token boundaries; dedupe is case-insensitive and first-seen-order stable.
- Legacy `monitored_keywords` and `vendors` remain supported and take precedence for monitored defaults.

## Migrated callers

- API/orchestration: `services/usaspending_client.py`, `services/gov_api_client.py`, `services/auto_refresh.py`, `services/sam_contract_awards_client.py`.
- Federal/lifecycle classification: `services/federal_register_client.py`, `services/federal_update_catalog.py`, `services/contract_lifecycle.py`.
- CLIs: `run_gov_search.py`, `usaspending_contracts.py` (through client config), `federal_register_updates.py`, `sam_contract_awards.py` (through client config), `sam_call_plan.py`, `state_opportunities.py`, `state_contracts.py`, `state_updates.py`.
- State adapters still receive `keywords`; no portal selector, request field, endpoint vocabulary, or parser context list was bulk-edited.

## Intentionally retained

- SAM ptypes, agency filters, approved-live-call vocabulary, and notice buckets.
- Grants.gov agency codes/status/request vocabulary and Federal Register agency slugs/fields.
- State portal selectors, ambiguous-term guards, procurement exclusions, parser labels, and feed-specific context terms.
- Scoring weights and topic-to-output mappings where terms carry model semantics rather than define monitored defaults. The audit marks genuinely shared copies as later migration candidates.

## Audit and verification

- `python scripts/audit_business_terms.py` reports remaining constants as `justified_source_specific` or `migration_candidate`; `--json` and optional `--fail-on-candidates` are available.
- Current audit: 128 justified source-specific findings and 10 migration candidates.
- Full offline suite: 127 tests passed.
- `python -m compileall -q services scripts tests` and `git diff --check` passed.
- No network calls, frontend edits, or generated CSV edits were made for this step.
