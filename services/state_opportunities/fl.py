from __future__ import annotations

import datetime as dt
import json
import re
import time
import urllib.parse
from typing import Any, Callable

from services.state_http import fetch_url
from services.state_normalization import clean_text, compact_raw_json, iso_date, keyword_hits, parse_date, stable_id, term_matches

BASE_URL = "https://vendor.myfloridamarketplace.com/"
SEARCH_PAGE_URL = urllib.parse.urljoin(BASE_URL, "search/bids")
API_BASE_URL = urllib.parse.urljoin(BASE_URL, "mfmp/")
SEARCH_URL = urllib.parse.urljoin(API_BASE_URL, "pub/search/bids")
SEARCH_COUNT_URL = urllib.parse.urljoin(API_BASE_URL, "pub/search/bids/count")
DETAIL_URL = urllib.parse.urljoin(API_BASE_URL, "pub/search/bids/detail")
USER_AGENT = "soe-group3-fl-mfmp-opportunities/0.1"
SOURCE_NOTE = (
    "Official MyFloridaMarketPlace VIP Angular public search API; route chunk exposes "
    "PUB_BID_SEARCH=/pub/search/bids and detail endpoint /pub/search/bids/detail."
)
OPEN_STATUS = ["OPEN"]
PAGE_SIZE = 100


def fetch_opportunities(
    *,
    keywords: list[str],
    days_back: int,
    max_records: int,
    progress: Callable[[str], None] | None = None,
) -> list[dict[str, str]]:
    limit = max(1, max_records)
    rows = fetch_open_rows(max_records=limit, progress=progress)
    emit(progress, f"FL MFMP open advertisements: {len(rows)} public rows")

    records: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in rows:
        record = normalize_bid_row(row, keywords=keywords)
        if not record.get("source_record_id") or record["id"] in seen:
            continue
        if not is_open_or_recent(record["posted_date"], record["due_date"], days_back):
            continue
        if keywords and not record["matched_keywords"]:
            continue
        if false_keyword_hit(record) or not useful_keyword_match(record["matched_keywords"].split(";"), record["raw_json"]):
            continue

        detail = fetch_detail_if_useful(row, record)
        if detail:
            record = enrich_with_detail(record, detail, keywords=keywords)
            if keywords and not record["matched_keywords"]:
                continue
            if false_keyword_hit(record) or not useful_keyword_match(record["matched_keywords"].split(";"), record["raw_json"]):
                continue

        seen.add(record["id"])
        records.append(record)
        if len(records) >= limit:
            break

    return sorted(records, key=record_sort_key, reverse=True)[:limit]


def fetch_open_rows(*, max_records: int, progress: Callable[[str], None] | None = None) -> list[dict[str, Any]]:
    base_payload = search_payload()
    total = int_or_zero(post_json(SEARCH_COUNT_URL, base_payload))
    max_to_scan = min(max(total, PAGE_SIZE), max(PAGE_SIZE, max_records * 20))
    rows: list[dict[str, Any]] = []
    page = 1
    while len(rows) < max_to_scan:
        batch = valid_rows(post_json(SEARCH_URL, {**base_payload, "page": page}))
        if not batch:
            break
        rows.extend(batch[: max_to_scan - len(rows)])
        emit(progress, f"FL MFMP page {page}: {len(batch)} open rows")
        if len(batch) < PAGE_SIZE or len(rows) >= total:
            break
        page += 1
        time.sleep(0.2)
    return rows


def search_payload() -> dict[str, Any]:
    return {
        "pageSize": PAGE_SIZE,
        "type": [],
        "status": OPEN_STATUS,
        "agency": [],
        "adNumber": "",
        "agencyAdvertisementNumber": "",
        "title": "",
        "publishedDate": "",
        "openDate": "",
        "endDate": "",
        "commodityCodes": [],
        "intendsToParticipate": "",
        "assignee": "",
    }


def post_json(url: str, payload: dict[str, Any]) -> Any:
    result = fetch_url(
        url,
        method="POST",
        json_data=payload,
        headers=api_headers(),
        timeout=60,
        user_agent=USER_AGENT,
    )
    result.raise_for_status()
    return json.loads(result.body_text())


def get_json(url: str, params: dict[str, Any]) -> Any:
    query_url = url + "?" + urllib.parse.urlencode(params)
    headers = api_headers()
    headers.pop("Content-Type", None)
    result = fetch_url(query_url, headers=headers, timeout=60, user_agent=USER_AGENT)
    result.raise_for_status()
    return json.loads(result.body_text())


def api_headers() -> dict[str, str]:
    return {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "Origin": BASE_URL.rstrip("/"),
        "Referer": SEARCH_PAGE_URL,
    }


