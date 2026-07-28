from __future__ import annotations

import urllib.parse
from typing import Any, Callable

from services.state_contracts.tabular import limit_records, normalize_awarded_record, unique_terms
from services.state_http import fetch_json

SOURCE_URL = "https://contracts.ocp.dc.gov/contracts/search"
API_URL = "https://contracts.ocp.dc.gov/api/contracts/search"
SOURCE_NOTE = "Official DC OCP Contracts and Procurement Transparency Portal contract API; contract results expose supplier, award/start/end dates, agency and amount."
MAX_QUERIES = 12


def fetch_contracts(*, vendor_terms: list[str], keywords: list[str], max_per_vendor: int, progress: Callable[[str], None] | None = None) -> list[dict[str, str]]:
    if max_per_vendor <= 0:
        return []
    terms = unique_terms(vendor_terms + keywords)[:MAX_QUERIES]
    records: list[dict[str, str]] = []
    for term in terms:
        for row in fetch_rows(term):
            record = normalize_row(row, vendor_terms=vendor_terms, keywords=keywords)
            if record:
                records.append(record)
    if progress:
        progress(f"DC OCP contracts: searched {len(terms)} bounded terms; matched {len(records)} current contracts")
    return limit_records(records, max_per_vendor, vendor_terms)


def fetch_rows(term: str) -> list[dict[str, Any]]:
    payload = {"FilterBy": [{"name": "Keyword", "value": term}], "OrderBy": []}
    data = fetch_json(API_URL, method="POST", json_data=payload, timeout=30, byte_limit=2_000_000)
    rows = data.get("results", []) if isinstance(data, dict) else []
    return [row for row in rows if isinstance(row, dict)]


def normalize_row(row: dict[str, Any], *, vendor_terms: list[str], keywords: list[str]) -> dict[str, str]:
    agencies = row.get("agencyNames")
    agency = "; ".join(str(value) for value in agencies) if isinstance(agencies, list) else agencies
    source_id = row.get("id")
    detail_url = "https://contracts.ocp.dc.gov/contracts/details" + ("?" + urllib.parse.urlencode({"id": source_id}) if source_id else "")
    return normalize_awarded_record(
        row, state="DC", source="DC OCP Contract Transparency Portal", source_key="dc_ocp_contracts",
        source_note=SOURCE_NOTE, source_url=SOURCE_URL, vendor_terms=vendor_terms, keywords=keywords,
        contract_number=row.get("contractNumber"), source_record_id=source_id or row.get("contractNumber"),
        title=row.get("title"), vendor_name=row.get("vendor"), agency=agency, amount=row.get("contractAmount"),
        execution_date=row.get("awardDate"), start_date=row.get("startDate"), end_date=row.get("endDate"),
        document_url=detail_url, document_type="DC Executed Contract", contract_record_type="contract_period",
    )
