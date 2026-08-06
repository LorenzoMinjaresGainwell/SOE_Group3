# Data Collection Runbook

Run all commands from the repo root

Optional keys are read from `.env`:

```bash
SAM_API_KEY=
REGULATIONS_API_KEY=
```

Use `--dry-run` to test without writing CSVs. Use `--json` for machine-readable summaries.

## Validation

```bash
python3 -m py_compile services/*.py services/state_contracts/*.py services/state_opportunities/*.py scripts/*.py
python3 -m json.tool data/search_parameters.json >/dev/null
```

## Federal Callers

### USAspending Contracts

Writes federal awarded contract history to:

```text
data/contracts.csv
```

Run configured vendors:

```bash
./scripts/usaspending_contracts.py --max-per-vendor 50
```

Run specific vendors:

```bash
./scripts/usaspending_contracts.py --vendors "MAXIMUS,Gainwell Technologies" --max-per-vendor 50
```

Run configured vendor group and aliases:

```bash
./scripts/usaspending_contracts.py --vendor-group "Gainwell Technologies" --max-per-vendor 50
```

Key flags:

```text
--vendors CSV             direct vendor names
--vendor-group CSV        configured vendor names using aliases
--years N                 lookback years, capped at 2007-10-01 by USAspending search
--start-date YYYY-MM-DD   explicit start date
--end-date YYYY-MM-DD     explicit end date
--max-per-vendor N        max records per vendor
--only-keyword-matches    keep only keyword-matching awards
--out PATH                output CSV
--dry-run                 no CSV write
--json                    JSON summary
```

### Federal Register Updates

Writes CMS/HHS rule, notice, RFI, and policy updates to:

```text
data/federal_register_updates.csv
```

Run recent updates:

```bash
./scripts/federal_register_updates.py --days-back 365 --max-records 100
```

Run a date range:

```bash
./scripts/federal_register_updates.py --start-date 2020-01-01 --end-date 2026-12-31 --max-records 500
```

Key flags:

```text
--keywords CSV            search keywords
--days-back N             lookback window
--start-date YYYY-MM-DD   explicit start date
--end-date YYYY-MM-DD     explicit end date
--max-records N           max records written
--out PATH                output CSV
--api-notes               print related API notes
--dry-run                 no CSV write
--json                    JSON summary
```

### Regulations.gov Updates

Requires one of:

```text
REGULATIONS_API_KEY
REGULATIONS_GOV_API_KEY
```

Writes docket/document enrichment to:

```text
data/regulations_updates.csv
```

Run from Federal Register docket IDs:

```bash
./scripts/regulations_updates.py --from-federal-register data/federal_register_updates.csv --max-records 100
```

Rerun later after rate limiting:

```bash
python -u scripts/regulations_updates.py --max-records 50
```

Run direct dockets:

```bash
./scripts/regulations_updates.py --dockets "CMS-2026-1255,CMS-2026-0062" --max-records 50
```

Key flags:

```text
--from-federal-register PATH   read docket IDs from Federal Register CSV
--dockets CSV                  direct docket IDs
--max-records N                max records written
--out PATH                     output CSV
--env-file PATH                env file, default .env
--dry-run                      no CSV write
--json                         JSON summary
```

Notes:

```text
Default input is data/federal_register_updates.csv when --dockets is omitted.
The client does not bulk-download comments.
Comment counts come from metadata when available.
Attachment counts are sampled to keep calls low.
If Regulations.gov returns 429, the run stops early and preserves existing CSV data.
```

### CMS Provider Data Catalog

Writes CMS Provider Data catalog metadata to:

```text
data/cms_provider_data.csv
```

Run full metadata refresh:

```bash
./scripts/cms_api_caller.py --out data/cms_provider_data.csv --history-years 20
```

Run since a date:

```bash
./scripts/cms_api_caller.py --since 2026-07-01 --date-field released_date
```

Key flags:

```text
--out PATH                                  output CSV
--since YYYY-MM-DD                          filter rows by date field
--date-field issued_date|released_date|modified_date
--history-years N                           reporting window for cadence stats
```

### Combined Federal/Gov Search

Runs multiple federal-style sources through one caller.

Common no-key run:

