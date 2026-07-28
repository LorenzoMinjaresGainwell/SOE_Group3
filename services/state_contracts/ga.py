from __future__ import annotations

import urllib.parse
from typing import Any, Callable

from services.state_contracts.tabular import limit_records, normalize_awarded_record, unique_terms
from services.state_opportunities.ga import BASE_URL, EVENT_SEARCH_URL, GeorgiaGprClient, event_detail_url, event_search_payload, valid_rows

SOURCE_NOTE = "Official Georgia Procurement Registry public DataTables endpoint queried with eventStatus=AWARD; only rows explicitly returned as Awarded are accepted. Awarded vendor and term are not exposed in this simple public result."


def fetch_contracts(*, vendor_terms: list[str], keywords: list[str], max_per_vendor: int, progress: Callable[[str], None] | None = None) -> list[dict[str, str]]:
    client = GeorgiaGprClient()
    rows: list[dict[str, Any]] = []
    for term in unique_terms(vendor_terms + keywords):
        batch = fetch_awarded_rows(client, term=term, limit=max(10, max_per_vendor * 3))
        rows.extend(batch)
        if progress:
            progress(f"GA GPR awarded search {term!r}: parsed {len(batch)} post-award rows")
    records = [normalize_row(row, vendor_terms=vendor_terms, keywords=keywords) for row in rows]
    return limit_records([row for row in records if row], max_per_vendor, vendor_terms)


def fetch_awarded_rows(client: GeorgiaGprClient, *, term: str, limit: int) -> list[dict[str, Any]]:
    client.prime()
    payload = event_search_payload(start=0, length=limit)
    payload["eventStatus"] = "AWARD"
    payload["eventIdTitle"] = term
    payload["order[0][column]"] = "4"
    payload["order[0][dir]"] = "desc"
    text, _ = client._request_text(EVENT_SEARCH_URL, data=urllib.parse.urlencode(payload).encode("utf-8"), referer=BASE_URL)
    import json
    return valid_rows(json.loads(text).get("data"))


def normalize_row(row: dict[str, Any], *, vendor_terms: list[str], keywords: list[str]) -> dict[str, str]:
    if str(row.get("status", "")).lower() != "awarded":
        return {}
    number = row.get("esourceNumber") or row.get("esourceNumberKey")
    return normalize_awarded_record(row, state="GA", source="Georgia Procurement Registry Awarded Events", source_key="ga_gpr_awarded_events", source_note=SOURCE_NOTE, source_url=BASE_URL, vendor_terms=vendor_terms, keywords=keywords, contract_number=number, title=row.get("title"), agency=row.get("agencyName"), execution_date=row.get("awardDate"), start_date=row.get("awardDate"), document_url=event_detail_url(row), document_type="Georgia GPR Awarded Event", contract_record_type="award")
