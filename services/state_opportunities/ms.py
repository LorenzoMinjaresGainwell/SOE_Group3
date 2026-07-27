from __future__ import annotations

import datetime as dt
import json
import re
import time
import urllib.parse
from typing import Any, Callable

from services.state_http import fetch_url
from services.state_normalization import (
    amount_string,
    clean_id,
    clean_text,
    compact_raw_json,
    iso_date,
    keyword_hits,
    parse_date,
    stable_id,
    term_matches,
)

APP_URL = "https://www.ms.gov/dfa/contract_bid_search/"
BID_PAGE_URL = urllib.parse.urljoin(APP_URL, "Bid?autoloadGrid=False")
BID_DATA_URL = urllib.parse.urljoin(APP_URL, "Bid/BidData")
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
SOURCE_NOTE = "Official Mississippi DFA bid-search DataTables JSON endpoint from public Bid page."
BID_COLUMNS = ["Agency", "BidNumber", "ObjectID", "VerNumber", "BidStatus", "AdvertiseDate", "SubmissionDate", "OpeningDate", "BidID"]
MAX_SCAN_ROWS = 5000


def fetch_opportunities(
    *,
    keywords: list[str],
    days_back: int,
    max_records: int,
    progress: Callable[[str], None] | None = None,
) -> list[dict[str, str]]:
    rows = fetch_bid_rows(max_records=max_records)
    emit(progress, f"MS DFA bid search: {len(rows)} public rows")

    records: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in rows:
        record = normalize_bid_row(row, keywords=keywords)
        if not record.get("source_record_id") or record["id"] in seen:
            continue
        if not is_open_or_recent(record["status"], record["posted_date"], record["due_date"], days_back):
            continue
        if keywords and not record["matched_keywords"]:
            continue
        if not useful_keyword_match(record["matched_keywords"].split(";"), record["raw_json"]):
            continue
        seen.add(record["id"])
        records.append(record)

    return sorted(records, key=record_sort_key, reverse=True)[: max(1, max_records)]


def fetch_bid_rows(*, max_records: int) -> list[dict[str, Any]]:
    page_size = max(1000, min(9999, max_records * 50))
    rows: list[dict[str, Any]] = []
    start = 0
    total: int | None = None

    while len(rows) < MAX_SCAN_ROWS:
        payload = fetch_bid_page(start=start, length=page_size)
        batch = valid_rows(payload.get("aaData") or payload.get("data"))
        if total is None:
            total = int_or_zero(payload.get("iTotalDisplayRecords") or payload.get("iTotalRecords") or len(batch))
        if not batch:
            break
        rows.extend(batch)
        start += len(batch)
        if total and len(rows) >= min(total, MAX_SCAN_ROWS):
            break
        if len(batch) < page_size:
            break
    return rows[:MAX_SCAN_ROWS]


def fetch_bid_page(*, start: int, length: int) -> dict[str, Any]:
    url = BID_DATA_URL + "?" + urllib.parse.urlencode({"AppId": "1"})
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": BID_PAGE_URL,
        "X-Requested-With": "XMLHttpRequest",
    }
    last_error = ""
    for attempt in range(3):
        result = fetch_url(
            url,
            method="POST",
            data=datatable_params(start=start, length=length),
            headers=headers,
            timeout=60,
            byte_limit=5_000_000,
            user_agent=USER_AGENT,
        )
        if result.ok:
            try:
                data = json.loads(result.body_text())
            except json.JSONDecodeError as exc:
                last_error = str(exc)
            else:
                if isinstance(data, dict):
                    return data
                last_error = "JSON root was not an object"
        else:
            last_error = result.error or f"HTTP {result.status_code}"
        time.sleep(1 + attempt)
    raise RuntimeError(f"MS DFA bid search request failed: {last_error}")


def datatable_params(*, start: int, length: int) -> dict[str, str]:
    params = {
        "sEcho": "1",
        "iColumns": str(len(BID_COLUMNS)),
        "sColumns": "",
        "iDisplayStart": str(start),
        "iDisplayLength": str(length),
        "sSearch": "",
        "bRegex": "false",
        "iSortingCols": "0",
    }
    for index, column in enumerate(BID_COLUMNS):
        params[f"mDataProp_{index}"] = column
        params[f"sSearch_{index}"] = ""
        params[f"bRegex_{index}"] = "false"
        params[f"bSearchable_{index}"] = "true"
        params[f"bSortable_{index}"] = "true"
    return params


