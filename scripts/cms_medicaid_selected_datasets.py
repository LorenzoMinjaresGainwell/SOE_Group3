#!/usr/bin/env python3
"""Pull curated CMS/Medicaid dataset signals for federal scoring."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.cms_medicaid_dataset_client import (  # noqa: E402
    build_selected_dataset_signals,
    write_signals,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build curated CMS/Medicaid scoring signals CSV.")
    parser.add_argument("--out", default="data/cms_medicaid_dataset_signals.csv")
    parser.add_argument("--dry-run", action="store_true", help="pull and summarize signals without writing CSV")
    parser.add_argument("--json", action="store_true", help="print machine-readable summary")
    parser.add_argument("--max-source-rows", type=int, default=15000, help="safety cap per selected datastore dataset")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        result = build_selected_dataset_signals(
            max_source_rows=args.max_source_rows,
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

    if not args.dry_run:
        write_signals(ROOT / args.out, result.rows)

    summary = {
        "status": "ok",
        "rows": len(result.rows),
        "counts_by_source": result.counts_by_source,
        "counts_by_endpoint": result.counts_by_endpoint,
        "cms_signal_count": result.cms_signal_count,
        "medicaid_signal_count": result.medicaid_signal_count,
        "rht_signal_count": result.rht_signal_count,
        "dataset_ids": [
            {
                "dataset_id": item.dataset_id,
                "title": item.dataset_title,
                "source_key": item.source_key,
                "endpoint_type": item.endpoint_type,
                "source_rows": item.source_rows,
                "signals": item.signals,
                "why_matters": item.why_matters,
            }
            for item in result.dataset_summaries
        ],
        "output": args.out,
        "dry_run": args.dry_run,
    }

    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        action = "Dry run" if args.dry_run else "Done"
        print(f"{action}: {len(result.rows)} scoring signals from {len(result.dataset_summaries)} selected datasets.")
        print(f"Output: {args.out}")
        print("Counts by source:")
        for source_key, count in result.counts_by_source.items():
            print(f"- {source_key}: {count}")
        print(f"RHT signals: {result.rht_signal_count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
