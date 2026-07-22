#!/usr/bin/env python3
"""Enrich Federal Register docket IDs with light Regulations.gov metadata."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.regulations_client import (  # noqa: E402
    REGULATIONS_CSV_FIELDS,
    RegulationsConfig,
    docket_refs_from_values,
    fetch_regulations_updates,
    load_docket_refs_from_federal_register,
    read_csv,
    regulations_api_key_from_env,
    upsert_regulations_updates,
    write_csv,
)


def split_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch Regulations.gov metadata for Federal Register docket IDs.")
    parser.add_argument(
        "--from-federal-register",
        default=None,
        help="Federal Register CSV to read. Defaults to data/federal_register_updates.csv unless --dockets is used.",
    )
    parser.add_argument("--dockets", default="", help="comma list of docket IDs to fetch directly")
    parser.add_argument("--max-records", type=int, default=100)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--out", default="data/regulations_updates.csv")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--json", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.max_records < 1:
        print("--max-records must be at least 1.", file=sys.stderr)
        return 2

    from_federal_register = args.from_federal_register
    if from_federal_register is None and not args.dockets:
        from_federal_register = "data/federal_register_updates.csv"

    refs = []
    if from_federal_register:
        refs.extend(load_docket_refs_from_federal_register(ROOT / from_federal_register))
    if args.dockets:
        refs.extend(docket_refs_from_values(split_csv(args.dockets)))
    refs = dedupe_refs(refs)

    if not refs:
        print("No docket IDs found. Pass --dockets or generate data/federal_register_updates.csv first.", file=sys.stderr)
        return 2

    api_key = regulations_api_key_from_env(ROOT / args.env_file)
    if not api_key:
        print("Missing REGULATIONS_API_KEY in .env.", file=sys.stderr)
        return 2

    print(f"Regulations.gov: docket_refs={len(refs)} max_records={args.max_records}")
    rows = fetch_regulations_updates(
        RegulationsConfig(api_key=api_key, docket_refs=refs, max_records=args.max_records),
        progress=lambda message: print(f"- {message}"),
    )

    output_path = ROOT / args.out
    if args.dry_run:
        added = updated = total = 0
    else:
        if rows:
            added, updated, total = upsert_regulations_updates(output_path, rows)
        else:
            if not output_path.exists():
                write_csv(output_path, REGULATIONS_CSV_FIELDS, [])
            added = updated = 0
            total = len(read_csv(output_path))

    result = {
        "status": "ok",
        "records_found": len(rows),
        "records_added": added,
        "records_updated": updated,
        "records_total": total,
        "output": args.out,
        "dry_run": args.dry_run,
        "from_federal_register": from_federal_register or "",
        "explicit_dockets": split_csv(args.dockets),
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
                    f"[{row['docket_status']}] {row['regulations_docket_id'] or row['federal_register_docket_id']} | "
                    f"comments={row['comment_count'] or 'n/a'} attachments={row['attachment_count'] or 'n/a'} | "
                    f"{row['title'][:90]}"
                )
    return 0


def dedupe_refs(refs):
    output = []
    seen = set()
    for ref in refs:
        key = ref.docket_id
        if key in seen:
            continue
        seen.add(key)
        output.append(ref)
    return output


if __name__ == "__main__":
    raise SystemExit(main())
