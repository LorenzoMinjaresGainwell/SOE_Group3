#!/usr/bin/env python3
"""Fetch awarded contracts for monitored vendors from USAspending.gov.

Run from repo root:
    ./scripts/usaspending_contracts.py --max-per-vendor 25
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.usaspending_client import (  # noqa: E402
    config_from_parameters,
    fetch_vendor_contracts,
    load_search_parameters,
    upsert_contracts,
)


def parse_date(value: str) -> dt.date | None:
    if not value:
        return None
    return dt.date.fromisoformat(value)


def split_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch vendor contract awards from USAspending.gov.")
    parser.add_argument("--params", default="data/search_parameters.json")
    parser.add_argument("--out", default="data/contracts.csv")
    parser.add_argument("--vendors", default="", help="comma list; overrides vendors in params file")
    parser.add_argument("--vendor-group", default="", help="comma list of configured vendor names to use with aliases")
    parser.add_argument("--years", type=int, default=0, help="lookback years; defaults to params file")
    parser.add_argument("--start-date", default="", help="YYYY-MM-DD")
    parser.add_argument("--end-date", default="", help="YYYY-MM-DD")
    parser.add_argument("--max-per-vendor", type=int, default=0, help="defaults to params file")
    parser.add_argument("--only-keyword-matches", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    params = load_search_parameters(ROOT / args.params)
    if args.vendor_group:
        selected = {name.lower() for name in split_csv(args.vendor_group)}
        params = dict(params)
        params["vendors"] = [
            vendor for vendor in params.get("vendors", [])
            if str(vendor.get("name") if isinstance(vendor, dict) else vendor).lower() in selected
        ]
    config = config_from_parameters(
        params,
        vendors_override=split_csv(args.vendors) or None,
        years_back=args.years or None,
        start_date=parse_date(args.start_date),
        end_date=parse_date(args.end_date),
        max_per_vendor=args.max_per_vendor or None,
        only_keyword_matches=True if args.only_keyword_matches else None,
    )

    if not config.vendors:
        print("No vendors configured. Add vendors to data/search_parameters.json or pass --vendors.", file=sys.stderr)
        return 2

    print(
        f"USAspending vendor contracts: vendors={len(config.vendors)} "
        f"window={config.start_date}..{config.end_date} max_per_vendor={config.max_per_vendor}"
    )
    contracts = fetch_vendor_contracts(config, progress=lambda message: print(f"- {message}"))

    if args.dry_run:
        added = updated = total = 0
    else:
        added, updated, total = upsert_contracts(ROOT / args.out, contracts)

    result = {
        "status": "ok",
        "contracts_found": len(contracts),
        "contracts_added": added,
        "contracts_updated": updated,
        "contracts_total": total,
        "output": args.out,
        "dry_run": args.dry_run,
    }

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(
            f"Done: {len(contracts)} found, {added} added, {updated} updated, "
            f"{total} total in {args.out}."
        )
        if contracts[:5]:
            print("Top matches:")
            for contract in contracts[:5]:
                print(
                    f"[{contract['relevance_score']}] {contract['vendor_name']} | "
                    f"${int(contract['award_amount']):,} | {contract['end_date'] or 'no end'} | "
                    f"{contract['description'][:90]}"
                )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
