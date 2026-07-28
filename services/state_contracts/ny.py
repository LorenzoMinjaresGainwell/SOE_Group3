from __future__ import annotations

import urllib.parse
from typing import Any, Callable

from services.state_contracts.tabular import limit_records, normalize_awarded_record
from services.state_http import fetch_json
from services.state_normalization import clean_text

DATASET_ID = "cfjm-ii27"
SOURCE_URL = f"https://data.ny.gov/resource/{DATASET_ID}.json"
CATALOG_URL = f"https://data.ny.gov/d/{DATASET_ID}"
SOURCE_NOTE = "Official NY Open Data State University Construction Fund contracts dataset; only Active, Assigned to Vendor, or In Guarantee awarded rows with vendor and end date are accepted."


def fetch_contracts(*, vendor_terms: list[str], keywords: list[str], max_per_vendor: int, progress: Callable[[str], None] | None = None) -> list[dict[str, str]]:
    rows = fetch_rows()
    if progress:
        progress(f"NY SUCF contracts: scanned {len(rows)} current awarded rows")
    records = [normalize_row(row, vendor_terms=vendor_terms, keywords=keywords) for row in rows]
    return limit_records([row for row in records if row], max_per_vendor, vendor_terms)


def fetch_rows() -> list[dict[str, Any]]:
    params = {"$limit": "5000", "$where": "status in ('Active','Assigned to Vendor','In Guarantee') AND vendor_name is not null AND contract_end_date is not null", "$order": "contract_award_date DESC"}
    data = fetch_json(SOURCE_URL + "?" + urllib.parse.urlencode(params), timeout=60, byte_limit=8_000_000)
    return [row for row in data if isinstance(row, dict)]


def normalize_row(row: dict[str, Any], *, vendor_terms: list[str], keywords: list[str]) -> dict[str, str]:
    status = clean_text(row.get("status"), 80).lower()
    number = clean_text(row.get("contract_number"), 180)
    project_number = clean_text(row.get("project_number"), 180)
    vendor = clean_text(row.get("vendor_name"), 180)
    if status not in {"active", "assigned to vendor", "in guarantee"} or not number or not project_number or not vendor or not row.get("contract_end_date"):
        return {}
    return normalize_awarded_record(row, state="NY", source="New York SUCF Contracts", source_key="ny_sucf_contracts", source_note=SOURCE_NOTE, source_url=CATALOG_URL, vendor_terms=vendor_terms, keywords=keywords, contract_number=number, source_record_id=f"{number}-{project_number}", title=row.get("title"), vendor_name=vendor, agency="State University Construction Fund", amount=row.get("contract_amount"), execution_date=row.get("contract_award_date"), start_date=row.get("contract_award_date"), end_date=row.get("contract_end_date"), document_url=CATALOG_URL, document_type=row.get("award_type") or "New York Authority Contract", contract_record_type="award")
