#!/usr/bin/env python3
"""Probe one catalog source URL and classify broad state platform family evidence."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.state_http import DEFAULT_BYTE_LIMIT, DEFAULT_TIMEOUT, fetch_url, redact_url  # noqa: E402
from services.state_platform_probe import classify_platform_evidence  # noqa: E402
from services.state_source_catalog import CatalogError, StateSourceCatalog  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Inspect one state source target and classify platform evidence.")
    parser.add_argument("--targets", default="data/state_api_targets.json", help="state source catalog JSON path")
    parser.add_argument("--groups", default="data/state_platform_groups.json", help="platform grouping JSON path")
    parser.add_argument("--source-key", default="", help="source_key from data/state_api_targets.json")
    parser.add_argument("--url", default="", help="explicit URL to probe instead of catalog target_url")
    parser.add_argument("--html-file", default="", help="local HTML file to classify without fetching")
    parser.add_argument("--no-network", action="store_true", help="skip HTTP fetch and classify catalog/URL/supplied HTML only")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    parser.add_argument("--byte-limit", type=int, default=DEFAULT_BYTE_LIMIT)
    parser.add_argument("--json", action="store_true", help="print JSON")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    source: dict[str, Any] | None = None

    if args.source_key:
        try:
            catalog = StateSourceCatalog.load(targets_path=ROOT / args.targets, groups_path=ROOT / args.groups)
        except CatalogError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        source = catalog.get_source(args.source_key)
        if source is None:
            print(f"unknown source_key: {args.source_key}", file=sys.stderr)
            return 2

    target_url = args.url or str((source or {}).get("target_url") or "")
    if not target_url:
        print("provide --source-key or --url", file=sys.stderr)
        return 2

    html = read_html_file(args.html_file) if args.html_file else ""
    final_url = ""
    content_type = ""
    fetch_metadata: dict[str, Any] = {"network": "skipped"}

    if not args.no_network:
        result = fetch_url(target_url, timeout=args.timeout, byte_limit=args.byte_limit)
        fetch_metadata = result.metadata()
        html = result.body_text() if result.body else html
        final_url = result.final_url
        content_type = result.content_type

    probe_result = classify_platform_evidence(
        url=target_url,
        html=html,
        final_url=final_url,
        content_type=content_type,
        source=source,
    ).to_dict()
    probe_result["input_url"] = redact_url(str(probe_result.get("input_url") or ""))
    probe_result["final_url"] = redact_url(str(probe_result.get("final_url") or ""))

    output = {
        "status": "ok",
        "source": source_summary(source) if source else None,
        "target": {
            "url": redact_url(target_url),
            "explicit_url": bool(args.url),
            "network_enabled": not args.no_network,
            "html_file": args.html_file or "",
        },
        "fetch": fetch_metadata,
        "probe": probe_result,
    }

    if args.json:
        print(json.dumps(output, indent=2, sort_keys=True))
    else:
        label = args.source_key or redact_url(target_url)
        print(f"{label}: {probe_result['platform_family']} confidence={probe_result['confidence']}")
        if source:
            print(f"catalog_family={source.get('platform_family') or ''} url={redact_url(target_url)}")
        if fetch_metadata.get("network") != "skipped":
            print(
                f"fetch status={fetch_metadata.get('status_code')} type={fetch_metadata.get('content_type')} "
                f"bytes={fetch_metadata.get('bytes_read')} final={fetch_metadata.get('final_url')}"
            )
        for item in probe_result.get("evidence") or []:
            print(f"- {item}")
    return 0


def read_html_file(path_text: str) -> str:
    path = Path(path_text)
    if not path.is_absolute():
        path = ROOT / path
    return path.read_text(encoding="utf-8", errors="replace")


def source_summary(source: dict[str, Any] | None) -> dict[str, Any]:
    if not source:
        return {}
    return {
        "source_key": source.get("source_key"),
        "source_name": source.get("source_name"),
        "jurisdiction_code": source.get("jurisdiction_code"),
        "jurisdiction_name": source.get("jurisdiction_name"),
        "target_url": redact_url(str(source.get("target_url") or "")),
        "access_method": source.get("access_method"),
        "platform": source.get("platform"),
        "platform_family": source.get("platform_family"),
        "information_tags": source.get("information_tags") or [],
        "adapter_targets": source.get("adapter_targets") or [],
        "confidence": source.get("confidence"),
        "classification_confidence": source.get("classification_confidence"),
    }


if __name__ == "__main__":
    raise SystemExit(main())
