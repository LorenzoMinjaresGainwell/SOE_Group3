from __future__ import annotations

import csv
import datetime as dt
import io
import re
from typing import Any, Callable
from urllib.parse import urlparse

from services.state_http import fetch_url
from services.state_contracts.keyword_context import useful_keyword_match as keyword_context_match
from services.state_normalization import clean_text, compact_raw_json, iso_date, keyword_hits, months_until, stable_id, term_matches

OFFICIAL_PAGE_URL = "https://ai.wyo.gov/divisions/general-services/purchasing/all-agency-contracts"
SHEET_URL = "https://docs.google.com/spreadsheets/d/1XbHRh54Ibtjk9PWnr-hrzgUNZupU4gOO1QdKMJCRQTU/edit?usp=sharing"
CSV_URL = "https://docs.google.com/spreadsheets/d/1XbHRh54Ibtjk9PWnr-hrzgUNZupU4gOO1QdKMJCRQTU/gviz/tq?tqx=out:csv"
PUBLIC_PURCHASE_CLOSED_URL = "https://www.publicpurchase.com/gems/wyominggsd,wy/buyer/public/publicClosedBidsInfo"
SOURCE_NAME = "Wyoming All Agency Contracts"
SOURCE_NOTE = (
    "Official Wyoming A&I General Services All Agency Contracts page links a public Google Sheet. "
    "The sheet exposes category, vendor, contract number, expiration date, and buyer. Public Purchase "
    "publicInfo/publicClosedBidsInfo were probed but are solicitation listings without awarded vendor/end-date fields."
)
USER_AGENT = "Mozilla/5.0 soe-group3-wy-all-agency-contracts/0.1"
PSEUDO_VENDOR_TERMS = {"", "tbd", "n/a", "na", "see website"}


def fetch_contracts(
    *,
    vendor_terms: list[str],
    keywords: list[str],
    max_per_vendor: int,
    progress: Callable[[str], None] | None = None,
) -> list[dict[str, str]]:
    rows = fetch_sheet_rows()
    emit(progress, f"WY all agency contracts: scanned {len(rows)} public sheet rows")

    terms = unique_terms(vendor_terms)
    records: list[dict[str, str]] = []
    seen: set[str] = set()
    query_counts: dict[str, int] = {}
    limit = max(1, max_per_vendor)

    for row in rows:
        record = normalize_sheet_row(row, vendor_terms=terms, keywords=keywords)
        if not record:
            continue
        query = record["vendor_query"]
        if query_counts.get(query, 0) >= limit:
            continue
        if record["id"] in seen:
            continue
        seen.add(record["id"])
        query_counts[query] = query_counts.get(query, 0) + 1
        records.append(record)

    emit(progress, f"WY all agency contracts: normalized {len(records)} records")
    return sorted(records, key=contract_sort_key, reverse=True)


def fetch_sheet_rows() -> list[dict[str, str]]:
    result = fetch_url(
        CSV_URL,
        headers={"Accept": "text/csv,text/plain,*/*", "Referer": OFFICIAL_PAGE_URL},
        timeout=60,
        byte_limit=1_000_000,
        user_agent=USER_AGENT,
    )
    result.raise_for_status()
    reader = csv.DictReader(io.StringIO(result.body_text()))
    return [{clean_header(key): clean_text(value, 1000) for key, value in row.items()} for row in reader]


