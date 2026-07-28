from __future__ import annotations

import datetime as dt
import json
import urllib.parse
from typing import Any, Callable

from services.state_contracts.tabular import normalized_end_date_fields
from services.state_http import fetch_url
from services.state_normalization import amount_string, clean_text, compact_raw_json, iso_date, keyword_hits, stable_id

PAGE_URL = "https://secure.in.gov/apps/idoa/contractsearch/"
API_URL = PAGE_URL + "api/contracts/search"
SOURCE_NAME = "Indiana IDOA Public Contract Search"
USER_AGENT = "soe-group3-in-contracts/0.1"


def fetch_contracts(*, vendor_terms: list[str], keywords: list[str], max_per_vendor: int, progress: Callable[[str], None] | None = None) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    seen: set[str] = set()
    for term in unique_terms(vendor_terms):
        # Results are history-first and the API exposes at most 100 rows per page; one
        # bounded request is needed so current amendments are not hidden by old history.
        rows = search_contracts(term, 100)
        accepted = 0
        for row in rows:
            record = normalize_row(row, vendor_terms=[term], keywords=keywords)
            if not record or record["id"] in seen or accepted >= max(1, max_per_vendor):
                continue
            seen.add(record["id"])
            records.append(record)
            accepted += 1
        emit(progress, f"IN IDOA contracts: vendor={term}: scanned {len(rows)}, normalized {accepted} current/post-award records")
    return sorted(records, key=lambda r: (int(r["relevance_score"]), r["end_date"]), reverse=True)


def search_contracts(vendor: str, limit: int) -> list[dict[str, Any]]:
    payload = json.dumps({"businessName": vendor, "pageNumber": 1, "pageSize": min(100, max(1, limit)), "contractTypeFlags": 0}).encode()
    result = fetch_url(API_URL, method="POST", data=payload, headers={"Content-Type": "application/json", "X-Requested-With": "XMLHttpRequest", "Referer": PAGE_URL}, timeout=30, byte_limit=2_000_000, user_agent=USER_AGENT)
    result.raise_for_status()
    body = json.loads(result.body_text())
    return body.get("results", []) if isinstance(body, dict) else []


def normalize_row(row: dict[str, Any], *, vendor_terms: list[str], keywords: list[str]) -> dict[str, str] | None:
    contract_number = clean_text(row.get("id"), 120)
    vendor = clean_text(row.get("vendorName"), 180)
    end_date, months, end_signal, expired = normalized_end_date_fields(row.get("endDate"))
    document_url = clean_text(row.get("pdfUrl"), 1000)
    if not contract_number or not vendor or end_signal == "Unknown end date" or not document_url or expired:
        return None
    amendment = clean_text(row.get("amendment"), 30)
    source_id = f"{contract_number}-{amendment or '0'}"
    action = clean_text(row.get("actionType") or "Contract", 80)
    agency = clean_text(row.get("agencyName"), 180)
    title = clean_text(f"{agency} {action}", 500)
    text = " ".join([contract_number, vendor, agency, action])
    vendor_hits = keyword_hits(vendor, vendor_terms)
    matched = keyword_hits(text, keywords)
    date_score = 25 if months is not None and months <= 18 else 18 if months is not None and months <= 36 else 8 if months is not None else 0
    score = min(100, 35 + len(matched) * 8 + date_score)
    raw = {"source_key": "in_idoa_contract_search", "source_note": "Official IDOA public contract-search JSON API; expired historical rows are rejected.", "row": row}
    return {
        "id": stable_id("IN", source_id, vendor, prefix="in-idoa-contract"), "state": "IN", "source": SOURCE_NAME,
        "source_record_id": source_id, "parent_id": contract_number, "contract_record_type": "amendment" if action.lower() == "amendment" else "parent_contract",
        "vendor_name": vendor, "vendor_query": ";".join(vendor_hits), "agency": agency, "contract_number": contract_number, "title": title,
        "amount": amount_string(row.get("amount")), "execution_date": "", "start_date": iso_date(row.get("startDate")), "end_date": end_date,
        "months_to_end": "" if months is None else str(months), "recompete_signal": end_signal, "document_type": f"Indiana {action}", "document_url": document_url,
        "source_url": PAGE_URL, "matched_keywords": ";".join(matched), "relevance_score": str(score), "raw_json": compact_raw_json(raw, limit=7000), "last_checked_at": now_iso(),
    }


def unique_terms(terms: list[str]) -> list[str]:
    return list(dict.fromkeys(clean_text(t, 100) for t in terms if clean_text(t)))


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def emit(progress: Callable[[str], None] | None, message: str) -> None:
    if progress:
        progress(message)
