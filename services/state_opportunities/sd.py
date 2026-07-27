from __future__ import annotations

import datetime as dt
import json
import re
import time
import urllib.parse
from typing import Any, Callable

from services.state_http import fetch_url
from services.state_normalization import clean_text, compact_raw_json, iso_date, keyword_hits, parse_date, stable_id, term_matches

ENTITY_ID = "3444a404-3818-494f-84c5-2a850acd7779"
BASE_URL = "https://postingboard.esmsolutions.com/"
LISTING_URL = urllib.parse.urljoin(BASE_URL, f"{ENTITY_ID}/events")
API_URL = urllib.parse.urljoin(BASE_URL, f"api/postingBoard/{ENTITY_ID}/currentevents")
OPEN_SD_RFP_URL = "https://open.sd.gov/rfp.aspx"
SOURCE_NOTE = (
    "Official Open SD RFP/Bid Search links to the public ESM Solutions posting board; "
    "the adapter normalizes currentevents API rows only."
)
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"


def fetch_opportunities(
    *,
    keywords: list[str],
    days_back: int,
    max_records: int,
    progress: Callable[[str], None] | None = None,
) -> list[dict[str, str]]:
    rows = fetch_current_event_rows(max_records=max_records)
    emit(progress, f"SD ESM posting board current events: {len(rows)} public rows")

    records: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in rows:
        record = normalize_event_row(row, keywords=keywords)
        if not record.get("source_record_id") or record["id"] in seen:
            continue
        if not is_open_or_recent(record["posted_date"], record["due_date"], days_back):
            continue
        if keywords and not record["matched_keywords"]:
            continue
        if not useful_keyword_match(record["matched_keywords"].split(";"), record["raw_json"]):
            continue
        seen.add(record["id"])
        records.append(record)

    return sorted(records, key=record_sort_key, reverse=True)[: max(1, max_records)]


def fetch_current_event_rows(*, max_records: int) -> list[dict[str, Any]]:
    page_size = 100
    first = fetch_events_page(page_no=0, page_size=page_size)
    rows = valid_rows(first.get("data"))
    total = int_or_zero(first.get("totalCount"))
    max_to_scan = min(max(total, len(rows)), max(100, max_records * 20))
    page_no = 1
    while len(rows) < max_to_scan:
        payload = fetch_events_page(page_no=page_no, page_size=page_size)
        batch = valid_rows(payload.get("data"))
        if not batch:
            break
        rows.extend(batch)
        page_no += 1
    return rows[:max_to_scan]


def fetch_events_page(*, page_no: int, page_size: int) -> dict[str, Any]:
    query = urllib.parse.urlencode(
        {
            "pageNo": str(page_no),
            "recordsPerPage": str(page_size),
            "searchText": "",
            "browserGlobalTimeZoneNameId": "Central Standard Time",
            "browserGlobalTimeZoneName": "America/Chicago",
            "browserOffset": "-05:00:00",
        }
    )
    url = f"{API_URL}?{query}"
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Referer": LISTING_URL,
    }
    last_error = ""
    for attempt in range(3):
        result = fetch_url(url, headers=headers, timeout=60, byte_limit=1_000_000, user_agent=USER_AGENT)
        if result.ok:
            data = result.json_data()
            return data if isinstance(data, dict) else {}
        last_error = result.error or f"HTTP {result.status_code}"
        time.sleep(1 + attempt)
    raise RuntimeError(f"SD ESM currentevents request failed: {last_error}")


