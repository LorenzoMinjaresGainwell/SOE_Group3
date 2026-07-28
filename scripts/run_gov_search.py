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

from services.gov_api_client import SearchConfig, run_gov_search  # noqa: E402
from services.search_taxonomy import load_search_taxonomy  # noqa: E402

DEFAULT_SOURCES = "sam,grants,federal_register,medicaid,cms_provider,usaspending"


def parse_date(value: str) -> dt.date | None:
    if not value:
        return None
    return dt.date.fromisoformat(value)


def split_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def repo_path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


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
    parser.add_argument("--keywords", default="", help="comma list; defaults to the monitored business taxonomy")
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
        help="optional comma list: o,k,r,p,a,s,u,i or eval. Empty uses core default o,k,r,p.",
    )
    parser.add_argument("--sam-quota-mode", choices=["cache-only", "live"], default="cache-only")
    parser.add_argument("--sam-live-budget", type=int, default=0, help="live SAM calls allowed today; default 0")
    parser.add_argument("--sam-cache-dir", default="data/raw/sam", help="SAM raw cache directory")
    parser.add_argument("--sam-ledger-path", default="", help="SAM call ledger path; defaults under cache dir")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true", help="print full JSON summary")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    vendors = split_csv(args.vendors)
    vendors.extend(load_vendors(ROOT / args.vendors_file))
    vendors = sorted(set(vendors), key=str.lower)

    sam_cache_dir = repo_path(args.sam_cache_dir)
    if args.sam_ledger_path:
        sam_ledger_path = repo_path(args.sam_ledger_path)
    else:
        sam_ledger_path = sam_cache_dir / "call_ledger.ndjson"

    config = SearchConfig(
        mode=args.mode,
        sources=split_csv(args.sources),
        keywords=split_csv(args.keywords) or load_search_taxonomy().business_terms,
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
        sam_quota_mode=args.sam_quota_mode,
        sam_live_budget=args.sam_live_budget,
        sam_cache_dir=sam_cache_dir,
        sam_ledger_path=sam_ledger_path,
    )

    print(f"mode={config.mode} sources={','.join(config.sources or [])} max_per_source={config.max_per_source}")
    if vendors:
        print(f"vendors={len(vendors)}")
    elif "usaspending" in (config.sources or []):
        print("usaspending: no CLI vendors; using data/search_parameters.json if configured")
    if config.sam_ptypes:
        print(f"sam_ptypes={','.join(config.sam_ptypes)}")
    elif "sam" in (config.sources or []):
        print("sam_ptypes=default(o,k,r,p)")
    if "sam" in (config.sources or []):
        print(f"sam_quota_mode={config.sam_quota_mode} sam_live_budget={config.sam_live_budget}")

    result = run_gov_search(config, progress=lambda message: print(f"- {message}"))

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(result["message"])
        for source in result["sources"]:
            print(f"{source['source']}: {source['status']} ({source.get('records_found', 0)} records) {source.get('message', '')}")
            if source.get("ptype_counts"):
                counts = ",".join(f"{ptype}:{count}" for ptype, count in source["ptype_counts"].items())
                buckets = ",".join(f"{name}:{count}" for name, count in source.get("notice_bucket_counts", {}).items())
                print(f"  ptype_counts={counts} notice_buckets={buckets}")
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