```bash
./scripts/run_gov_search.py --mode continue --sources grants,federal_register,medicaid,cms_provider,usaspending --max-per-source 25
```

SAM test after quota reset:

```bash
./scripts/run_gov_search.py --mode continue --sources sam --max-per-source 10 --days-back 30 --dry-run
```

SAM with procurement types:

```bash
./scripts/run_gov_search.py --mode continue --sources sam --sam-ptypes o,k,r,p --max-per-source 25
```

Source keys:

```text
sam
grants
federal_register
medicaid
cms_provider
usaspending
```

SAM procurement types:

```text
o = Solicitation
k = Combined Synopsis/Solicitation
r = Sources Sought
p = Pre-solicitation
a = Award Notice
s = Special Notice
u = Justification
i = Intent to Bundle
g = Sale of Surplus Property
```

Key flags:

```text
--mode continue|historic      run mode
--sources CSV                 source keys
--keywords CSV                business keywords
--max-per-source N            max records per source
--days-back N                 first-run lookback
--overlap-days N              continue-mode overlap
--start-date YYYY-MM-DD       historic start date
--end-date YYYY-MM-DD         historic end date
--vendors CSV                 USAspending vendor names
--vendors-file PATH           optional vendor file
--sam-ptypes CSV              SAM procurement type filters
--env-file PATH               env file, default .env
--dry-run                     no CSV write
--json                        JSON summary
```

## State Callers

State and territory adapters are selected explicitly with comma-separated postal abbreviations passed to `--states`. The special tag `--states all` runs every active adapter in that family; `--all` is an equivalent shorthand. Do not combine `all` with other tags. There are no default states. Active tags and module mappings are loaded at runtime from `data/state_collectors.json`; unknown or unregistered tags are rejected before collection. Use `--dry-run` before writing snapshots. A registered adapter can return zero qualifying records, and official portals can still be temporarily unavailable.

### State Opportunities

Output:

```text
data/state_opportunities.csv
```

Dry-run selected jurisdictions:

```bash
python scripts/state_opportunities.py \
  --states IL,MA,NJ,OR,AR,NV,VI \
  --keywords "Medicaid,MMIS,eligibility,claims,rural health" \
  --days-back 90 \
  --max-records 25 \
  --dry-run
```

Active tags (48):

```text
AK,AL,AR,AZ,CA,CO,DC,DE,FL,GA,HI,IA,ID,IL,IN,KS,KY,LA,
MA,MD,ME,MI,MO,MS,MT,NC,NE,NJ,NM,NV,NY,OK,OR,PA,PR,RI,
SC,SD,TN,TX,UT,VA,VI,VT,WA,WI,WV,WY
```

Flags:

```text
--states CSV              configured abbreviations or the special tag all (no default)
--all                     shorthand for --states all; runs all 48 opportunity collectors
--keywords CSV            defaults to the shared business taxonomy
--days-back N             recent record window; future-due records are retained
--max-records N           maximum normalized results
--params PATH             default data/search_parameters.json
--out PATH                default data/state_opportunities.csv
--dry-run                 call and normalize without writing
--json                    print a machine-readable summary
```

A lightweight static-HTML check:

```bash
python scripts/state_opportunities.py --states NE --max-records 10 --dry-run
```

A shared BSO/BuySpeed adapter check:

```bash
python scripts/state_opportunities.py --states IL --max-records 10 --dry-run
```

### State Contracts and Potential Recompetes

Output:

```text
data/state_contracts.csv
```

Contract callers require direct vendor terms or configured vendor groups:

```bash
python scripts/state_contracts.py \
  --states AR,IL,MA,NJ,OR \
  --vendor-group "Gainwell Technologies" \
  --max-per-vendor 25 \
  --dry-run
```

Direct vendor terms:

```bash
python scripts/state_contracts.py \
  --states PA,TX \
  --vendors "Gainwell,DXC,HMS,Health Management Systems,MAXIMUS" \
  --max-per-vendor 25 \
  --dry-run
```

Active tags (34):

```text
AK,AL,AR,AZ,CA,CO,DC,DE,FL,GA,IA,ID,IL,IN,LA,MA,MD,MI,
MO,NC,NJ,NY,OK,OR,PA,PR,TN,TX,UT,VA,VT,WA,WV,WY
```

