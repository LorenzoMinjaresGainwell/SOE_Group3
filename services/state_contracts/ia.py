from __future__ import annotations

import datetime as dt
import json
import re
import urllib.parse
from typing import Any, Callable

from services.state_contracts.tabular import limit_records, normalize_awarded_record, unique_terms
from services.state_http import fetch_url
from services.state_normalization import clean_text

PAGE_URL = "https://bidopportunities.iowa.gov/Home/AwardedContracts"
API_URL = "https://bidopportunities.iowa.gov/Home/DT_AwardedContractsSearch"
SOURCE_NOTE = (
    "Official Iowa DAS Awarded Contracts DataTables endpoint; only rows explicitly marked "
    "active with stable IDs are accepted, and expired rows are rejected during normalization."
)
USER_AGENT = "soe-group3-ia-contracts/0.1"
MAX_PAGE_SIZE = 100
MAX_QUERY_COUNT = 40
_DOTNET_DATE = re.compile(r"^/Date\((-?\d+)(?:[+-]\d{4})?\)/$")


def fetch_contracts(
    *,
    vendor_terms: list[str],
    keywords: list[str],
    max_per_vendor: int,
    progress: Callable[[str], None] | None = None,
) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    # The official endpoint searches vendor, contract, product/service,
    # description, and keyword fields with enteredSearchText.
    for term in unique_terms([*vendor_terms, *keywords])[:MAX_QUERY_COUNT]:
        try:
            rows = fetch_rows(term, limit=max(10, max_per_vendor * 3))
        except (RuntimeError, json.JSONDecodeError) as exc:
            if progress:
                detail = clean_text(exc, 300)
                progress(f"IA DAS awarded contracts: query={term!r}: skipped after {detail}")
            continue
        if progress:
            progress(f"IA DAS awarded contracts: query={term!r}: scanned {len(rows)} active-result rows")
        records.extend(
            record
            for row in rows
            if (record := normalize_row(row, vendor_terms=vendor_terms, keywords=keywords))
        )
    return limit_records(records, max_per_vendor, vendor_terms)


def fetch_rows(search_text: str, *, limit: int) -> list[dict[str, Any]]:
    page_size = min(MAX_PAGE_SIZE, max(1, int(limit)))
    query = urllib.parse.urlencode(
        {
            "vendorName": "",
            "enteredSearchText": clean_text(search_text, 200),
            "sEcho": "1",
            "iDisplayStart": "0",
            "iDisplayLength": str(page_size),
        }
    )
    result = fetch_url(
        f"{API_URL}?{query}",
        headers={"Accept": "application/json", "Referer": PAGE_URL},
        timeout=30,
        byte_limit=2_000_000,
        user_agent=USER_AGENT,
    )
    result.raise_for_status()
    payload = json.loads(result.body_text())
    rows = payload.get("aaData", []) if isinstance(payload, dict) else []
    return [row for row in rows[:page_size] if isinstance(row, dict)]


def normalize_row(
    row: dict[str, Any], *, vendor_terms: list[str], keywords: list[str]
) -> dict[str, str]:
    if row.get("IsActive") is not True:
        return {}
    record_id = clean_text(row.get("ID"), 180)
    number = clean_text(row.get("Number"), 180)
    if not record_id or not number:
        return {}
    return normalize_awarded_record(
        row,
        state="IA",
        source="Iowa DAS Awarded Contracts",
        source_key="ia_das_awarded_contracts",
        source_note=SOURCE_NOTE,
        source_url=PAGE_URL,
        vendor_terms=vendor_terms,
        keywords=keywords,
        contract_number=number,
        source_record_id=record_id,
        title=row.get("ProductService") or row.get("Description") or number,
        vendor_name=row.get("VendorName"),
        agency="Iowa Department of Administrative Services",
        execution_date=dotnet_date(row.get("EffectiveDate")),
        start_date=dotnet_date(row.get("EffectiveDate")),
        end_date=dotnet_date(row.get("ExpirationDate")),
        document_url=f"https://bidopportunities.iowa.gov/Home/ContractInfo?contractId={urllib.parse.quote(record_id)}",
        document_type="Iowa DAS Awarded Contract",
        contract_record_type="award",
    )


def dotnet_date(value: Any) -> str:
    match = _DOTNET_DATE.fullmatch(clean_text(value, 80))
    if not match:
        return ""
    try:
        return dt.datetime.fromtimestamp(int(match.group(1)) / 1000, tz=dt.timezone.utc).date().isoformat()
    except (OverflowError, OSError, ValueError):
        return ""