def normalize_sheet_row(row: dict[str, str], *, vendor_terms: list[str], keywords: list[str]) -> dict[str, str]:
    contract_number = clean_text(row.get("contract number"), 120)
    vendor_name = clean_text(row.get("vendor"), 180)
    title = clean_text(row.get("category") or contract_number, 500)
    end_date = iso_date(row.get("expiration date"))
    if not contract_number or not normalized_vendor(vendor_name) or not title or not end_date:
        return {}

    website = clean_text(row.get("website"), 500)
    official_detail_url = official_contract_detail_url(website)
    buyer = clean_text(row.get("buyer"), 120)
    search_text = " ".join([contract_number, vendor_name, title, website, buyer])
    vendor_hits = keyword_hits(vendor_name, vendor_terms)
    matched = keyword_hits(search_text, keywords)
    if not vendor_hits and not useful_keyword_match(matched, search_text):
        return {}

    query = vendor_hits[0] if vendor_hits else matched[0]
    months = months_until(end_date)
    record_type = contract_record_type(search_text)
    raw = {
        "source_key": "wy_all_agency_contracts",
        "source_note": SOURCE_NOTE,
        "official_page_url": OFFICIAL_PAGE_URL,
        "public_purchase_closed_url_rejected": PUBLIC_PURCHASE_CLOSED_URL,
        "row": row,
    }
    if website:
        raw["row_website"] = website
        raw["row_website_url_type"] = "official_contract_detail" if official_detail_url else "vendor_or_non_official"
        if not official_detail_url:
            raw["vendor_website"] = website
    return {
        "id": stable_id("WY", contract_number, vendor_name, prefix="wy-all-agency-contract"),
        "state": "WY",
        "source": SOURCE_NAME,
        "source_record_id": contract_number,
        "parent_id": contract_number,
        "contract_record_type": record_type,
        "vendor_name": vendor_name,
        "vendor_query": query,
        "agency": "Wyoming Administration and Information - General Services Division",
        "contract_number": contract_number,
        "title": title,
        "amount": "0",
        "execution_date": "",
        "start_date": "",
        "end_date": end_date,
        "months_to_end": "" if months is None else str(months),
        "recompete_signal": recompete_signal(months),
        "document_type": "Wyoming All Agency Contract",
        "document_url": official_detail_url or SHEET_URL,
        "source_url": SHEET_URL,
        "matched_keywords": ";".join(matched),
        "relevance_score": str(relevance_score(vendor_hits, matched, months, search_text, record_type)),
        "raw_json": compact_raw_json(raw),
        "last_checked_at": now_iso(),
    }


def clean_header(value: Any) -> str:
    return clean_text(value).strip().lower()


def normalized_vendor(value: str) -> str:
    vendor = clean_text(value, 180)
    return "" if vendor.lower() in PSEUDO_VENDOR_TERMS else vendor


def official_contract_detail_url(website: str) -> str:
    url = normalized_http_url(website)
    if not url:
        return ""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    path = parsed.path.lower()
    if is_official_wy_host(host) and any(term in path for term in ("contract", "procurement", "purchas")):
        return url
    return ""


def normalized_http_url(value: str) -> str:
    text = clean_text(value, 500)
    if not text or re.search(r"\s", text):
        return ""
    if re.match(r"https?://", text, re.IGNORECASE):
        candidate = text
    elif re.match(r"(?:[A-Za-z0-9-]+\.)*wyo\.gov(?:/|$)", text, re.IGNORECASE):
        candidate = f"https://{text}"
    else:
        return ""
    parsed = urlparse(candidate)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return ""
    return candidate


def is_official_wy_host(host: str) -> bool:
    return host == "wyo.gov" or host.endswith(".wyo.gov")


def useful_keyword_match(matches: list[str], text: str) -> bool:
    return keyword_context_match(matches, text)


def contract_record_type(text: str) -> str:
    lower = text.lower()
    if any(term in lower for term in ["sourcewell", "naspo", "cooperative"]):
        return "cooperative_contract"
    return "master_agreement"


def relevance_score(vendor_hits: list[str], matches: list[str], months_to_end: int | None, text: str, record_type: str) -> int:
    score = min(45, len(matches) * 8)
    if vendor_hits:
        score += 35
    if any(term_matches(text, term) for term in ["Medicaid", "MMIS", "Medicare", "managed care", "provider data"]):
        score += 25
    if any(term_matches(text, term) for term in ["software", "technology", "cloud", "data"]):
        score += 12
    if months_to_end is not None:
        if 0 <= months_to_end <= 18:
            score += 25
        elif months_to_end <= 36:
            score += 18
        elif months_to_end > 36:
            score += 6
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


def contract_sort_key(row: dict[str, str]) -> tuple[int, int, str, str]:
    return (
        int_or_zero(row.get("relevance_score")),
        1 if row.get("vendor_query") else 0,
        row.get("end_date", ""),
        row.get("title", ""),
    )


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
