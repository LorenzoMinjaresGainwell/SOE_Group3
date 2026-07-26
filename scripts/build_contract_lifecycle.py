#!/usr/bin/env python3
"""Build the federal contract lifecycle catalog from existing source CSVs."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.contract_lifecycle import (  # noqa: E402
    DEFAULT_OPTIONAL_INPUTS,
    LifecycleBlockedError,
    build_contract_lifecycle,
    write_lifecycle,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build data/federal_contract_lifecycle.csv from USAspending contracts CSV.")
    parser.add_argument("--out", default="data/federal_contract_lifecycle.csv")
    parser.add_argument("--contracts", default="data/contracts.csv")
    parser.add_argument("--sam-contract-awards", default=DEFAULT_OPTIONAL_INPUTS["sam_contract_awards"])
    parser.add_argument("--federal-opportunities", default=DEFAULT_OPTIONAL_INPUTS["sam_opportunities"])
    parser.add_argument("--vendor-entities", default=DEFAULT_OPTIONAL_INPUTS["vendor_entities"])
    parser.add_argument("--search-parameters", default="data/search_parameters.json")
    parser.add_argument("--recompete-months", type=int, default=0, help="override search_parameters usaspending.recompete_months")
    parser.add_argument("--dry-run", action="store_true", help="summarize without writing output CSV")
    parser.add_argument("--json", action="store_true", help="print machine-readable summary")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    optional_inputs = {
        "sam_contract_awards": args.sam_contract_awards,
        "sam_opportunities": args.federal_opportunities,
        "vendor_entities": args.vendor_entities,
    }
    try:
        result = build_contract_lifecycle(
            ROOT,
            contracts_path=args.contracts,
            optional_inputs=optional_inputs,
            search_parameters_path=args.search_parameters,
            recompete_months=args.recompete_months or None,
            progress=None if args.json else lambda message: print(f"- {message}"),
        )
    except LifecycleBlockedError as exc:
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
        write_lifecycle(ROOT / args.out, result.rows)

    summary = {
        "status": "ok",
        "rows": len(result.rows),
        "counts_by_status": result.counts_by_status,
        "counts_by_vendor": result.counts_by_vendor,
        "source_counts": result.source_counts,
        "missing_optional_inputs": result.missing_optional_inputs,
        "empty_optional_inputs": result.empty_optional_inputs,
        "output": args.out,
        "dry_run": args.dry_run,
        "source_scope": "multi_source" if len(result.source_counts) > 1 else "usaspending_only",
    }

    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        action = "Dry run" if args.dry_run else "Done"
        print(f"{action}: {len(result.rows)} rows in {args.out}.")
        print("Counts by status:")
        for status, count in result.counts_by_status.items():
            print(f"- {status}: {count}")
        print("Counts by vendor:")
        for vendor_key, count in result.counts_by_vendor.items():
            print(f"- {vendor_key}: {count}")
        print("Counts by source:")
        for source_key, count in result.source_counts.items():
            print(f"- {source_key}: {count}")
        if result.missing_optional_inputs:
            print("Missing SAM/entity inputs not joined:")
            for path in result.missing_optional_inputs:
                print(f"- {path}")
        if result.empty_optional_inputs:
            print("Empty optional inputs not joined:")
            for path in result.empty_optional_inputs:
                print(f"- {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
