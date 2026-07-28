from __future__ import annotations

import re
from typing import Any, Callable

from services.state_contracts.tabular import limit_records, normalize_awarded_record
from services.state_normalization import clean_text
from services.state_opportunities.md import PAGE_URL, fetch_bid_award_rows, first_link

SOURCE_NOTE = "Official Maryland DGS Open Bids and Contract Awards HTML table; only rows explicitly carrying Awarded status, vendor, and award dates are accepted."


def fetch_contracts(*, vendor_terms: list[str], keywords: list[str], max_per_vendor: int, progress: Callable[[str], None] | None = None) -> list[dict[str, str]]:
    rows = fetch_bid_award_rows()
    awarded = [row for row in rows if clean_text(row.get("Status")).lower() == "awarded"]
    if progress:
        progress(f"MD DGS contract awards: parsed {len(awarded)} awarded rows from {len(rows)} table rows")
    records = [normalize_row(row, vendor_terms=vendor_terms, keywords=keywords) for row in awarded]
    return limit_records([row for row in records if row], max_per_vendor, vendor_terms)


def normalize_row(row: dict[str, Any], *, vendor_terms: list[str], keywords: list[str]) -> dict[str, str]:
    if clean_text(row.get("Status")).lower() != "awarded":
        return {}
    link = first_link(row)
    bpo = clean_text(row.get("BPO No"), 240)
    match = re.search(r"\b0?\d{2,3}B\d{7}\b", bpo, re.IGNORECASE)
    number = match.group(0).upper() if match else re.sub(r"(?i)\.pdf.*$", "", bpo).strip(" -_")
    return normalize_awarded_record(row, state="MD", source="Maryland DGS Contract Awards", source_key="md_dgs_contract_awards", source_note=SOURCE_NOTE, source_url=PAGE_URL, vendor_terms=vendor_terms, keywords=keywords, contract_number=number, title=row.get("Description"), vendor_name=row.get("Vendor"), agency="Maryland Department of General Services", start_date=row.get("Award Start Date"), end_date=row.get("Award End Date"), document_url=link, document_type="Maryland Statewide Contract Award", contract_record_type="master_agreement")
