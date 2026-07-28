from __future__ import annotations

from typing import Any, Callable

from services.state_contracts.html_table import parse_table_rows
from services.state_contracts.tabular import limit_records, normalize_awarded_record, unique_terms
from services.state_http import fetch_url
from services.state_normalization import clean_text

SOURCE_URL = "https://statecontracts.utah.gov/Home/Search"
RESULT_URL = "https://statecontracts.utah.gov/Home/GetResult"
SOURCE_NOTE = "Official Utah Division of Purchasing statewide cooperative contract directory; search results expose contract ID, contractor and expiration date."
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
        progress(f"UT cooperative contracts: searched {len(terms)} bounded terms; matched {len(records)} current contracts")
    return limit_records(records, max_per_vendor, vendor_terms)


def fetch_rows(term: str) -> list[dict[str, Any]]:
    result = fetch_url(RESULT_URL, method="POST", data={"keywords": term, "counties": ""}, headers={"Accept": "text/html,*/*"}, timeout=30, byte_limit=2_000_000)
    result.raise_for_status()
    return parse_results(result.body_text())


def parse_results(markup: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for cells in parse_table_rows(markup, RESULT_URL):
        if len(cells) < 5 or clean_text(cells[0]["text"]).lower() == "contract id":
            continue
        number, title, vendor, end_date = (cells[index]["text"] for index in range(4))
        links = cells[4]["links"]
        if number and title and vendor:
            rows.append({"contract_id": number, "title": title, "vendor": vendor, "end_date": end_date, "detail_url": links[0] if links else SOURCE_URL})
    return rows


def normalize_row(row: dict[str, Any], *, vendor_terms: list[str], keywords: list[str]) -> dict[str, str]:
    return normalize_awarded_record(
        row, state="UT", source="Utah State Cooperative Contracts", source_key="ut_state_contracts",
        source_note=SOURCE_NOTE, source_url=SOURCE_URL, vendor_terms=vendor_terms, keywords=keywords,
        contract_number=row.get("contract_id"), title=row.get("title"), vendor_name=row.get("vendor"),
        agency="Utah Division of Purchasing", end_date=row.get("end_date"), document_url=row.get("detail_url"),
        document_type="Utah Cooperative Contract", contract_record_type="master_agreement",
    )
