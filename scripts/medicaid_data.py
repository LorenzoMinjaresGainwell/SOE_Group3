#!/usr/bin/env python3
"""Fetch Medicaid.gov dataset catalog metadata into CSV."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.medicaid_client import (  # noqa: E402
    build_medicaid_catalog,
    parse_date,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch Medicaid.gov dataset catalog metadata into CSV.")
    parser.add_argument("--out", default="data/medicaid_data.csv")
    parser.add_argument("--since", default="", help="YYYY-MM-DD; optional incremental filter")
    parser.add_argument(
        "--date-field",
        choices=["issued_date", "released_date", "modified_date"],
        default="modified_date",
        help="date used for --since, sort, and cadence stats",
    )
    parser.add_argument("--history-years", type=int, default=20)
    parser.add_argument("--dry-run", action="store_true", help="fetch and summarize without writing CSV")
    parser.add_argument("--json", action="store_true", help="print machine-readable summary")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    since = parse_date(args.since) if args.since else None
    if args.since and since is None:
        print(f"Invalid --since date: {args.since}", file=sys.stderr)
        return 2

    out_path = ROOT / args.out
    try:
        result = build_medicaid_catalog(
            out_path=out_path,
            since=since,
            date_field=args.date_field,
            history_years=args.history_years,
            dry_run=args.dry_run,
            progress=None if args.json else lambda message: print(f"- {message}"),
        )
    except Exception as exc:
        summary = {
            "status": "blocked",
            "message": str(exc),
            "output": args.out,
            "dry_run": args.dry_run,
        }
        if args.json:
            print(json.dumps(summary, indent=2, sort_keys=True))
        else:
            print(f"Blocked: {exc}", file=sys.stderr)
        return 2

    summary = {
        "status": "ok",
        "endpoint_called": result.endpoint,
        "raw_rows": result.raw_rows,
        "rows_fetched": result.fetched_rows,
        "rows_after_since_filter": result.rows_after_filter,
        "skipped_without_id": result.skipped_without_id,
        "existing_rows": result.existing_rows,
        "rows_written": result.rows_written,
        "added": result.added,
        "updated": result.updated,
        "date_coverage": result.date_coverage,
        "monthly_cadence": result.monthly_cadence,
        "output": args.out,
        "date_field": args.date_field,
        "since": since.isoformat() if since else "",
        "dry_run": args.dry_run,
    }

    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        action = "Dry run" if args.dry_run else "Done"
        print(f"{action}: fetched {result.fetched_rows} Medicaid.gov catalog rows.")
        print(f"CSV rows {'would be written' if args.dry_run else 'written'}: {result.rows_written} ({result.added} added, {result.updated} updated)")
        print(f"Output: {args.out}")
        coverage = result.date_coverage
        cadence = result.monthly_cadence
        print(
            f"{args.date_field} coverage: {coverage['min_date'] or 'n/a'}..{coverage['max_date'] or 'n/a'} "
            f"({coverage['records_with_date']} with date, {coverage['records_missing_date']} missing)"
        )
        print(
            f"Average {args.date_field.replace('_', ' ')} posts/month over available "
            f"{args.history_years}-year window: {cadence['average_records_per_active_month']:.2f} "
            f"({cadence['records_in_window']} records across {cadence['first_month'] or 'n/a'}..{cadence['last_month'] or 'n/a'})"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
