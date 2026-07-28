from __future__ import annotations

import datetime as dt
import json
import urllib.parse
from typing import Any, Callable

from services.state_contracts.tabular import normalized_end_date_fields, record_sort_key
from services.state_http import fetch_url
from services.state_normalization import amount_string, clean_text, compact_raw_json, iso_date, keyword_hits, stable_id

DATASET_ID = "6fx9-ncas"
PAGE_URL = f"https://data.wa.gov/d/{DATASET_ID}"
API_URL = f"https://data.wa.gov/resource/{DATASET_ID}.json"
SOURCE_NAME = "Washington OFM Agency Contracts Fiscal Year 2025"
USER_AGENT = "soe-group3-wa-contracts/0.1"


def fetch_contracts(*, vendor_terms: list[str], keywords: list[str], max_per_vendor: int, progress: Callable[[str], None] | None = None) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []; seen: set[str] = set()
    for term in unique_terms(vendor_terms):
        rows = query_rows(term, max(5, max_per_vendor * 4))
        accepted = 0
        for row in rows:
            record = normalize_row(row, vendor_terms=[term], keywords=keywords)
            if not record or record["id"] in seen or accepted >= max(1, max_per_vendor): continue
            seen.add(record["id"]); records.append(record); accepted += 1
        emit(progress, f"WA OFM FY2025 agency contracts: vendor={term}: scanned {len(rows)}, normalized {accepted} current records")
    return sorted(records, key=record_sort_key, reverse=True)


def query_rows(vendor: str, limit: int) -> list[dict[str, Any]]:
    escaped = vendor.lower().replace("'", "''")
    query = urllib.parse.urlencode({"$limit": min(100, max(1, limit)), "$where": f"lower(contractor_name_search_for) like '%{escaped}%'", "$order": "contract_effective_end_date DESC"})
    result = fetch_url(f"{API_URL}?{query}", headers={"Accept": "application/json", "Referer": PAGE_URL}, timeout=30, byte_limit=2_000_000, user_agent=USER_AGENT)
    result.raise_for_status(); body = json.loads(result.body_text())
    return body if isinstance(body, list) else []


def normalize_row(row: dict[str, Any], *, vendor_terms: list[str], keywords: list[str]) -> dict[str, str] | None:
    number = clean_text(row.get("agency_contract_no"), 120); amendment = clean_text(row.get("agency_contract_amendment"), 120)
    vendor = clean_text(row.get("contractor_name_search_for"), 180); agency = clean_text(row.get("agency_number_agency_name"), 180)
    title = clean_text(row.get("purpose_of_the_contract_1") or row.get("purpose_of_the_contract"), 500)
    end_date, months, end_signal, expired = normalized_end_date_fields(row.get("period_of_performance_end") or row.get("contract_effective_end_date"))
    if not number or not vendor or not title or end_signal == "Unknown end date" or expired: return None
    source_id = f"{number}-{amendment or number}-{agency}"; text = " ".join([number, amendment, vendor, agency, title, clean_text(row.get("procurement_type"))])
    vendor_hits = keyword_hits(vendor, vendor_terms); matched = keyword_hits(text, keywords)
    date_score = 25 if months is not None and months <= 18 else 18 if months is not None and months <= 36 else 8 if months is not None else 0
    score = min(100, 35 + len(matched) * 8 + date_score)
    raw = {"source_key": "wa_ofm_agency_contracts_fy2025", "source_note": "Official OFM post-award agency-contract dataset; rows lacking a parseable non-expired end date are rejected.", "row": row}
    return {"id": stable_id("WA", source_id, vendor, prefix="wa-ofm-contract"), "state": "WA", "source": SOURCE_NAME, "source_record_id": source_id, "parent_id": number,
        "contract_record_type": "amendment" if amendment and amendment != number else "parent_contract", "vendor_name": vendor, "vendor_query": ";".join(vendor_hits), "agency": agency,
        "contract_number": amendment or number, "title": title, "amount": amount_string(row.get("cost_of_contract")), "execution_date": "", "start_date": iso_date(row.get("period_of_performance_start") or row.get("contract_effective_start")),
        "end_date": end_date, "months_to_end": "" if months is None else str(months), "recompete_signal": end_signal,
        "document_type": clean_text(row.get("procurement_type") or "Washington Agency Contract", 160), "document_url": PAGE_URL, "source_url": API_URL,
        "matched_keywords": ";".join(matched), "relevance_score": str(score), "raw_json": compact_raw_json(raw, limit=7000), "last_checked_at": now_iso()}


def unique_terms(terms: list[str]) -> list[str]:
    return list(dict.fromkeys(clean_text(t, 100) for t in terms if clean_text(t)))


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def emit(progress: Callable[[str], None] | None, message: str) -> None:
    if progress: progress(message)
