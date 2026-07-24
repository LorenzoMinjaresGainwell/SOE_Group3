#!/usr/bin/env python3
"""Fetch SAM.gov Contract Awards into data/sam_contract_awards.csv.

Run from repo root:
    ./scripts/sam_contract_awards.py --dry-run --max-searches 4
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.sam_contract_awards_client import (  # noqa: E402
    RateLimitError,
    SAM_CONTRACT_AWARD_FIELDS,
    SAMContractAwardsConfig,
    fetch_sam_contract_awards,
    load_search_parameters,
    upsert_sam_contract_awards,
    write_csv,
)


def parse_date(value: str) -> dt.date | None:
    if not value:
        return None
    return dt.date.fromisoformat(value)


def split_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def repo_path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


def default_years(params: dict[str, Any]) -> int:
    try:
        return min(20, max(1, int((params.get("usaspending") or {}).get("years_back") or 8)))
    except (TypeError, ValueError):
        return 8


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch SAM.gov Contract Awards for GWT and competitors.")
    parser.add_argument("--params", default="data/search_parameters.json")
    parser.add_argument("--vendor-entities", default="data/vendor_entities.csv")
    parser.add_argument("--out", default="data/sam_contract_awards.csv")
    parser.add_argument("--mode", choices=["continue", "historic"], default="historic")
    parser.add_argument("--vendors", default="", help="comma list; overrides configured vendors and aliases")
    parser.add_argument("--vendor-group", default="", help="comma list of configured vendor names/keys to include with aliases")
    parser.add_argument("--keywords", default="", help="comma list; defaults to monitored_keywords in params")
    parser.add_argument("--agency-codes", default="7500", help="comma list of SAM contractingDepartmentCode filters; 7500=HHS")
    parser.add_argument("--years", type=int, default=0, help="historic lookback; defaults to params usaspending.years_back capped at 20")
    parser.add_argument("--days-back", type=int, default=365, help="continue-mode lastModifiedDate lookback")
    parser.add_argument("--start-date", default="", help="YYYY-MM-DD")
    parser.add_argument("--end-date", default="", help="YYYY-MM-DD")
    parser.add_argument("--max-searches", type=int, default=12, help="cap search specs; vendor aliases are ordered first")
    parser.add_argument("--max-per-search", type=int, default=25)
    parser.add_argument("--page-limit", type=int, default=25, help="SAM API limit per page; max 100")
    parser.add_argument("--max-pages", type=int, default=1)
    parser.add_argument("--include-vendor-searches", action="store_true", help="enable vendor alias/UEI/CAGE search specs")
    parser.add_argument("--include-keyword-searches", action="store_true", help="enable keyword search specs after broad agency specs")
    parser.add_argument("--skip-keyword-searches", action="store_true", help="legacy no-op unless --include-keyword-searches is used")
    parser.add_argument("--include-deleted", action="store_true", help="also query SAM deletedStatus=yes rows")
    parser.add_argument("--sam-quota-mode", choices=["cache-only", "live"], default="cache-only")
    parser.add_argument("--sam-live-budget", type=int, default=0, help="live SAM calls allowed today; default 0")
    parser.add_argument("--sam-cache-dir", default="data/raw/sam", help="SAM raw cache directory")
    parser.add_argument("--sam-ledger-path", default="", help="SAM call ledger path; defaults under cache dir")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--init-empty", action="store_true", help="write only the CSV header without calling SAM")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    params_path = ROOT / args.params
    params = load_search_parameters(params_path)

    out_path = ROOT / args.out
    if args.init_empty:
        write_csv(out_path, SAM_CONTRACT_AWARD_FIELDS, [])
        print(f"Initialized {args.out} with SAM Contract Awards header.")
        return 0

    sam_cache_dir = repo_path(args.sam_cache_dir)
    sam_ledger_path = repo_path(args.sam_ledger_path) if args.sam_ledger_path else sam_cache_dir / "call_ledger.ndjson"

    config = SAMContractAwardsConfig(
        params_path=params_path,
        vendor_entities_path=ROOT / args.vendor_entities,
        mode=args.mode,
        keywords=split_csv(args.keywords) or None,
        vendors_override=split_csv(args.vendors) or None,
        vendor_group=split_csv(args.vendor_group) or None,
        start_date=parse_date(args.start_date),
        end_date=parse_date(args.end_date),
        years_back=args.years or default_years(params),
        days_back=args.days_back,
        agency_codes=split_csv(args.agency_codes),
        max_searches=args.max_searches,
        max_per_search=args.max_per_search,
        page_limit=args.page_limit,
        max_pages=args.max_pages,
        include_vendor_searches=args.include_vendor_searches,
        include_keyword_searches=args.include_keyword_searches and not args.skip_keyword_searches,
        include_deleted=args.include_deleted,
        env_file=ROOT / args.env_file,
        sam_quota_mode=args.sam_quota_mode,
        sam_live_budget=args.sam_live_budget,
        sam_cache_dir=sam_cache_dir,
        sam_ledger_path=sam_ledger_path,
    )

    print(
        f"SAM contract awards: mode={config.mode} years={config.years_back} "
        f"max_searches={config.max_searches} max_per_search={config.max_per_search} "
        f"sam_quota_mode={config.sam_quota_mode} sam_live_budget={config.sam_live_budget}"
    )
    try:
        rows, summary = fetch_sam_contract_awards(config, progress=lambda message: print(f"- {message}"))
    except RateLimitError:
        print("SAM_API_KEY 429; existing sam_contract_awards.csv preserved.", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"SAM contract awards failed; existing sam_contract_awards.csv preserved: {exc}", file=sys.stderr)
        return 1

    if args.dry_run:
        added = updated = total = 0
    else:
        added, updated, total = upsert_sam_contract_awards(out_path, rows)

    result = {
        "status": "ok",
        "awards_found": len(rows),
        "awards_added": added,
        "awards_updated": updated,
        "awards_total": total,
        "output": args.out,
        "dry_run": args.dry_run,
        "summary": summary,
    }

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(
            f"Done: {len(rows)} found, {added} added, {updated} updated, "
            f"{total} total in {args.out}."
        )
        print_counts("vendor_key", summary.get("by_vendor_key", {}))
        print_counts("agency", summary.get("by_agency", {}))
        print_counts("program_focus", summary.get("by_program_focus", {}))
        print(
            "date coverage: "
            f"period_end={summary.get('period_end_present', 0)}/{len(rows)} "
            f"potential_end={summary.get('potential_end_present', 0)}/{len(rows)}"
        )
    return 0


def print_counts(label: str, counts: dict[str, int], limit: int = 8) -> None:
    if not counts:
        print(f"{label}: none")
        return
    top = list(counts.items())[:limit]
    rendered = ", ".join(f"{key}={value}" for key, value in top)
    print(f"{label}: {rendered}")


if __name__ == "__main__":
    raise SystemExit(main())