Flags:

```text
--states CSV              configured abbreviations or the special tag all (no default)
--all                     shorthand for --states all; runs all 34 contract collectors
--vendors CSV             direct vendor search terms
--vendor-group CSV        configured vendor groups with aliases
--keywords CSV            defaults to the shared business taxonomy
--max-per-vendor N        maximum records per vendor term
--params PATH             default data/search_parameters.json
--out PATH                default data/state_contracts.csv
--dry-run                 call and normalize without writing
--json                    print a machine-readable summary
```

Contract end dates produce planning signals rather than confirmed future solicitations. Unknown and far-future placeholder dates are treated neutrally.

### State Medicaid, Medicare, CMS, and RHT Updates

Output:

```text
data/state_policy_updates.csv
```

```bash
python scripts/state_updates.py \
  --states CA,FL,NJ,PA,PR,TX,VI \
  --max-records 50 \
  --dry-run
```

Active tags (47):

```text
AK,AL,AR,AZ,CA,CO,CT,DC,FL,GA,HI,IA,ID,IL,IN,KY,LA,MD,
ME,MI,MO,MP,MS,MT,NC,ND,NE,NJ,NM,NV,NY,OK,OR,PA,PR,RI,
SC,SD,TN,TX,UT,VA,VI,VT,WA,WV,WY
```

Flags:

```text
--states CSV              configured abbreviations or the special tag all (no default)
--all                     shorthand for --states all; runs all 47 update collectors
--keywords CSV            defaults to the shared business taxonomy
--max-records N           maximum normalized results across selected sources
--params PATH             default data/search_parameters.json
--out PATH                default data/state_policy_updates.csv
--dry-run                 call and normalize without writing
--json                    print a machine-readable summary
```

## Derived Federal Datasets

After refreshing federal source CSVs, rebuild the files consumed by the canonical contract and update families:

```bash
python scripts/build_contract_lifecycle.py
python scripts/build_federal_update_catalog.py
```

The lifecycle builder reads `data/contracts.csv` and writes `data/federal_contract_lifecycle.csv`. The update builder combines Federal Register, Regulations.gov, grants, opportunities, CMS, and Medicaid source snapshots into `data/federal_updates_catalog.csv` while preserving family separation.

## Recommended Refresh Sequence

```bash
# 1. Dry-run the selected federal and state callers.
python scripts/run_gov_search.py --mode continue --sources grants,federal_register,medicaid,cms_provider,usaspending --max-per-source 25 --dry-run
python scripts/state_opportunities.py --states all --max-records 100 --dry-run
python scripts/state_contracts.py --states all --vendor-group "Gainwell Technologies" --max-per-vendor 25 --dry-run
python scripts/state_updates.py --states all --max-records 300 --dry-run

# 2. Repeat approved commands without --dry-run to update snapshots.

# 3. Rebuild derived federal datasets.
python scripts/build_contract_lifecycle.py
python scripts/build_federal_update_catalog.py

# 4. Validate before starting the dashboard.
python -m unittest discover -s tests -q
python -m compileall -q app.py services scripts
node --check static/app.js
node --check static/federal-records.js
```

Use `--states` instead of `--all` to run a comma-separated subset from the relevant family list. Running every active source can take time and remains dependent on external portal availability.

## Configuration

Runtime state collector registry and main search configuration:

```text
data/state_collectors.json
data/search_parameters.json
```

`data/state_collectors.json` is the runtime source of active state/territory tags and maps each tag to its module under `services/state_<family>/`.

Shared and family-specific configuration:

```text
services/search_taxonomy.py
data/competitor_aliases.csv
data/capability_rules.csv
data/strategic_jurisdictions.csv
```

Template secrets file:

```text
.env.example
```

Local secrets file:

```text
.env
```

Do not commit `.env`.

## Generated and Canonical CSVs

```text
data/contracts.csv
data/federal_contract_lifecycle.csv
data/federal_opportunities.csv
data/federal_grants.csv
data/federal_register_updates.csv
data/regulations_updates.csv
data/cms_provider_data.csv
data/medicaid_data.csv
data/federal_updates_catalog.csv
data/state_contracts.csv
data/state_opportunities.csv
data/state_policy_updates.csv
```
