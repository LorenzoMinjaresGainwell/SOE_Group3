#!/usr/bin/env python3
"""Fetch state contract records from supported state portals.

PA is currently supported:
    ./scripts/state_contracts.py --states PA --vendor-group "Gainwell Technologies"
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.state_contracts import fetch_state_contracts  # noqa: E402
from services.state_contracts.store import upsert_state_contracts  # noqa: E402
from services.usaspending_client import DEFAULT_KEYWORDS, load_search_parameters, vendor_searches  # noqa: E402


def split_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def configured_vendor_terms(params: dict, groups: list[str]) -> list[str]:
    selected = {group.lower() for group in groups}
    state_terms = params.get("state_contracts", {}).get("vendor_terms_by_group", {})
    terms: list[str] = []

    for group_name, group_terms in state_terms.items():
        if selected and group_name.lower() not in selected:
            continue
        terms.extend(str(term) for term in group_terms if str(term).strip())

    if terms:
        return sorted(set(terms), key=str.lower)

    for vendor in vendor_searches(params):
        if selected and vendor.name.lower() not in selected:
            continue
        terms.extend(vendor.queries)
    return sorted(set(terms), key=str.lower)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch state contract records.")
    parser.add_argument("--states", default="PA", help="comma list of state abbreviations")
    parser.add_argument("--params", default="data/search_parameters.json")
    parser.add_argument("--out", default="data/state_contracts.csv")
    parser.add_argument("--vendors", default="", help="comma list of direct vendor search terms")
    parser.add_argument("--vendor-group", default="", help="comma list of configured vendor names, uses aliases")
    parser.add_argument("--keywords", default="", help="comma list; defaults to params monitored_keywords")
    parser.add_argument("--max-per-vendor", type=int, default=100)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    params = load_search_parameters(ROOT / args.params)
    vendor_terms = split_csv(args.vendors)
    if not vendor_terms:
        vendor_terms = configured_vendor_terms(params, split_csv(args.vendor_group))
    if not vendor_terms:
        print("No vendors supplied. Use --vendors or --vendor-group.", file=sys.stderr)
        return 2

    keywords = split_csv(args.keywords) or [str(item) for item in params.get("monitored_keywords") or DEFAULT_KEYWORDS]
    states = split_csv(args.states)

    print(
        f"State contracts: states={','.join(states)} vendor_terms={len(vendor_terms)} "
        f"max_per_vendor={args.max_per_vendor}"
    )
    records = fetch_state_contracts(
        states=states,
        vendor_terms=vendor_terms,
        keywords=keywords,
        max_per_vendor=args.max_per_vendor,
        progress=lambda message: print(f"- {message}"),
    )

    if args.dry_run:
        added = updated = total = 0
    else:
        added, updated, total = upsert_state_contracts(ROOT / args.out, records)

    result = {
        "status": "ok",
        "records_found": len(records),
        "records_added": added,
        "records_updated": updated,
        "records_total": total,
        "output": args.out,
        "dry_run": args.dry_run,
    }
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"Done: {len(records)} found, {added} added, {updated} updated, {total} total in {args.out}.")
        for record in records[:8]:
            amount = int(float(record.get("amount") or 0))
            print(
                f"[{record['relevance_score']}] {record['state']} | {record['vendor_name']} | "
                f"${amount:,} | end={record['end_date'] or 'unknown'} | {record['title'][:90]}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