def normalize_bid_row(row: dict[str, Any], *, keywords: list[str]) -> dict[str, str]:
    bid_id = clean_id(row.get("BidID"))
    source_record_id = clean_text(row.get("BidNumber") or row.get("ObjectID") or bid_id, 180)
    title = clean_text(row.get("BidDescription") or row.get("ShortDescription") or source_record_id, 500)
    agency = clean_text(row.get("Agency"), 180) or infer_agency(" ".join([title, clean_text(row.get("AdditionalInfo"), 500)]))
    posted_date = iso_date(row.get("AdvertiseDate"))
    due_date = iso_date(row.get("SubmissionDate") or row.get("OpeningDate"))
    status = clean_text(row.get("BidStatus") or "Open", 80)
    document_url = clean_text(row.get("PDFUrl"), 500) or first_attachment_url(row)
    detail_url = bid_detail_url(bid_id)
    raw = dict(row)
    raw["source_key"] = "ms_contract_bid_search"
    raw["source_note"] = SOURCE_NOTE
    raw["detail_url"] = detail_url
    search_text = " ".join(
        [
            source_record_id,
            clean_text(row.get("ObjectID"), 120),
            title,
            agency,
            clean_text(row.get("BidType") or row.get("BidTypeDescription"), 120),
            clean_text(row.get("ProcurementCategoryDescription"), 200),
            clean_text(row.get("SubProcurementCategoryDescription"), 200),
            clean_text(row.get("AdditionalInfo"), 1200),
            clean_text(row.get("BuyerName"), 160),
            compact_raw_json(row.get("Attachments") or [], limit=2000),
        ]
    )
    matched = keyword_hits(search_text, keywords)

    return {
        "id": stable_id("MS", bid_id or source_record_id, prefix="ms-dfa-bid"),
        "state": "MS",
        "source": "Mississippi DFA Procurement Opportunities",
        "source_record_id": source_record_id,
        "title": title,
        "agency": agency,
        "document_type": document_type(row, source_record_id, title),
        "posted_date": posted_date,
        "due_date": due_date,
        "status": status,
        "amount": amount_string(row.get("AwardAmount")),
        "document_url": document_url or detail_url or BID_PAGE_URL,
        "source_url": detail_url or BID_PAGE_URL,
        "matched_keywords": ";".join(matched),
        "relevance_score": str(relevance_score(matched, status, search_text, due_date)),
        "raw_json": compact_raw_json(raw),
        "last_checked_at": now_iso(),
    }


def document_type(row: dict[str, Any], source_record_id: str, title: str) -> str:
    bid_type = clean_text(row.get("BidType") or row.get("BidTypeDescription"), 120)
    if bid_type:
        return f"Mississippi {bid_type}"
    text = " ".join([source_record_id, title]).upper()
    if "RFIN" in text or code_matches(text, "RFI"):
        return "Mississippi Request for Information"
    if "RFPR" in text or code_matches(text, "RFP"):
        return "Mississippi Request for Proposal"
    if "RFQU" in text or code_matches(text, "RFQ"):
        return "Mississippi Request for Quote"
    if "NBID" in text:
        return "Mississippi Negotiated Bid"
    return "Mississippi Procurement Opportunity"


def first_attachment_url(row: dict[str, Any]) -> str:
    for item in row.get("Attachments") or []:
        if not isinstance(item, dict):
            continue
        url = clean_text(item.get("Url"), 500)
        if url:
            return url
    return ""


def bid_detail_url(bid_id: str) -> str:
    if not bid_id:
        return ""
    return urllib.parse.urljoin(APP_URL, f"Bid/Details/{urllib.parse.quote(bid_id)}?" + urllib.parse.urlencode({"AppId": "1"}))


def infer_agency(text: str) -> str:
    cleaned = clean_text(text, 1500)
    patterns = [
        r"\b(Department of Human Services)\b",
        r"\b(Dept\.? of Human Services)\b",
        r"\b(Department of Health)\b",
        r"\b(Dept\.? of Health)\b",
        r"\b(Division of Medicaid)\b",
        r"\b(Department of Mental Health)\b",
        r"\b(Department of Information Technology Services)\b",
        r"\b(MS Dept\.? of Information Technology Services)\b",
        r"\b(University of Mississippi Medical Center)\b",
        r"\b(Univ of MS Medical Center)\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, cleaned, re.IGNORECASE)
        if match:
            return clean_text(match.group(1), 180)
    return ""


def useful_keyword_match(matches: list[str], text: str) -> bool:
    ambiguous_terms = {"claims", "eligibility", "enrollment", "cms"}
    matched_terms = {match.lower() for match in matches if match}
    if not matched_terms or not matched_terms <= ambiguous_terms:
        return True
    context_terms = [
        "department of health",
        "department of human services",
        "division of medicaid",
        "university of mississippi medical center",
        "medical center",
        "healthcare",
        "health care",
        "medicaid",
        "medicare",
        "medical",
        "behavioral",
        "managed care",
        "provider",
        "chip",
        "mmis",
        "wic",
    ]
    return any(term_matches(text, term) for term in context_terms)


def relevance_score(matches: list[str], status: str, text: str, due_date: str) -> int:
    score = min(50, len([match for match in matches if match]) * 10)
    if any(term_matches(text, term) for term in ["Medicaid", "MMIS", "Department of Health", "Department of Human Services", "Division of Medicaid", "Medical Center"]):
        score += 25
    if any(term_matches(text, term) for term in ["eligibility", "claims", "enrollment", "managed care", "interoperability", "FHIR", "prior authorization", "provider data"]):
        score += 15
    if term_matches(text, "rural health"):
        score += 25
    if any(term_matches(text, term) for term in ["RFP", "RFI", "RFQ", "software", "system", "services", "maintenance"]):
        score += 12
    if status.lower() == "open":
        score += 10
    due = parse_date(due_date)
    if due and due >= dt.date.today():
        score += 8
    return min(score, 100)


def is_open_or_recent(status: str, posted_date: str, due_date: str, days_back: int) -> bool:
    if status.lower() in {"open", "active", "upcoming"}:
        return True
    due = parse_date(due_date)
    if due and due >= dt.date.today():
        return True
    if days_back <= 0:
        return True
    posted = parse_date(posted_date)
    return not posted or (dt.date.today() - posted).days <= days_back


def valid_rows(value: Any) -> list[dict[str, Any]]:
    return [row for row in value or [] if isinstance(row, dict)]


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
