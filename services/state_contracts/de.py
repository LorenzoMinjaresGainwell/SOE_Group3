from __future__ import annotations

import urllib.parse
from typing import Any, Callable

from services.state_contracts.tabular import limit_records, normalize_awarded_record
from services.state_http import fetch_json
from services.state_normalization import clean_text

DATASET_ID = "h9bb-iyu6"
SOURCE_URL = f"https://data.delaware.gov/resource/{DATASET_ID}.json"
CATALOG_URL = f"https://data.delaware.gov/d/{DATASET_ID}"
SOURCE_NOTE = "Official Delaware Open Data Awarded Vendor Contact Info dataset; rows explicitly mark awarded vendors and expose contract detail links and expiration dates."


def fetch_contracts(*, vendor_terms: list[str], keywords: list[str], max_per_vendor: int, progress: Callable[[str], None] | None = None) -> list[dict[str, str]]:
    rows = fetch_rows()
    if progress:
        progress(f"DE awarded vendor dataset: scanned {len(rows)} rows")
    records = [normalize_row(row, vendor_terms=vendor_terms, keywords=keywords) for row in rows]
    return limit_records([row for row in records if row], max_per_vendor, vendor_terms)


def fetch_rows() -> list[dict[str, Any]]:
    url = SOURCE_URL + "?" + urllib.parse.urlencode({"$limit": "5000", "$where": "awarded_vendor='Y'", "$order": "expiredate DESC"})
    data = fetch_json(url, timeout=60, byte_limit=5_000_000)
    return [row for row in data if isinstance(row, dict)]


def normalize_row(row: dict[str, Any], *, vendor_terms: list[str], keywords: list[str]) -> dict[str, str]:
    if clean_text(row.get("awarded_vendor")).upper() != "Y":
        return {}
    detail = row.get("contracturl") or {}
    url = clean_text(detail.get("url") if isinstance(detail, dict) else detail, 500)
    detail_id = url.rstrip("/").rsplit("/", 1)[-1] if url else ""
    return normalize_awarded_record(row, state="DE", source="Delaware Awarded Vendor Contact Info", source_key="de_awarded_vendor_contacts", source_note=SOURCE_NOTE, source_url=CATALOG_URL, vendor_terms=vendor_terms, keywords=keywords, contract_number=detail_id, source_record_id=detail_id or f"{row.get('contracttitle', '')}-{row.get('companyname', '')}", title=row.get("contracttitle"), vendor_name=row.get("companyname"), end_date=row.get("expiredate"), document_url=url, document_type="Delaware Awarded Contract", contract_record_type="award")
