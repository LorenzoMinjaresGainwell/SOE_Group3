#!/usr/bin/env python3
"""Fetch CMS/HHS Federal Register updates into data/federal_register_updates.csv.

Federal Register uses no API key. The companion Regulations.gov API can use
Federal Register docket IDs, but Regulations.gov v4 returns API_KEY_MISSING
without an api.data.gov key, so it is documented here for later integration only.
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

from services.federal_register_client import (  # noqa: E402
    DEFAULT_KEYWORDS,
    FEDERAL_REGISTER_CSV_FIELDS,
    RELATED_API_NOTES,
    FederalRegisterConfig,
    date_window,
    fetch_federal_register_updates,
    upsert_federal_register_updates,
    write_csv,
)


def parse_date(value: str) -> dt.date | None:
    if not value:
        return None
    return dt.date.fromisoformat(value)


def split_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch CMS/HHS Federal Register updates.")
    parser.add_argument("--keywords", default=",".join(DEFAULT_KEYWORDS), help="comma list of search keywords")
    parser.add_argument("--days-back", type=int, default=365, help="lookback window when --start-date is omitted")
    parser.add_argument("--start-date", default="", help="YYYY-MM-DD; overrides --days-back")
    parser.add_argument("--end-date", default="", help="YYYY-MM-DD; defaults to today")
    parser.add_argument("--max-records", type=int, default=100)
    parser.add_argument("--dry-run", action="store_true", help="fetch and summarize without writing CSV")
    parser.add_argument("--out", default="data/federal_register_updates.csv")
    parser.add_argument("--json", action="store_true", help="print JSON summary")
    parser.add_argument("--api-notes", action="store_true", help="print related API notes")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    keywords = split_csv(args.keywords)
    if not keywords:
        print("No keywords provided.", file=sys.stderr)
        return 2
    if args.max_records < 1:
        print("--max-records must be at least 1.", file=sys.stderr)
        return 2

    start, end = date_window(args.days_back, parse_date(args.start_date), parse_date(args.end_date))
    config = FederalRegisterConfig(
        keywords=keywords,
        start_date=start,
        end_date=end,
        max_records=args.max_records,
    )

    print(
        f"Federal Register: window={config.start_date}..{config.end_date} "
        f"keywords={len(config.keywords)} max_records={config.max_records}"
    )
    rows = fetch_federal_register_updates(config, progress=lambda message: print(f"- {message}"))

    output_path = ROOT / args.out
    if args.dry_run:
        added = updated = total = 0
    else:
        if rows:
            added, updated, total = upsert_federal_register_updates(output_path, rows)
        else:
            write_csv(output_path, FEDERAL_REGISTER_CSV_FIELDS, [])
            added = updated = total = 0

    result = {
        "status": "ok",
        "records_found": len(rows),
        "records_added": added,
        "records_updated": updated,
        "records_total": total,
        "output": args.out,
        "dry_run": args.dry_run,
        "start_date": config.start_date.isoformat(),
        "end_date": config.end_date.isoformat(),
        "related_apis": RELATED_API_NOTES,
    }

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        action = "Dry run" if args.dry_run else "Done"
        print(f"{action}: {len(rows)} found, {added} added, {updated} updated, {total} total in {args.out}.")
        if rows[:5]:
            print("Top matches:")
            for row in rows[:5]:
                print(
                    f"[{row['relevance_score']}] {row['publication_date']} | "
                    f"{row['document_type']} | {row['title'][:100]}"
                )
        if args.api_notes:
            print("Related APIs/sources:")
            for note in RELATED_API_NOTES:
                print(
                    f"- {note['source']}: key={note['api_key_required']} | "
                    f"{note['url']} | {note['notes']}"
                )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
