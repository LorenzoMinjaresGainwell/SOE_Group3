#!/usr/bin/env python3
"""Build the federal update catalog from existing federal source outputs."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.federal_update_catalog import (  # noqa: E402
    CatalogBlockedError,
    build_federal_update_catalog,
    write_catalog,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build data/federal_updates_catalog.csv from existing source CSVs.")
    parser.add_argument("--out", default="data/federal_updates_catalog.csv")
    parser.add_argument("--federal-register", default="data/federal_register_updates.csv")
    parser.add_argument("--regulations", default="data/regulations_updates.csv")
    parser.add_argument("--federal-grants", default="data/federal_grants.csv")
    parser.add_argument("--federal-opportunities", default="data/federal_opportunities.csv")
    parser.add_argument("--cms-provider-data", default="data/cms_provider_data.csv")
    parser.add_argument("--medicaid-data", default="data/medicaid_data.csv")
    parser.add_argument("--dataset-signals", default="data/cms_medicaid_dataset_signals.csv")
    parser.add_argument("--search-parameters", default="data/search_parameters.json")
    parser.add_argument("--dry-run", action="store_true", help="summarize without writing output CSV")
    parser.add_argument("--json", action="store_true", help="print machine-readable summary")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    input_paths = {
        "federal_register": args.federal_register,
        "regulations": args.regulations,
        "grants": args.federal_grants,
        "sam_opportunities": args.federal_opportunities,
        "cms_provider": args.cms_provider_data,
        "medicaid_data": args.medicaid_data,
        "dataset_signals": args.dataset_signals,
    }

    try:
        result = build_federal_update_catalog(
            ROOT,
            input_paths=input_paths,
            search_parameters_path=args.search_parameters,
            progress=None if args.json else lambda message: print(f"- {message}"),
        )
    except CatalogBlockedError as exc:
        summary = {
            "status": "blocked",
            "message": str(exc),
            "missing_inputs": exc.missing_inputs,
            "empty_inputs": exc.empty_inputs,
            "output": args.out,
            "dry_run": args.dry_run,
        }
        if args.json:
            print(json.dumps(summary, indent=2, sort_keys=True))
        else:
            print(f"Blocked: {exc}", file=sys.stderr)
        return 2

    if not args.dry_run:
        write_catalog(ROOT / args.out, result.rows)

    summary = {
        "status": "ok",
        "rows": len(result.rows),
        "counts_by_source": result.counts_by_source,
        "missing_inputs": result.missing_inputs,
        "empty_inputs": result.empty_inputs,
        "weak_inputs": result.weak_inputs,
        "output": args.out,
        "dry_run": args.dry_run,
    }

    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        action = "Dry run" if args.dry_run else "Done"
        print(f"{action}: {len(result.rows)} rows in {args.out}.")
        print("Counts by source:")
        for source_key, count in result.counts_by_source.items():
            print(f"- {source_key}: {count}")
        if result.missing_inputs:
            print("Missing inputs:")
            for path in result.missing_inputs:
                print(f"- {path}")
        if result.empty_inputs:
            print("Empty inputs:")
            for path in result.empty_inputs:
                print(f"- {path}")
        if result.weak_inputs:
            print("Weak inputs:")
            for path in result.weak_inputs:
                print(f"- {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