def normalize_bid_row(row: dict[str, Any], *, keywords: list[str]) -> dict[str, str]:
    advertisement_id = clean_id(row.get("advertisementId"))
    source_record_id = clean_text(row.get("uniqueName") or row.get("agencyAdNumber") or advertisement_id, 160)
    title = clean_text(row.get("title") or source_record_id, 500)
    agency = clean_text(row.get("agency") or nested_text(row, "organization", "name"), 180)
    bid_type = clean_text(row.get("type"), 120)
    status = clean_text(row.get("status") or "OPEN", 80)
    posted_date = iso_date(row.get("publishedDate") or row.get("publishDate") or row.get("openDate"))
    due_date = iso_date(row.get("closeDate"))
    detail_url = detail_page_url(advertisement_id)
    search_text = row_search_text(row)
    matched = keyword_hits(search_text, keywords)
    raw = dict(row)
    raw.update(
        {
            "source_key": "fl_myfloridamarketplace",
            "source_note": SOURCE_NOTE,
            "detail_url": detail_url,
        }
    )

    return {
        "id": stable_id("FL", advertisement_id or source_record_id, prefix="fl-mfmp-bid"),
        "state": "FL",
        "source": "MyFloridaMarketPlace VIP Public Advertisements",
        "source_record_id": source_record_id,
        "title": title,
        "agency": agency,
        "document_type": document_type(bid_type, source_record_id, title),
        "posted_date": posted_date,
        "due_date": due_date,
        "status": status,
        "amount": "",
        "document_url": detail_url,
        "source_url": SEARCH_PAGE_URL,
        "matched_keywords": ";".join(matched),
        "relevance_score": str(relevance_score(matched, status, search_text, due_date)),
        "raw_json": compact_raw_json(raw),
        "last_checked_at": now_iso(),
    }


def fetch_detail_if_useful(row: dict[str, Any], record: dict[str, str]) -> dict[str, Any] | None:
    if record.get("matched_keywords"):
        return fetch_detail(row)
    agency = record.get("agency", "")
    if any(term_matches(agency, term) for term in health_agency_terms()):
        return fetch_detail(row)
    return None


def fetch_detail(row: dict[str, Any]) -> dict[str, Any] | None:
    advertisement_id = clean_id(row.get("advertisementId"))
    if not advertisement_id:
        return None
    try:
        detail = get_json(DETAIL_URL, {"id": advertisement_id})
    except Exception:
        return None
    return detail if isinstance(detail, dict) else None


def enrich_with_detail(record: dict[str, str], detail: dict[str, Any], *, keywords: list[str]) -> dict[str, str]:
    detail_text = detail_search_text(detail)
    matched = keyword_hits(detail_text, keywords)
    raw = json.loads(record.get("raw_json") or "{}")
    raw["detail"] = detail
    raw["detail_url"] = record.get("document_url", "")

    enriched = dict(record)
    enriched["title"] = clean_text(detail.get("title") or record.get("title"), 500)
    enriched["agency"] = clean_text(detail.get("agency") or record.get("agency"), 180)
    enriched["document_type"] = document_type(clean_text(detail.get("type"), 120), record.get("source_record_id", ""), enriched["title"])
    enriched["posted_date"] = iso_date(detail.get("publishedDate") or detail.get("openDate")) or record.get("posted_date", "")
    enriched["due_date"] = iso_date(detail.get("closeDate")) or record.get("due_date", "")
    enriched["status"] = clean_text(detail.get("status") or record.get("status"), 80)
    enriched["matched_keywords"] = ";".join(matched)
    enriched["relevance_score"] = str(relevance_score(matched, enriched["status"], detail_text, enriched["due_date"]))
    enriched["raw_json"] = compact_raw_json(raw)
    return enriched


def row_search_text(row: dict[str, Any]) -> str:
    text = " ".join(
        [
            clean_text(row.get("uniqueName"), 160),
            clean_text(row.get("agencyAdNumber"), 160),
            clean_text(row.get("title"), 500),
            clean_text(row.get("agency"), 180),
            clean_text(nested_text(row, "organization", "name"), 180),
            clean_text(row.get("type"), 120),
            clean_text(row.get("status"), 80),
        ]
    )
    return expand_related_terms(text)


