from __future__ import annotations

import csv
import datetime as dt
import io
import json
from typing import Any, Callable

from services.state_http import fetch_url
from services.state_contracts.keyword_context import useful_keyword_match as keyword_context_match
from services.state_normalization import amount_string, clean_text, iso_date, keyword_hits, months_until, stable_id, term_matches

DASHBOARD_PAGE_URL = "https://www.tn.gov/generalservices/procurement/central-procurement-office--cpo-/contract-information/all-contracts-dashboard.html"
CSV_URL = "https://data.tn.gov/t/Public/views/CPOContractDetails/CPOContracts-ADACompliant.csv?:showVizHome=no"
SOURCE_NAME = "Tennessee CPO All Contracts Dashboard"
USER_AGENT = "soe-group3-tn-cpo-contracts/0.1"

FIELD_MAP = {
    "available_to_local_government": "Available To Local Government Calculated",
    "contract_number": "Contract",
    "title": "Contract Description",
    "contract_type": "Contract Type",
    "measure_name": "Measure Names",
    "multi_year": "Multi-Year Contract Calculated",
    "supplier_name": "Supplier Name",
    "index": "Index",
    "start_date": "Begin Date Calculated",
    "end_date": "Expire Date Calculated",
    "amount": "Measure Values",
}


def fetch_contracts(
    *,
    vendor_terms: list[str],
    keywords: list[str],
    max_per_vendor: int,
    progress: Callable[[str], None] | None = None,
) -> list[dict[str, str]]:
    rows = fetch_dashboard_rows()
    emit(progress, f"TN CPO all contracts Tableau CSV: scanned {len(rows)} amount rows")

    records: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in rows:
        record = normalize_row(row, vendor_terms=vendor_terms, keywords=keywords)
        if not record or record["id"] in seen:
            continue
        seen.add(record["id"])
        records.append(record)

    limit = max(1, max_per_vendor) * max(1, len(unique_terms(vendor_terms)))
    return sorted(records, key=contract_sort_key, reverse=True)[:limit]


def fetch_dashboard_rows() -> list[dict[str, str]]:
    result = fetch_url(
        CSV_URL,
        headers={"Accept": "text/csv,*/*", "Referer": DASHBOARD_PAGE_URL},
        timeout=120,
        byte_limit=10_000_000,
        user_agent=USER_AGENT,
    )
    result.raise_for_status()
    text = result.body_text()
    reader = csv.DictReader(io.StringIO(text))
    rows: list[dict[str, str]] = []
    for row in reader:
        if clean_text(row.get(FIELD_MAP["measure_name"])).lower() != "amount":
            continue
        if not clean_text(row.get(FIELD_MAP["contract_number"])) or not clean_text(row.get(FIELD_MAP["supplier_name"])):
            continue
        rows.append({key: clean_text(row.get(source_key), 1000) for key, source_key in FIELD_MAP.items()})
    return rows


