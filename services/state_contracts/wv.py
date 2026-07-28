from __future__ import annotations

from typing import Any, Callable

from services.state_contracts.html_table import parse_table_rows
from services.state_contracts.tabular import limit_records, normalize_awarded_record
from services.state_http import fetch_url
from services.state_normalization import clean_text

SOURCE_URL = "https://www.state.wv.us/admin/purchase/swc/"
SOURCE_NOTE = "Official West Virginia Purchasing Division Statewide Contract Index; the simple HTML table identifies awarded vendor and expiration date."


def fetch_contracts(*, vendor_terms: list[str], keywords: list[str], max_per_vendor: int, progress: Callable[[str], None] | None = None) -> list[dict[str, str]]:
    if max_per_vendor <= 0 or not (vendor_terms or keywords):
        return []
    rows = fetch_rows()
    records = [normalize_row(row, vendor_terms=vendor_terms, keywords=keywords) for row in rows]
    matched = [record for record in records if record]
    if progress:
        progress(f"WV statewide contracts: parsed {len(rows)} official current-contract rows; matched {len(matched)}")
    return limit_records(matched, max_per_vendor, vendor_terms)


def fetch_rows() -> list[dict[str, Any]]:
    result = fetch_url(SOURCE_URL, headers={"Accept": "text/html,*/*"}, timeout=30, byte_limit=2_000_000)
    result.raise_for_status()
    return parse_results(result.body_text())


def parse_results(markup: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for cells in parse_table_rows(markup, SOURCE_URL):
        if len(cells) < 9 or clean_text(cells[0]["text"]).lower() == "good or services":
            continue
        number = cells[1]["text"].split(" | ", 1)[0]
        links = cells[1]["links"]
        if number and cells[4]["text"] and cells[7]["text"]:
            rows.append({
                "title": cells[0]["text"], "contract_number": number, "vendor": cells[4]["text"],
                "mandatory": cells[5]["text"], "end_date": cells[7]["text"],
                "document_url": links[0] if links else SOURCE_URL,
            })
    return rows


def normalize_row(row: dict[str, Any], *, vendor_terms: list[str], keywords: list[str]) -> dict[str, str]:
    return normalize_awarded_record(
        row, state="WV", source="West Virginia Statewide Contract Index", source_key="wv_statewide_contracts",
        source_note=SOURCE_NOTE, source_url=SOURCE_URL, vendor_terms=vendor_terms, keywords=keywords,
        contract_number=row.get("contract_number"), title=row.get("title"), vendor_name=row.get("vendor"),
        agency="West Virginia Purchasing Division", end_date=row.get("end_date"), document_url=row.get("document_url"),
        document_type=f"WV {'Mandatory' if clean_text(row.get('mandatory')).upper() == 'M' else 'Statewide'} Contract",
        contract_record_type="master_agreement",
    )
