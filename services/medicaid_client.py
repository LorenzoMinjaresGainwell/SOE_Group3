from __future__ import annotations

import csv
import datetime as dt
import html
import json
import re
import time
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

API_URL = "https://data.medicaid.gov/api/1/metastore/schemas/dataset/items"
USER_AGENT = "soe-group3-medicaid-catalog/0.1"
DATE_FIELDS = ("issued_date", "released_date", "modified_date")
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


@dataclass(frozen=True)
class MedicaidCatalogResult:
    rows: list[dict[str, str]]
    endpoint: str
    raw_rows: int
    fetched_rows: int
    rows_after_filter: int
    skipped_without_id: int
    existing_rows: int
    rows_written: int
    added: int
    updated: int
    date_coverage: dict[str, str | int]
    monthly_cadence: dict[str, str | int | float]


class MedicaidCatalogClient:
    def __init__(self, timeout: int = 45, retries: int = 3) -> None:
        self.timeout = timeout
        self.retries = retries

    def get_json(self, url: str = API_URL) -> Any:
        if "api.sam.gov" in url.lower():
            raise RuntimeError("Refusing to call api.sam.gov from Medicaid catalog client")
        request = urllib.request.Request(
            url,
            headers={"Accept": "application/json", "User-Agent": USER_AGENT},
        )
        last_error: Exception | None = None
        for attempt in range(self.retries):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    return json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                last_error = exc
                if exc.code not in {429, 500, 502, 503, 504}:
                    break
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                last_error = exc
            time.sleep(1 + attempt)
        raise RuntimeError(f"Medicaid.gov catalog request failed for {url}: {last_error}")


def fetch_medicaid_catalog_rows(client: MedicaidCatalogClient | None = None) -> tuple[list[dict[str, str]], int, int]:
    client = client or MedicaidCatalogClient()
    payload = client.get_json(API_URL)
    if not isinstance(payload, list):
        raise RuntimeError("Medicaid.gov catalog returned unexpected payload")

    normalized = [normalize_dataset(row) for row in payload if isinstance(row, dict)]
    rows = [row for row in normalized if row.get("source_record_id")]
    skipped_without_id = len(normalized) - len(rows)
    return rows, len(payload), skipped_without_id


def build_medicaid_catalog(
    out_path: Path,
    since: dt.date | None = None,
    date_field: str = "modified_date",
    history_years: int = 20,
    dry_run: bool = False,
    client: MedicaidCatalogClient | None = None,
    progress: Callable[[str], None] | None = None,
) -> MedicaidCatalogResult:
    if date_field not in DATE_FIELDS:
        raise ValueError(f"Unsupported date field: {date_field}")

    fetched_rows, raw_rows, skipped_without_id = fetch_medicaid_catalog_rows(client)
    emit(progress, f"fetched {len(fetched_rows)} Medicaid.gov catalog rows")
    rows_after_filter = filter_since(fetched_rows, since, date_field)
    existing = read_existing(out_path)
    merged_rows, added, updated = upsert_rows(existing, rows_after_filter)
    sorted_rows = sort_rows(merged_rows, date_field)

    if not dry_run:
        write_csv(out_path, sorted_rows)

    return MedicaidCatalogResult(
        rows=sorted_rows,
        endpoint=API_URL,
        raw_rows=raw_rows,
        fetched_rows=len(fetched_rows),
        rows_after_filter=len(rows_after_filter),
        skipped_without_id=skipped_without_id,
        existing_rows=len(existing),
        rows_written=len(sorted_rows),
        added=added,
        updated=updated,
        date_coverage=date_coverage(fetched_rows, date_field),
        monthly_cadence=monthly_cadence(fetched_rows, date_field, history_years),
    )


def normalize_dataset(row: dict[str, Any]) -> dict[str, str]:
    return {
        "source_record_id": clean_text(row.get("identifier") or row.get("%Ref:ds.identifier")),
        "title": clean_text(row.get("title")),
        "issued_date": iso_date(row.get("issued")),
        "released_date": iso_date(row.get("released")),
        "modified_date": iso_date(row.get("modified")),
        "next_update_date": iso_date(row.get("nextUpdateDate")),
        "landing_page": clean_text(row.get("landingPage")),
        "download_url": first_distribution_url(row),
        "publisher": publisher_name(row.get("publisher")),
        "keywords": "; ".join(keyword_values(row.get("keyword"))),
        "description": clean_text(row.get("description")),
        "raw_json": compact_json(row),
    }