def normalize_event_row(row: dict[str, Any], *, keywords: list[str]) -> dict[str, str]:
    source_record_id = clean_text(row.get("id") or row.get("eventId"), 180)
    event_id = clean_text(row.get("eventId"), 80)
    title = clean_text(row.get("eventName") or source_record_id, 500)
    posted_date = iso_date(row.get("publishedDate"))
    due_date = iso_date(row.get("eventDueDate"))
    status = nested_description(row.get("status")) or status_from_due(due_date)
    invitation_type = nested_description(row.get("invitationType"))
    agency = infer_agency(source_record_id, title)
    detail_url = event_detail_url(event_id)
    search_text = " ".join([source_record_id, event_id, title, agency, status, invitation_type])
    matched = keyword_hits(search_text, keywords)
    raw = dict(row)
    raw["source_key"] = "sd_procurement_management"
    raw["source_note"] = SOURCE_NOTE
    raw["listing_url"] = LISTING_URL
    raw["open_sd_rfp_url"] = OPEN_SD_RFP_URL

    return {
        "id": stable_id("SD", source_record_id or event_id, prefix="sd-esm-event"),
        "state": "SD",
        "source": "South Dakota ESM Posting Board Current Events",
        "source_record_id": source_record_id,
        "title": title,
        "agency": agency,
        "document_type": document_type(source_record_id, title),
        "posted_date": posted_date,
        "due_date": due_date,
        "status": status,
        "amount": "",
        "document_url": detail_url or LISTING_URL,
        "source_url": LISTING_URL,
        "matched_keywords": ";".join(matched),
        "relevance_score": str(relevance_score(matched, status, search_text, due_date)),
        "raw_json": compact_raw_json(raw),
        "last_checked_at": now_iso(),
    }


def valid_rows(value: Any) -> list[dict[str, Any]]:
    return [row for row in value or [] if isinstance(row, dict)]


def nested_description(value: Any) -> str:
    if isinstance(value, dict):
        return clean_text(value.get("description"), 120)
    return clean_text(value, 120)


def event_detail_url(event_id: str) -> str:
    if not event_id:
        return ""
    return urllib.parse.urljoin(BASE_URL, f"{ENTITY_ID}/eventDetail/{urllib.parse.quote(event_id)}")


def infer_agency(source_record_id: str, title: str) -> str:
    text = " ".join([source_record_id, title])
    if re.search(r"\bDOH\b", text, re.IGNORECASE) or term_matches(text, "rural health"):
        return "Department of Health"
    return ""


def document_type(source_record_id: str, title: str) -> str:
    text = " ".join([source_record_id, title]).upper()
    if code_matches(text, "RFI"):
        return "ESM Request for Information"
    if code_matches(text, "RFP"):
        return "ESM Request for Proposal"
    if code_matches(text, "RFQ"):
        return "ESM Request for Quote"
    if code_matches(text, "IFB"):
        return "ESM Invitation for Bid"
    return "ESM Solicitation"


def status_from_due(due_date: str) -> str:
    due = parse_date(due_date)
    if due and due < dt.date.today():
        return "Closed"
    return "Open"


def useful_keyword_match(matches: list[str], text: str) -> bool:
    generic_terms = {"claims", "eligibility", "enrollment"}
    matched_terms = {match.lower() for match in matches if match}
    if not matched_terms or not matched_terms <= generic_terms:
        return True
    context_terms = [
        "department of health",
        "department of social services",
        "health",
        "healthcare",
        "health care",
        "medicaid",
        "medicare",
        "medical",
        "hospital",
        "behavioral health",
        "managed care",
        "provider",
        "chip",
        "mmis",
        "rural health",
        "long-term care",
        "nursing facility",
    ]
    return any(term_matches(text, term) for term in context_terms)


def relevance_score(matches: list[str], status: str, text: str, due_date: str) -> int:
    score = min(50, len([match for match in matches if match]) * 10)
    if any(term_matches(text, term) for term in ["Medicaid", "MMIS", "Department of Health", "rural health", "long-term care"]):
        score += 25
    if any(term_matches(text, term) for term in ["eligibility", "claims", "enrollment", "managed care", "interoperability", "FHIR", "prior authorization", "provider data"]):
        score += 15
    if term_matches(text, "rural health transformation"):
        score += 25
    if any(term_matches(text, term) for term in ["RFP", "RFI", "RFQ", "software", "data", "platform", "services"]):
        score += 12
    if is_open_status(status):
        score += 10
    due = parse_date(due_date)
    if due and due >= dt.date.today():
        score += 8
    return min(score, 100)


def is_open_status(status: str) -> bool:
    return any(term in status.lower() for term in ["open", "ready", "response"])


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
