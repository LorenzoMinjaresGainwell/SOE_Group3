#!/usr/bin/env python3
"""Fetch CMS Provider Data catalog metadata into CSV.

This caller uses the public CMS Provider Data metastore API. It captures dataset
metadata, not row-level provider data, so it is suitable for tracking CMS data
releases and update cadence without downloading huge CSV datasets.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Any

API_URL = "https://data.cms.gov/provider-data/api/1/metastore/schemas/dataset/items"
USER_AGENT = "soe-group3-cms-api-caller/0.1"
FIELDNAMES = [
    "source_record_id",
    "title",
    "issued_date",
    "released_date",
    "modified_date",
    "next_update_date",
    "landing_page",
    "download_url",
    "publisher",
    "keywords",
    "description",
    "raw_json",
]


def http_json(url: str, timeout: int = 45) -> Any:
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": USER_AGENT},
    )
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = exc
            time.sleep(1 + attempt)
    raise RuntimeError(f"CMS API request failed: {last_error}")


def parse_date(value: Any) -> dt.date | None:
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z"):
        try:
            return dt.datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    try:
        return dt.datetime.fromisoformat(text).date()
    except ValueError:
        return None


def iso_date(value: Any) -> str:
    parsed = parse_date(value)
    return parsed.isoformat() if parsed else ""


def first_download_url(row: dict[str, Any]) -> str:
    for dist in row.get("distribution") or []:
        if isinstance(dist, dict):
            return dist.get("downloadURL") or dist.get("accessURL") or ""
    return ""


def publisher_name(row: dict[str, Any]) -> str:
    publisher = row.get("publisher")
    if isinstance(publisher, dict):
        return str(publisher.get("name") or "")
    return str(publisher or "")


def normalize(row: dict[str, Any]) -> dict[str, str]:
    return {
        "source_record_id": str(row.get("identifier") or row.get("%Ref:ds.identifier") or ""),
        "title": str(row.get("title") or ""),
        "issued_date": iso_date(row.get("issued")),
        "released_date": iso_date(row.get("released")),
        "modified_date": iso_date(row.get("modified")),
        "next_update_date": iso_date(row.get("nextUpdateDate")),
        "landing_page": str(row.get("landingPage") or ""),
        "download_url": first_download_url(row),
        "publisher": publisher_name(row),
        "keywords": "; ".join(str(item) for item in row.get("keyword") or []),
        "description": str(row.get("description") or "").replace("\n", " ").strip(),
        "raw_json": json.dumps(row, ensure_ascii=False, sort_keys=True),
    }


def read_existing(path: Path) -> dict[str, dict[str, str]]:
    if not path.exists():
        return {}
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    return {row["source_record_id"]: row for row in rows if row.get("source_record_id")}


def write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDNAMES, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def month_key(row: dict[str, str], date_field: str) -> str | None:
    parsed = parse_date(row.get(date_field))
    return parsed.strftime("%Y-%m") if parsed else None


def monthly_average(rows: list[dict[str, str]], date_field: str, years: int) -> tuple[float, int, str, str]:
    cutoff = dt.date.today() - dt.timedelta(days=years * 365)
    months = Counter()
    for row in rows:
        parsed = parse_date(row.get(date_field))
        if parsed and parsed >= cutoff:
            months[parsed.strftime("%Y-%m")] += 1
    if not months:
        return 0.0, 0, "", ""
    ordered = sorted(months)
    average = sum(months.values()) / len(months)
    return average, sum(months.values()), ordered[0], ordered[-1]


def filter_since(rows: list[dict[str, str]], since: dt.date | None, date_field: str) -> list[dict[str, str]]:
    if since is None:
        return rows
    kept = []
    for row in rows:
        parsed = parse_date(row.get(date_field))
        if parsed and parsed >= since:
            kept.append(row)
    return kept


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch CMS Provider Data metadata into CSV.")
    parser.add_argument("--out", default="data/cms_provider_data.csv")
    parser.add_argument("--since", default="", help="YYYY-MM-DD; optional incremental filter")
    parser.add_argument(
        "--date-field",
        choices=["issued_date", "released_date", "modified_date"],
        default="released_date",
        help="date used for --since and cadence stats",
    )
    parser.add_argument("--history-years", type=int, default=20)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    out_path = Path(args.out)
    since = parse_date(args.since) if args.since else None

    raw_rows = http_json(API_URL)
    if not isinstance(raw_rows, list):
        raise RuntimeError("CMS API returned unexpected payload")

    fetched_rows = [normalize(row) for row in raw_rows if isinstance(row, dict)]
    rows_for_output = filter_since(fetched_rows, since, args.date_field)

    existing = read_existing(out_path)
    added = 0
    updated = 0
    for row in rows_for_output:
        old = existing.get(row["source_record_id"])
        if old is None:
            added += 1
        elif old != row:
            updated += 1
        existing[row["source_record_id"]] = row

    merged_rows = sorted(
        existing.values(),
        key=lambda row: (row.get(args.date_field) or "", row.get("title") or ""),
        reverse=True,
    )
    write_csv(out_path, merged_rows)

    average, counted, first_month, last_month = monthly_average(fetched_rows, args.date_field, args.history_years)
    print(f"CMS Provider Data API rows fetched: {len(fetched_rows)}")
    print(f"CSV rows written: {len(merged_rows)} ({added} added, {updated} updated)")
    print(f"Output: {out_path}")
    print(
        f"Average {args.date_field.replace('_', ' ')} posts/month over available "
        f"{args.history_years}-year window: {average:.2f} "
        f"({counted} records across {first_month or 'n/a'}..{last_month or 'n/a'})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
