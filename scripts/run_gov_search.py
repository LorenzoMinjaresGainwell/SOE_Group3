#!/usr/bin/env python3
"""Run the government-source search and update dashboard CSVs.

Run from repo root:
    python scripts/run_gov_search.py --mode continue
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

from services.gov_api_client import DEFAULT_KEYWORDS, SearchConfig, run_gov_search  # noqa: E402

DEFAULT_SOURCES = "sam,grants,federal_register,medicaid,cms_provider,usaspending"


def parse_date(value: str) -> dt.date | None:
    if not value:
        return None
    return dt.date.fromisoformat(value)


def split_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def load_vendors(path: Path) -> list[str]:
    if not path.exists():
        return []
    vendors: list[str] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "," in line:
            first = line.split(",", 1)[0].strip()
            if first.lower() in {"vendor", "vendor_name", "name"}:
                continue
            if first:
                vendors.append(first)
        else:
            vendors.append(line)
    return vendors


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Search gov APIs and update dashboard CSVs.")
    parser.add_argument("--mode", choices=["continue", "historic"], default="continue")
    parser.add_argument("--sources", default=DEFAULT_SOURCES, help="comma list of source keys")
    parser.add_argument("--keywords", default=",".join(DEFAULT_KEYWORDS), help="comma list of business keywords")
    parser.add_argument("--max-per-source", type=int, default=25)
    parser.add_argument("--days-back", type=int, default=60, help="used before a source has a previous run")
    parser.add_argument("--overlap-days", type=int, default=14, help="continue mode overlap after last run")
    parser.add_argument("--start-date", default="", help="YYYY-MM-DD for historic mode")
    parser.add_argument("--end-date", default="", help="YYYY-MM-DD; defaults today")
    parser.add_argument("--vendors", default="", help="comma list for USAspending vendor lookups")
    parser.add_argument("--vendors-file", default="data/vendors.csv")
    parser.add_argument(
        "--sam-ptypes",
        default="",
        help="optional comma list: u,p,a,r,s,o,g,k,i. Empty means omit ptype filter.",
    )
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true", help="print full JSON summary")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    vendors = split_csv(args.vendors)
    vendors.extend(load_vendors(ROOT / args.vendors_file))
    vendors = sorted(set(vendors), key=str.lower)

    config = SearchConfig(
        mode=args.mode,
        sources=split_csv(args.sources),
        keywords=split_csv(args.keywords),
        max_per_source=args.max_per_source,
        days_back=args.days_back,
        overlap_days=args.overlap_days,
        start_date=parse_date(args.start_date),
        end_date=parse_date(args.end_date),
        data_dir=ROOT / "data",
        vendors=vendors,
        sam_ptypes=split_csv(args.sam_ptypes),
        dry_run=args.dry_run,
        env_file=ROOT / args.env_file,
    )

    print(f"mode={config.mode} sources={','.join(config.sources or [])} max_per_source={config.max_per_source}")
    if vendors:
        print(f"vendors={len(vendors)}")
    elif "usaspending" in (config.sources or []):
        print("usaspending: no CLI vendors; using data/search_parameters.json if configured")
    if config.sam_ptypes:
        print(f"sam_ptypes={','.join(config.sam_ptypes)}")

    result = run_gov_search(config, progress=lambda message: print(f"- {message}"))

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(result["message"])
        for source in result["sources"]:
            print(f"{source['source']}: {source['status']} ({source.get('records_found', 0)} records) {source.get('message', '')}")
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
