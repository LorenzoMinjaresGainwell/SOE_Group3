from __future__ import annotations

import urllib.parse
from html.parser import HTMLParser
from typing import Any, Callable

from services.state_contracts.tabular import limit_records, normalize_awarded_record
from services.state_http import fetch_url
from services.state_normalization import clean_text

PAGE_URL = "https://www.doa.nc.gov/divisions/purchase-contract/statewide-term-contracts"
SOURCE_NOTE = "Official North Carolina DOA Statewide Term Contracts HTML table; rows expose title, beginning/ending dates and awarded vendors."
USER_AGENT = "soe-group3-nc-statewide-contracts/0.1"


class ContractTableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.in_cell = False
        self.cell_parts: list[str] = []
        self.cell_links: list[str] = []
        self.current_row: list[dict[str, Any]] | None = None
        self.rows: list[list[dict[str, Any]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "tr":
            self.current_row = []
        elif tag in {"td", "th"} and self.current_row is not None:
            self.in_cell = True
            self.cell_parts = []
            self.cell_links = []
        elif self.in_cell and tag in {"br", "li", "p", "div"}:
            self.cell_parts.append(" | ")
        elif self.in_cell and tag == "a":
            href = dict(attrs).get("href")
            if href:
                self.cell_links.append(urllib.parse.urljoin(PAGE_URL, href))

    def handle_data(self, data: str) -> None:
        if self.in_cell:
            self.cell_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"td", "th"} and self.in_cell and self.current_row is not None:
            self.current_row.append({"text": clean_text(" ".join(self.cell_parts), 2000), "links": self.cell_links[:]})
            self.in_cell = False
        elif tag == "tr" and self.current_row is not None:
            if self.current_row:
                self.rows.append(self.current_row)
            self.current_row = None


def fetch_contracts(*, vendor_terms: list[str], keywords: list[str], max_per_vendor: int, progress: Callable[[str], None] | None = None) -> list[dict[str, str]]:
    rows = fetch_rows()
    if progress:
        progress(f"NC statewide term contracts: parsed {len(rows)} official table rows")
    records: list[dict[str, str]] = []
    for row in rows:
        for vendor in split_vendors(row.get("Awarded Vendor(s)", "")):
            record = normalize_row(row, vendor_name=vendor, vendor_terms=vendor_terms, keywords=keywords)
            if record:
                records.append(record)
    return limit_records(records, max_per_vendor, vendor_terms)


def fetch_rows() -> list[dict[str, Any]]:
    result = fetch_url(PAGE_URL, headers={"Accept": "text/html,application/xhtml+xml,*/*"}, timeout=60, byte_limit=2_000_000, user_agent=USER_AGENT)
    result.raise_for_status()
    parser = ContractTableParser()
    parser.feed(result.body_text())
    headers = ["Title", "Beginning Date", "Ending Date", "Mandatory / Convenience", "Contract Manager", "Awarded Vendor(s)", "Related Content"]
    records: list[dict[str, Any]] = []
    for cells in parser.rows:
        texts = [cell["text"] for cell in cells]
        if texts[: len(headers)] == headers:
            continue
        if len(cells) < len(headers) or not cells[0]["text"]:
            continue
        row = {headers[index]: cells[index]["text"] for index in range(len(headers))}
        row["links"] = [url for cell in cells for url in cell["links"]]
        if contract_number(row["Title"]):
            records.append(row)
    return records


def normalize_row(row: dict[str, Any], *, vendor_name: str, vendor_terms: list[str], keywords: list[str]) -> dict[str, str]:
    number = contract_number(row.get("Title", ""))
    links = row.get("links") if isinstance(row.get("links"), list) else []
    return normalize_awarded_record(row, state="NC", source="North Carolina Statewide Term Contracts", source_key="nc_statewide_term_contracts", source_note=SOURCE_NOTE, source_url=PAGE_URL, vendor_terms=vendor_terms, keywords=keywords, contract_number=number, source_record_id=f"{number}-{vendor_name}", title=row.get("Title"), vendor_name=vendor_name, agency="North Carolina Department of Administration", start_date=row.get("Beginning Date"), end_date=row.get("Ending Date"), document_url=links[0] if links else PAGE_URL, document_type=f"NC {row.get('Mandatory / Convenience') or 'Statewide'} Term Contract", contract_record_type="master_agreement")


def contract_number(title: Any) -> str:
    text = clean_text(title, 500)
    return text.split(" - ", 1)[0].strip() if " - " in text else ""


def split_vendors(value: Any) -> list[str]:
    text = clean_text(value, 2000).strip(" |")
    if not text:
        return []
    vendors: list[str] = []
    for item in text.split("|"):
        vendor = clean_text(item, 180)
        if vendor and vendor not in vendors:
            vendors.append(vendor)
    return vendors
