from __future__ import annotations

import csv
import datetime as dt
import io
import time
import urllib.error
import urllib.request
from typing import Any, Callable

from services.state_contracts.vss import VssContractConfig, fetch_vss_awarded_contracts
from services.state_contracts.keyword_context import useful_keyword_match as keyword_context_match
from services.state_normalization import clean_text, compact_raw_json, iso_date, keyword_hits, months_until, stable_id, term_matches

CO_VSS_CONTRACT_CONFIG = VssContractConfig(
    state="CO",
    source_name="ColoradoVSS Awarded Solicitations",
    source_key="co_vss",
    base_url="https://prd.co.cgiadvantage.com/PRDVSS1X1/Advantage4",
    source_note=(
        "Official OSC/SPCO Solicitations page links this ColoradoVSS public route. The public "
        "carousel does not expose Award History, so records come from awarded solicitation detail JSON."
    ),
)
CO_SPA_PAGE_URL = "https://osc.colorado.gov/spco/state-price-agreements"
CO_SPA_SHEET_URL = "https://docs.google.com/spreadsheets/d/1BucK0tZ4YOGpfAQlMSrtO1n_MCtBHG1VwLichR-iaqo/export?format=csv&gid=0"
CO_BIDS_CATEGORY_URL = "https://www.bidscolorado.com/co/portal.nsf/xpPriceAgreementsByCategory.xsp"
CO_SPA_SOURCE_NAME = "Colorado OSC/SPCO State Price Agreement Schedule"
USER_AGENT = "soe-group3-co-contracts/0.1"


def fetch_contracts(
    *,
    vendor_terms: list[str],
    keywords: list[str],
    max_per_vendor: int,
    progress: Callable[[str], None] | None = None,
) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    seen: set[str] = set()

    for record in fetch_state_price_agreements(vendor_terms=vendor_terms, keywords=keywords, max_per_vendor=max_per_vendor, progress=progress):
        if record["id"] not in seen:
            seen.add(record["id"])
            records.append(record)

    try:
        vss_records = fetch_vss_awarded_contracts(
            config=CO_VSS_CONTRACT_CONFIG,
            vendor_terms=vendor_terms,
            keywords=keywords,
            max_per_vendor=max_per_vendor,
            progress=progress,
        )
    except RuntimeError as exc:
        emit(progress, f"CO VSS awarded solicitations: skipped after {exc}")
        vss_records = []
    emit(progress, f"CO VSS awarded solicitations: normalized {len(vss_records)} records")
    for record in vss_records:
        if record["id"] not in seen:
            seen.add(record["id"])
            records.append(record)

    return sorted(records, key=contract_sort_key, reverse=True)


def fetch_state_price_agreements(
    *,
    vendor_terms: list[str],
    keywords: list[str],
    max_per_vendor: int,
    progress: Callable[[str], None] | None = None,
) -> list[dict[str, str]]:
    rows = fetch_spa_rows()
    terms = unique_terms(vendor_terms)
    records: list[dict[str, str]] = []
    seen: set[str] = set()
    query_counts: dict[str, int] = {}
    query_limit = max(1, max_per_vendor)

    for row in rows:
        record = normalize_spa_row(row, vendor_terms=terms, keywords=keywords)
        if not record:
            continue
        query = record.get("vendor_query", "")
        if query and query_counts.get(query, 0) >= query_limit:
            continue
        if record["id"] in seen:
            continue
        seen.add(record["id"])
        if query:
            query_counts[query] = query_counts.get(query, 0) + 1
        records.append(record)

    emit(progress, f"CO State Price Agreements: scanned {len(rows)} public current rows, normalized {len(records)} records")
    return sorted(records, key=contract_sort_key, reverse=True)


def fetch_spa_rows() -> list[dict[str, str]]:
    text = fetch_text(CO_SPA_SHEET_URL)
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    header_index = next((index for index, row in enumerate(rows) if "Vendor" in row and "Current Term Expiration Date" in row), -1)
    if header_index < 0:
        raise RuntimeError("CO state price agreement schedule CSV did not expose expected headers")
    headers = rows[header_index]
    parsed: list[dict[str, str]] = []
    for row in rows[header_index + 1 :]:
        if not any(cell.strip() for cell in row):
            continue
        padded = row + [""] * max(0, len(headers) - len(row))
        parsed.append({header: clean_text(value, 1000) for header, value in zip(headers, padded)})
    return parsed


