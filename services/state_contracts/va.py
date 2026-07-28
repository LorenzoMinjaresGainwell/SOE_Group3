from __future__ import annotations

import urllib.parse
from typing import Any, Callable

from services.state_contracts.tabular import limit_records, normalize_awarded_record, unique_terms
from services.state_http import fetch_json
from services.state_normalization import clean_text

RESOURCE_ID = "76f6831d-fac7-4c1f-8313-cc7ff238ddca"
API_URL = "https://data.virginia.gov/api/action/datastore_search"
CATALOG_URL = "https://data.virginia.gov/dataset/e6f7f6ba-23a1-4423-93cd-31697b89a26f"
SOURCE_NOTE = "Official Virginia Open Data eVA Procurement Data 2026 datastore; accepted rows are issued purchase-order lines, a post-award transaction record, not opportunities."


def fetch_contracts(*, vendor_terms: list[str], keywords: list[str], max_per_vendor: int, progress: Callable[[str], None] | None = None) -> list[dict[str, str]]:
    terms = unique_terms(vendor_terms + keywords)
    rows: list[dict[str, Any]] = []
    for term in terms:
        params = {"resource_id": RESOURCE_ID, "limit": str(max(20, max_per_vendor * 5)), "q": term}
        data = fetch_json(API_URL + "?" + urllib.parse.urlencode(params), timeout=60, byte_limit=3_000_000)
        batch = (data.get("result") or {}).get("records") if isinstance(data, dict) else []
        rows.extend(row for row in batch or [] if isinstance(row, dict))
    if progress:
        progress(f"VA eVA procurement datastore: parsed {len(rows)} matching purchase-order lines")
    records = [normalize_row(row, vendor_terms=vendor_terms, keywords=keywords) for row in rows]
    return limit_records([row for row in records if row], max_per_vendor, vendor_terms)


def normalize_row(row: dict[str, Any], *, vendor_terms: list[str], keywords: list[str]) -> dict[str, str]:
    order = clean_text(row.get("Order #"), 180)
    line = clean_text(row.get("Order Line Number"), 180)
    number = clean_text(row.get("Contract Number") or order, 180)
    if not order or not line or not number:
        return {}
    source_record_id = f"{order}-{number}-{line}"
    return normalize_awarded_record(row, state="VA", source="Virginia eVA Procurement Data", source_key="va_eva_purchase_orders_2026", source_note=SOURCE_NOTE, source_url=CATALOG_URL, vendor_terms=vendor_terms, keywords=keywords, contract_number=number, source_record_id=source_record_id, title=row.get("Item Description") or row.get("NIGP Description") or number, vendor_name=row.get("Vendor Name"), agency=row.get("Entity Description"), amount=row.get("Line Total"), execution_date=row.get("Ordered Date"), start_date=row.get("Ordered Date"), document_url=CATALOG_URL, document_type="eVA Purchase Order", contract_record_type="purchase_order")
