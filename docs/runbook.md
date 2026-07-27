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

State outputs are organized by data type:

```text
data/state_contracts.csv        awarded contracts and incumbent/recompete signals
data/state_opportunities.csv    RFPs, bids, awards, and funding opportunities
```

Supported state keys currently include:

```text
PA
TX
```

### State Contracts

Run configured Gainwell-style terms:

```bash
./scripts/state_contracts.py --states PA,TX --vendor-group "Gainwell Technologies" --max-per-vendor 100
```

Run direct vendor terms:

```bash
./scripts/state_contracts.py --states PA,TX --vendors "Gainwell,DXC,HMS,Health Management Systems,MAXIMUS" --max-per-vendor 100
```

Run one state:

```bash
./scripts/state_contracts.py --states PA --vendor-group "Gainwell Technologies" --max-per-vendor 100
```

Key flags:

```text
--states CSV              state abbreviations
--vendors CSV             direct vendor search terms
--vendor-group CSV        configured vendor groups and aliases
--keywords CSV            scoring/matching keywords
--max-per-vendor N        max records per vendor term
--params PATH             config file
--out PATH                output CSV
--dry-run                 no CSV write
--json                    JSON summary
```

Notes:

```text
PA uses OpenBookPA contracts.
TX uses TXSmartBuy contracts.
Far-future PA dates like 2099-12-31 are treated as placeholders.
PA rows distinguish parent contracts from amendments.
```

### State Opportunities

Run PA/TX opportunity sources:

```bash
./scripts/state_opportunities.py --states PA,TX --max-records 100
```

Run with focused keywords:

```bash
./scripts/state_opportunities.py --states PA,TX --keywords "Medicaid,MMIS,eligibility,claims,rural health" --max-records 100
```

Run one state:

```bash
./scripts/state_opportunities.py --states PA --max-records 100
```

Key flags:

```text
--states CSV              state abbreviations
--keywords CSV            opportunity keywords
--days-back N             recent award window
--max-records N           max records written
--params PATH             config file
--out PATH                output CSV
--dry-run                 no CSV write
--json                    JSON summary
```

Notes:

```text
PA uses eMarketplace and DHS Rural Health Transformation pages.
TX uses ESBD.
Strict filtering can return zero rows when only unrelated matches are found.
```

## Config

Main config:

```text
data/search_parameters.json
```

Common sections:

```text
monitored_keywords
vendors
usaspending
state_contracts
state_sources
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

## Generated CSVs

```text
data/contracts.csv
data/state_contracts.csv
data/state_opportunities.csv
data/federal_register_updates.csv
data/regulations_updates.csv
data/cms_provider_data.csv
```