def first_distribution_url(row: dict[str, Any]) -> str:
    for distribution in as_list(row.get("distribution")):
        if not isinstance(distribution, dict):
            continue
        url = first_text(distribution.get("downloadURL")) or first_text(distribution.get("accessURL"))
        if url:
            return url
    return ""


def publisher_name(value: Any) -> str:
    if isinstance(value, dict):
        return clean_text(value.get("name"))
    return first_text(value)


def keyword_values(value: Any) -> list[str]:
    values: list[str] = []
    for item in as_list(value):
        if isinstance(item, dict):
            text = clean_text(item.get("name") or item.get("title") or item.get("value"))
        else:
            text = clean_text(item)
        if text:
            values.append(text)
    return dedupe_preserve_order(values)


def read_existing(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return [normalize_csv_row(row) for row in csv.DictReader(handle)]


def normalize_csv_row(row: dict[str, Any]) -> dict[str, str]:
    return {field: str(row.get(field) or "") for field in FIELDNAMES}


def upsert_rows(existing: list[dict[str, str]], incoming: list[dict[str, str]]) -> tuple[list[dict[str, str]], int, int]:
    by_id = {row["source_record_id"]: row for row in existing if row.get("source_record_id")}
    added = 0
    updated = 0
    for row in incoming:
        source_record_id = row.get("source_record_id")
        if not source_record_id:
            continue
        old = by_id.get(source_record_id)
        if old is None:
            added += 1
        elif any((old.get(field) or "") != row.get(field, "") for field in FIELDNAMES):
            updated += 1
        by_id[source_record_id] = row
    return list(by_id.values()), added, updated


def write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDNAMES, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def filter_since(rows: list[dict[str, str]], since: dt.date | None, date_field: str) -> list[dict[str, str]]:
    if since is None:
        return rows
    return [row for row in rows if (parse_date(row.get(date_field)) or dt.date.min) >= since]


def sort_rows(rows: list[dict[str, str]], date_field: str) -> list[dict[str, str]]:
    def key(row: dict[str, str]) -> tuple[int, str]:
        parsed = parse_date(row.get(date_field))
        ordinal = parsed.toordinal() if parsed else 0
        return (-ordinal, row.get("title", "").casefold())

    return sorted(rows, key=key)


def date_coverage(rows: list[dict[str, str]], date_field: str) -> dict[str, str | int]:
    dates = [parsed for row in rows if (parsed := parse_date(row.get(date_field)))]
    return {
        "date_field": date_field,
        "records_with_date": len(dates),
        "records_missing_date": len(rows) - len(dates),
        "min_date": min(dates).isoformat() if dates else "",
        "max_date": max(dates).isoformat() if dates else "",
    }


def monthly_cadence(rows: list[dict[str, str]], date_field: str, history_years: int) -> dict[str, str | int | float]:
    cutoff = dt.date.today() - dt.timedelta(days=max(history_years, 0) * 365)
    months: Counter[str] = Counter()
    for row in rows:
        parsed = parse_date(row.get(date_field))
        if parsed and parsed >= cutoff:
            months[parsed.strftime("%Y-%m")] += 1
    ordered = sorted(months)
    average = sum(months.values()) / len(months) if months else 0.0
    return {
        "date_field": date_field,
        "history_years": history_years,
        "records_in_window": sum(months.values()),
        "active_months": len(months),
        "average_records_per_active_month": round(average, 2),
        "first_month": ordered[0] if ordered else "",
        "last_month": ordered[-1] if ordered else "",
    }


def parse_date(value: Any) -> dt.date | None:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    text = text.replace("Z", "+00:00")
    for fmt in (
        "%Y-%m-%d",
        "%m/%d/%Y",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S.%f%z",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%S.%f",
    ):
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


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = first_text(value) if isinstance(value, list) else str(value)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def first_text(value: Any) -> str:
    if isinstance(value, list):
        for item in value:
            text = first_text(item)
            if text:
                return text
        return ""
    if isinstance(value, dict):
        return clean_text(value.get("name") or value.get("title") or value.get("value"))
    return clean_text(value)


def as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def dedupe_preserve_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for value in values:
        key = value.casefold()
        if key in seen:
            continue
        seen.add(key)
        output.append(value)
    return output


def compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def emit(progress: Callable[[str], None] | None, message: str) -> None:
    if progress:
        progress(message)
