#!/usr/bin/env python3
"""List/query state source catalogs used by state platform adapter sessions."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.state_http import redact_url  # noqa: E402
from services.state_source_catalog import CatalogError, StateSourceCatalog, priority_sort_key  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="List/query state source catalog entries.")
    parser.add_argument("--targets", default="data/state_api_targets.json", help="state source catalog JSON path")
    parser.add_argument("--groups", default="data/state_platform_groups.json", help="platform grouping JSON path")
    parser.add_argument("--source-key", default="", help="exact source_key")
    parser.add_argument("--state", default="", help="state/territory code, name, or alias")
    parser.add_argument("--platform-family", default="", help="platform_family from state_platform_groups.json")
    parser.add_argument("--tag", action="append", default=[], help="required information tag; may be repeated or comma-separated")
    parser.add_argument("--adapter-target", action="append", default=[], help="required adapter target; repeated or comma-separated")
    parser.add_argument("--priority", default="", help="implementation priority: implemented, high, medium, low")
    parser.add_argument("--confidence", default="", help="source or classification confidence")
    parser.add_argument("--limit", type=int, default=0, help="max sources to print")
    parser.add_argument("--summary", action="store_true", help="include catalog summary")
    parser.add_argument("--json", action="store_true", help="print JSON")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        catalog = StateSourceCatalog.load(targets_path=ROOT / args.targets, groups_path=ROOT / args.groups)
    except CatalogError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    sources = catalog.query_sources(
        source_key=args.source_key or None,
        state=args.state or None,
        platform_family=args.platform_family or None,
        tags=args.tag or None,
        adapter_targets=args.adapter_target or None,
        priority=args.priority or None,
        confidence=args.confidence or None,
        limit=args.limit or None,
    )
    sources = sorted(sources, key=priority_sort_key)
    if args.limit:
        sources = sources[: args.limit]

    output_sources = [redact_source(source) for source in sources]
    result: dict[str, Any] = {
        "status": "ok",
        "count": len(output_sources),
        "sources": output_sources,
    }
    if args.summary:
        result["summary"] = catalog.summary()

    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        if args.summary:
            summary = catalog.summary()
            print(f"Sources: {summary['source_count']} across {summary['jurisdiction_count']} jurisdictions")
        for source in output_sources:
            print(format_source(source))
    return 0


def redact_source(source: dict[str, Any]) -> dict[str, Any]:
    safe_source = dict(source)
    if safe_source.get("target_url"):
        safe_source["target_url"] = redact_url(str(safe_source["target_url"]))
    return safe_source


def format_source(source: dict[str, Any]) -> str:
    tags = ",".join(source.get("information_tags") or [])
    targets = ",".join(source.get("adapter_targets") or [])
    return (
        f"{source.get('source_key')} | {source.get('jurisdiction_code')} | "
        f"{source.get('platform_family') or 'unknown_family'} | {source.get('confidence')} | "
        f"tags={tags} | targets={targets} | {source.get('target_url')}"
    )


if __name__ == "__main__":
    raise SystemExit(main())