def fetch_text(url: str, timeout: int = 60) -> str:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/csv,text/plain,*/*",
        "Referer": CO_SPA_PAGE_URL,
    }
    request = urllib.request.Request(url, headers=headers)
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read().decode("utf-8", "replace")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = exc
            time.sleep(1 + attempt)
    raise RuntimeError(f"CO state price agreement schedule request failed: {last_error}")


def normalize_spa_row(row: dict[str, str], *, vendor_terms: list[str], keywords: list[str]) -> dict[str, str] | None:
    vendor_name = clean_text(row.get("Vendor"), 180)
    contract_number = clean_text(row.get("Contract #") or row.get("CORE SPA1 #"), 120)
    core_spa = clean_text(row.get("CORE SPA1 #"), 120)
    end_date = iso_date(row.get("Current Term Expiration Date"))
    status = clean_text(row.get("Status"), 80)
    category = clean_text(row.get("BIDS Category Name"), 220)
    subcategory = clean_text(row.get("BIDS Sub-Category"), 220)
    if not vendor_name or not contract_number or not end_date or not is_current_status(status):
        return None

    title = clean_text(" - ".join(part for part in [category, subcategory] if part and part.upper() != "N/A") or category, 500)
    if not title:
        return None

    search_text = " ".join(
        [
            vendor_name,
            row.get("CORE Vendor Number", ""),
            core_spa,
            contract_number,
            title,
            row.get("Mandatory or Permissive", ""),
            row.get("NVP, MMCAP, Omnia, RFx or SPCO", ""),
            row.get("Lead State (if NVP or MMCAP)", ""),
            row.get("Comments", ""),
        ]
    )
    vendor_hits = keyword_hits(vendor_name, vendor_terms)
    matched = keyword_hits(search_text, keywords)
    if not vendor_hits and not useful_keyword_match(matched, search_text):
        return None

    months = months_until(end_date)
    record_type = contract_record_type(row)
    query = ";".join(vendor_hits) if vendor_hits else ";".join(matched)
    raw = {
        "source_key": "co_state_price_agreements",
        "source_note": "Official OSC/SPCO page links the State Price Agreement schedule CSV and BIDS category portal.",
        "source_page_url": CO_SPA_PAGE_URL,
        "schedule_url": CO_SPA_SHEET_URL,
        "bids_category_url": CO_BIDS_CATEGORY_URL,
        "row": row,
    }

    return {
        "id": stable_id("CO", core_spa, contract_number, vendor_name, prefix="co-spa"),
        "state": "CO",
        "source": CO_SPA_SOURCE_NAME,
        "source_record_id": core_spa or contract_number,
        "parent_id": core_spa or contract_number,
        "contract_record_type": record_type,
        "vendor_name": vendor_name,
        "vendor_query": query,
        "agency": "Colorado State Purchasing & Contracts Office",
        "contract_number": contract_number,
        "title": title,
        "amount": "",
        "execution_date": "",
        "start_date": iso_date(row.get("Contract Effective Date")),
        "end_date": end_date,
        "months_to_end": "" if months is None else str(months),
        "recompete_signal": recompete_signal(months),
        "document_type": document_type(row),
        "document_url": CO_BIDS_CATEGORY_URL,
        "source_url": CO_SPA_PAGE_URL,
        "matched_keywords": ";".join(matched),
        "relevance_score": str(relevance_score(vendor_hits, matched, recompete_signal(months), search_text, record_type)),
        "raw_json": compact_raw_json(raw, limit=7000),
        "last_checked_at": now_iso(),
    }


def is_current_status(status: str) -> bool:
    return status.lower() in {"", "current", "active"}


def useful_keyword_match(matches: list[str], text: str) -> bool:
    return keyword_context_match(matches, text)


def contract_record_type(row: dict[str, str]) -> str:
    text = " ".join([row.get("BIDS Category Name", ""), row.get("NVP, MMCAP, Omnia, RFx or SPCO", "")]).lower()
    if "mmcap" in text or "omnia" in text or "nvp" in text:
        return "cooperative_contract"
    return "master_agreement"


def document_type(row: dict[str, str]) -> str:
    mandatory = clean_text(row.get("Mandatory or Permissive"), 20).upper()
    usage = {"M": "Mandatory", "P": "Permissive"}.get(mandatory, clean_text(row.get("Mandatory or Permissive"), 80))
    source = clean_text(row.get("NVP, MMCAP, Omnia, RFx or SPCO"), 80)
    parts = [part for part in [usage, source, "State Price Agreement"] if part]
    return clean_text(" ".join(parts), 160) or "State Price Agreement"


def relevance_score(vendor_hits: list[str], matches: list[str], recompete: str, text: str, record_type: str) -> int:
    score = min(45, len(matches) * 8)
    if vendor_hits:
        score += 35
    if any(term_matches(text, term) for term in ["Medicaid", "MMIS", "Medicare", "managed care", "provider data"]):
        score += 25
    if any(term_matches(text, term) for term in ["information technology", "IT Services", "software", "cloud", "SaaS"]):
        score += 12
    if recompete == "Expiring soon":
        score += 25
    elif recompete == "Recompete watch":
        score += 18
    elif recompete == "Longer-term contract":
        score += 8
    if record_type in {"master_agreement", "cooperative_contract"}:
        score += 8
    return max(0, min(score, 100))


def recompete_signal(months_to_end: int | None) -> str:
    if months_to_end is None:
        return "Unknown end date"
    if months_to_end < 0:
        return "Expired/past award"
    if months_to_end <= 18:
        return "Expiring soon"
    if months_to_end <= 36:
        return "Recompete watch"
    return "Longer-term contract"


def contract_sort_key(row: dict[str, str]) -> tuple[int, str, str, str]:
    return (int_or_zero(row.get("relevance_score")), row.get("end_date", ""), row.get("vendor_name", ""), row.get("title", ""))


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
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def emit(progress: Callable[[str], None] | None, message: str) -> None:
    if progress:
        progress(message)
