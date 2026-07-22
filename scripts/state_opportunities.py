#!/usr/bin/env python3
"""Fetch state opportunity/RFP records from supported state portals.

PA is currently supported:
    ./scripts/state_opportunities.py --states PA --keywords "Medicaid,MMIS,rural health"
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.state_opportunities import fetch_state_opportunities  # noqa: E402
from services.state_opportunities.store import upsert_state_opportunities  # noqa: E402
from services.usaspending_client import DEFAULT_KEYWORDS, load_search_parameters  # noqa: E402


def split_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch state opportunity/RFP records.")
    parser.add_argument("--states", default="PA", help="comma list of state abbreviations")
    parser.add_argument("--params", default="data/search_parameters.json")
    parser.add_argument("--out", default="data/state_opportunities.csv")
    parser.add_argument("--keywords", default="", help="comma list; defaults to params monitored_keywords")
    parser.add_argument("--days-back", type=int, default=365, help="recent award window; open future-due solicitations are kept")
    parser.add_argument("--max-records", type=int, default=100)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    params = load_search_parameters(ROOT / args.params)
    keywords = split_csv(args.keywords) or [str(item) for item in params.get("monitored_keywords") or DEFAULT_KEYWORDS]
    states = split_csv(args.states)

    print(
        f"State opportunities: states={','.join(states)} keywords={len(keywords)} "
        f"days_back={args.days_back} max_records={args.max_records}"
    )
    records = fetch_state_opportunities(
        states=states,
        keywords=keywords,
        days_back=args.days_back,
        max_records=args.max_records,
        progress=lambda message: print(f"- {message}"),
    )

    if args.dry_run:
        added = updated = total = 0
    else:
        added, updated, total = upsert_state_opportunities(ROOT / args.out, records)

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
            amount_text = f"${amount:,}" if amount else "amount=unknown"
            print(
                f"[{record['relevance_score']}] {record['state']} | {record['document_type']} | "
                f"due={record['due_date'] or 'unknown'} | {amount_text} | {record['title'][:90]}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
