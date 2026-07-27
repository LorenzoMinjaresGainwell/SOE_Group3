#!/usr/bin/env python3
"""Fetch state healthcare policy/program update records from supported state sources."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.state_updates import fetch_state_updates  # noqa: E402
from services.state_updates.store import upsert_state_updates  # noqa: E402
from services.usaspending_client import DEFAULT_KEYWORDS, load_search_parameters  # noqa: E402


def split_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch state healthcare policy/program update records.")
    parser.add_argument("--states", default="PA,TX", help="comma list of state abbreviations")
    parser.add_argument("--params", default="data/search_parameters.json")
    parser.add_argument("--out", default="data/state_policy_updates.csv")
    parser.add_argument("--keywords", default="", help="comma list; defaults to params monitored_keywords")
    parser.add_argument("--max-records", type=int, default=300)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    params = load_search_parameters(ROOT / args.params)
    keywords = split_csv(args.keywords) or [str(item) for item in params.get("monitored_keywords") or DEFAULT_KEYWORDS]
    states = split_csv(args.states)
    messages: list[str] = []

    def progress(message: str) -> None:
        messages.append(message)
        print(f"- {message}", file=sys.stderr if args.json else sys.stdout)

    banner = f"State updates: states={','.join(states)} keywords={len(keywords)} max_records={args.max_records}"
    print(banner, file=sys.stderr if args.json else sys.stdout)

    records = fetch_state_updates(
        states=states,
        keywords=keywords,
        max_records=args.max_records,
        progress=progress,
    )

    if args.dry_run:
        added = updated = total = 0
    else:
        added, updated, total = upsert_state_updates(ROOT / args.out, records)

    result = {
        "status": "ok",
        "records_found": len(records),
        "records_added": added,
        "records_updated": updated,
        "records_total": total,
        "output": args.out,
        "dry_run": args.dry_run,
        "messages": messages,
    }
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(f"Done: {len(records)} found, {added} added, {updated} updated, {total} total in {args.out}.")
        for record in records[:8]:
            print(
                f"[{record['importance_score']}] {record['state']} | {record['record_type']} | "
                f"posted={record['posted_date'] or 'unknown'} | {record['title'][:90]}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