def detail_search_text(detail: dict[str, Any]) -> str:
    commodities = " ".join(clean_text(item.get("value"), 200) for item in detail.get("commodityCodes") or [] if isinstance(item, dict))
    docs = " ".join(clean_text(item.get("description") or item.get("fileName"), 300) for item in detail.get("docs") or [] if isinstance(item, dict))
    contact = detail.get("responseContact") if isinstance(detail.get("responseContact"), dict) else {}
    text = " ".join(
        [
            row_search_text(detail),
            clean_text(detail.get("description"), 4000),
            commodities,
            docs,
            clean_text(contact.get("responseContact"), 120),
            clean_text(contact.get("email"), 160),
        ]
    )
    return expand_related_terms(text)


def valid_rows(value: Any) -> list[dict[str, Any]]:
    return [row for row in value or [] if isinstance(row, dict)]


def expand_related_terms(text: str) -> str:
    if term_matches(text, "RHTP"):
        return f"{text} rural health rural health transformation"
    return text


def detail_page_url(advertisement_id: str) -> str:
    if not advertisement_id:
        return SEARCH_PAGE_URL
    return urllib.parse.urljoin(BASE_URL, f"search/bids/detail/{urllib.parse.quote(advertisement_id)}")


def document_type(bid_type: str, source_record_id: str, title: str) -> str:
    base = f"VBS {bid_type}" if bid_type else "VBS Advertisement"
    text = " ".join([source_record_id, title, bid_type]).upper()
    if code_matches(text, "RFI"):
        return "VBS Request for Information"
    if code_matches(text, "RFP"):
        return "VBS Request for Proposals"
    if code_matches(text, "ITB"):
        return "VBS Invitation to Bid"
    if "GRANT" in text:
        return "VBS Grant Opportunity"
    return base


def useful_keyword_match(matches: list[str], text: str) -> bool:
    generic_terms = {"claims", "eligibility", "enrollment"}
    matched_terms = {match.lower() for match in matches if match}
    if not matched_terms or not matched_terms <= generic_terms:
        return True
    context_terms = [
        "agency for health care administration",
        "department of children and families",
        "department of health",
        "healthcare",
        "health care",
        "medicaid",
        "medicare",
        "medical",
        "hospital",
        "behavioral",
        "managed care",
        "provider",
        "chip",
        "mmis",
        "ahca",
        "dcf",
    ]
    return any(term_matches(text, term) for term in context_terms)


def false_keyword_hit(record: dict[str, str]) -> bool:
    text = " ".join([record.get("title", ""), record.get("raw_json", "")])
    return "mmis" in {item.lower() for item in record.get("matched_keywords", "").split(";") if item} and term_matches(text, "commissary") and not term_matches(text, "MMIS")


def relevance_score(matches: list[str], status: str, text: str, due_date: str) -> int:
    score = min(50, len([match for match in matches if match]) * 10)
    if any(term_matches(text, term) for term in ["Medicaid", "MMIS", "Agency for Health Care Administration", "Department of Children and Families", "Department of Health"]):
        score += 25
    if any(term_matches(text, term) for term in ["eligibility", "claims", "enrollment", "managed care", "interoperability", "FHIR", "prior authorization", "provider data"]):
        score += 15
    if any(term_matches(text, term) for term in ["rural health", "RHTP", "rural health transformation"]):
        score += 25
    if any(term_matches(text, term) for term in ["RFP", "RFI", "RFQ", "ITB", "software", "data", "cloud", "platform", "services"]):
        score += 12
    if status.lower() in {"open", "posted", "published"}:
        score += 10
    due = parse_date(due_date)
    if due and due >= dt.date.today():
        score += 8
    return min(score, 100)


def is_open_or_recent(posted_date: str, due_date: str, days_back: int) -> bool:
    due = parse_date(due_date)
    if due and due >= dt.date.today():
        return True
    if days_back <= 0:
        return True
    posted = parse_date(posted_date)
    return not posted or (dt.date.today() - posted).days <= days_back


def record_sort_key(row: dict[str, str]) -> tuple[int, str, str]:
    return (int_or_zero(row.get("relevance_score")), row.get("due_date", ""), row.get("posted_date", ""))


def health_agency_terms() -> list[str]:
    return [
        "Agency for Health Care Administration",
        "Department of Children and Families",
        "Department of Health",
        "AHCA",
        "DCF",
    ]


def nested_text(row: dict[str, Any], *keys: str) -> str:
    value: Any = row
    for key in keys:
        if not isinstance(value, dict):
            return ""
        value = value.get(key)
    return clean_text(value, 300)


def clean_id(value: Any) -> str:
    if value is None or value == "":
        return ""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return clean_text(value, 80)
    return str(int(number)) if number.is_integer() else clean_text(value, 80)


def code_matches(text: str, code: str) -> bool:
    return re.search(rf"(?<![A-Z0-9]){re.escape(code)}(?![A-Z0-9])", text, re.IGNORECASE) is not None


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
