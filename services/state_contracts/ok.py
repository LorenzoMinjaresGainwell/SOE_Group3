from __future__ import annotations

import csv
import io
from typing import Any, Callable

from services.state_contracts.tabular import limit_records, normalize_awarded_record
from services.state_http import fetch_url

SOURCE_URL = "https://oklahoma.gov/omes/divisions/central-purchasing/solicitations/awarded-contracts.html"
CSV_URL = "https://oklahoma.gov/content/dam/ok/en/omes/documents/cp-awarded-contracts.csv"
SOURCE_NOTE = "Official Oklahoma OMES awarded-contract CSV linked by the Awarded Contracts page; it supplies event ID, awarded supplier, agency and award date but no contract end date."


def fetch_contracts(*, vendor_terms: list[str], keywords: list[str], max_per_vendor: int, progress: Callable[[str], None] | None = None) -> list[dict[str, str]]:
    if max_per_vendor <= 0 or not (vendor_terms or keywords):
        return []
    rows = fetch_rows()
    records = [normalize_row(row, vendor_terms=vendor_terms, keywords=keywords) for row in rows]
    matched = [record for record in records if record]
    if progress:
        progress(f"OK OMES awarded contracts: scanned {len(rows)} official post-award rows; matched {len(matched)} (end dates unavailable)")
    return limit_records(matched, max_per_vendor, vendor_terms)


def fetch_rows() -> list[dict[str, Any]]:
    result = fetch_url(CSV_URL, headers={"Accept": "text/csv,*/*"}, timeout=30, byte_limit=1_000_000)
    result.raise_for_status()
    return parse_csv(result.body)


def parse_csv(data: bytes | str) -> list[dict[str, str]]:
    text = data.decode("utf-8-sig", "replace") if isinstance(data, bytes) else data.lstrip("\ufeff")
    return [dict(row) for row in csv.DictReader(io.StringIO(text)) if row.get("EVENT #")]


def normalize_row(row: dict[str, Any], *, vendor_terms: list[str], keywords: list[str]) -> dict[str, str]:
    event = row.get("EVENT #")
    return normalize_awarded_record(
        row, state="OK", source="Oklahoma OMES Awarded Contracts", source_key="ok_omes_awarded_contracts",
        source_note=SOURCE_NOTE, source_url=SOURCE_URL, vendor_terms=vendor_terms, keywords=keywords,
        contract_number=event, source_record_id=event, title=f"Awarded contract {event}", vendor_name=row.get("AWARDED SUPPLIER"),
        agency=row.get("AGENCY NAME"), execution_date=row.get("AWARD DATE"), document_url=SOURCE_URL,
        document_type="Oklahoma Contract Award", contract_record_type="award",
    )