def normalize_row(row: dict[str, str], *, vendor_terms: list[str], keywords: list[str]) -> dict[str, str] | None:
    contract_number = clean_text(row.get("contract_number"), 120)
    supplier_name = clean_text(row.get("supplier_name"), 180)
    title = clean_text(row.get("title") or contract_number, 500)
    end_date = iso_date(row.get("end_date"))
    if not contract_number or not supplier_name or not end_date:
        return None

    search_text = " ".join(
        [
            contract_number,
            title,
            supplier_name,
            clean_text(row.get("contract_type"), 180),
            clean_text(row.get("available_to_local_government"), 80),
            clean_text(row.get("multi_year"), 80),
        ]
    )
    vendor_hits = keyword_hits(supplier_name, unique_terms(vendor_terms))
    matched = keyword_hits(search_text, keywords)
    if not vendor_hits and not useful_keyword_match(matched, search_text):
        return None

    months = months_until(end_date)
    recompete = recompete_signal(months)
    amount = amount_string(row.get("amount")) or "0"
    record_type = contract_record_type(row.get("contract_type"))
    source_record_id = f"{contract_number}-{clean_text(row.get('index'), 40)}" if clean_text(row.get("index")) else contract_number

    raw = dict(row)
    raw["source_key"] = "tn_cpo_all_contracts_dashboard"
    raw["source_note"] = "Official tn.gov Tableau dashboard CSV export for all contracts; public probe on 2026-07-27 found no safely reachable fuller export. ADA CSV truncates Supplier Name to 40 chars and Contract Description to 30 chars; NV00000000 is dashboard-present but reused, so source_record_id appends Index when available. Statewide listing links redirect to Edison/Oracle IDCS from CLI."

    return {
        "id": stable_id("TN", source_record_id, supplier_name, title, prefix="tn-cpo-contract"),
        "state": "TN",
        "source": SOURCE_NAME,
        "source_record_id": source_record_id,
        "parent_id": contract_number,
        "contract_record_type": record_type,
        "vendor_name": supplier_name,
        "vendor_query": ";".join(vendor_hits),
        "agency": "Tennessee Central Procurement Office",
        "contract_number": contract_number,
        "title": title,
        "amount": amount,
        "execution_date": "",
        "start_date": iso_date(row.get("start_date")),
        "end_date": end_date,
        "months_to_end": "" if months is None else str(months),
        "recompete_signal": recompete,
        "document_type": clean_text(row.get("contract_type") or "Tennessee Contract", 160),
        "document_url": DASHBOARD_PAGE_URL,
        "source_url": CSV_URL,
        "matched_keywords": ";".join(matched),
        "relevance_score": str(relevance_score(vendor_hits, matched, recompete, search_text, amount, record_type)),
        "raw_json": json.dumps(raw, ensure_ascii=True, sort_keys=True, separators=(",", ":")),
        "last_checked_at": now_iso(),
    }


def useful_keyword_match(matches: list[str], text: str) -> bool:
    return keyword_context_match(matches, text)


def contract_record_type(contract_type: Any) -> str:
    lower = clean_text(contract_type).lower()
    if "grant" in lower:
        return "award"
    if "agency term" in lower or "statewide" in lower:
        return "master_agreement"
    if "delegated" in lower:
        return "award"
    return "parent_contract"


def relevance_score(vendor_hits: list[str], matches: list[str], recompete: str, text: str, amount: str, record_type: str) -> int:
    score = min(45, len(matches) * 8)
    if vendor_hits:
        score += 35
    if any(term_matches(text, term) for term in ["Medicaid", "MMIS", "TennCare", "Medicare", "managed care", "provider data"]):
        score += 28
    if any(term_matches(text, term) for term in ["rural health", "behavioral health", "claims", "eligibility", "CMS"]):
        score += 16
    if int_or_zero(amount) >= 1_000_000:
        score += 12
    if recompete == "Expiring soon":
        score += 25
    elif recompete == "Recompete watch":
        score += 18
    if record_type in {"master_agreement", "award"}:
        score += 5
    return max(0, min(score, 100))


def recompete_signal(months_to_end: int | None) -> str:
    if months_to_end is None:
        return "Unknown end date"
    if months_to_end < 0:
        return "Expired/past award"
    if months_to_end > 600:
        return "Open-ended/placeholder end date"
    if months_to_end <= 18:
        return "Expiring soon"
    if months_to_end <= 36:
        return "Recompete watch"
    return "Longer-term contract"


def contract_sort_key(row: dict[str, str]) -> tuple[int, int, str, int]:
    return (
        int_or_zero(row.get("relevance_score")),
        1 if row.get("vendor_query") else 0,
        row.get("end_date", ""),
        int_or_zero(row.get("amount")),
    )


def unique_terms(terms: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for term in terms:
        cleaned = clean_text(term, 100)
        key = cleaned.lower()
        if cleaned and key not in seen:
            seen.add(key)
            result.append(cleaned)
    return result


def int_or_zero(value: Any) -> int:
    try:
        return int(float(clean_text(value).replace(",", "") or 0))
    except (TypeError, ValueError):
        return 0


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def emit(progress: Callable[[str], None] | None, message: str) -> None:
    if progress:
        progress(message)
