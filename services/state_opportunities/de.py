from __future__ import annotations

import datetime as dt
import json
import re
import time
import urllib.parse
from html.parser import HTMLParser
from typing import Any, Callable

from services.state_http import fetch_url
from services.state_normalization import clean_text, compact_raw_json, iso_date, keyword_hits, parse_date, stable_id, term_matches

BASE_URL = "https://mmp.delaware.gov/"
BIDS_PAGE_URL = urllib.parse.urljoin(BASE_URL, "Bids/")
BIDS_DATA_URL = urllib.parse.urljoin(BASE_URL, "Bids/GetBids")
BID_DETAIL_URL = urllib.parse.urljoin(BASE_URL, "Bids/GetBidDetail")
BID_DOCUMENTS_URL = urllib.parse.urljoin(BASE_URL, "Bids/GetBidDocumentList")
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
SOURCE_NOTE = "Official Delaware MyMarketplace jqGrid JSON endpoint /Bids/GetBids?status=Open with document-list HTML endpoint."
MAX_SCAN_ROWS = 1000


class LinkParser(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__()
        self.base_url = base_url
        self.current_href = ""
        self.current_text: list[str] = []
        self.links: list[dict[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        href = attrs_dict(attrs).get("href")
        if href:
            self.current_href = urllib.parse.urljoin(self.base_url, href)
            self.current_text = []

    def handle_data(self, data: str) -> None:
        if self.current_href:
            self.current_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self.current_href:
            self.links.append({"url": self.current_href, "label": clean_text(" ".join(self.current_text), 200)})
            self.current_href = ""
            self.current_text = []


def fetch_opportunities(
    *,
    keywords: list[str],
    days_back: int,
    max_records: int,
    progress: Callable[[str], None] | None = None,
) -> list[dict[str, str]]:
    rows = fetch_bid_rows(max_records=max_records)
    emit(progress, f"DE MyMarketplace open bids: {len(rows)} public rows")

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
        if not useful_keyword_match(record["matched_keywords"].split(";"), record["raw_json"]):
            continue

        raw = json.loads(record["raw_json"] or "{}")
        documents = fetch_document_links(raw.get("bid_id", ""), progress=progress)
        if documents:
            raw["documents"] = documents
            record["document_url"] = documents[0]["url"]
        record["raw_json"] = compact_raw_json(raw)
        seen.add(record["id"])
        records.append(record)

    return sorted(records, key=record_sort_key, reverse=True)[: max(1, max_records)]


def fetch_bid_rows(*, max_records: int) -> list[dict[str, Any]]:
    page_size = max(100, min(500, max_records * 50))
    rows: list[dict[str, Any]] = []
    page = 1
    total_pages = 1

    while page <= total_pages and len(rows) < MAX_SCAN_ROWS:
        payload = fetch_bid_page(page=page, rows=page_size)
        batch = valid_rows(payload.get("rows"))
        if page == 1:
            total_pages = max(1, int_or_zero(payload.get("total")) or 1)
        if not batch:
            break
        rows.extend(batch)
        page += 1
        if len(batch) < page_size:
            break
    return rows[:MAX_SCAN_ROWS]


def fetch_bid_page(*, page: int, rows: int) -> dict[str, Any]:
    url = BIDS_DATA_URL + "?" + urllib.parse.urlencode({"status": "Open"})
    payload = {
        "page": page,
        "rows": rows,
        "sidx": "OpenDate",
        "sord": "desc",
        "_search": False,
    }
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Accept-Language": "en-US,en;q=0.9",
        "Content-Type": "application/json; charset=utf-8",
        "Referer": BIDS_PAGE_URL,
        "X-Requested-With": "XMLHttpRequest",
    }
    last_error = ""
    for attempt in range(3):
        result = fetch_url(
            url,
            method="POST",
            json_data=payload,
            headers=headers,
            timeout=60,
            byte_limit=2_000_000,
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
    raise RuntimeError(f"DE MyMarketplace bid search failed: {last_error}")


def normalize_bid_row(row: dict[str, Any], *, keywords: list[str]) -> dict[str, str]:
    bid_id = clean_text(row.get("Id"), 80)
    source_record_id = clean_text(row.get("ContractNumber") or bid_id, 180)
    title = clean_text(row.get("Title") or source_record_id, 500)
    agency = agency_name(row)
    posted_date = iso_date(row.get("OpenDate"))
    due_date = iso_date(row.get("DeadlineDate"))
    status = "Open"
    detail_url = bid_detail_page_url(bid_id)
    search_text = " ".join(
        [
            source_record_id,
            title,
            agency,
            clean_text(row.get("AgencyCode"), 80),
            clean_text(row.get("ContactEmail"), 160),
            clean_text(row.get("BidUnspscCodesString"), 500),
        ]
    )
    matched = keyword_hits(search_text, keywords)
    raw = dict(row)
    raw["source_key"] = "de_mymarketplace"
    raw["source_note"] = SOURCE_NOTE
    raw["bid_id"] = bid_id
    raw["detail_url"] = detail_url

    return {
        "id": stable_id("DE", bid_id or source_record_id, prefix="de-mmp-bid"),
        "state": "DE",
        "source": "Delaware MyMarketplace Open Bids",
        "source_record_id": source_record_id,
        "title": title,
        "agency": agency,
        "document_type": document_type(source_record_id, title),
        "posted_date": posted_date,
        "due_date": due_date,
        "status": status,
        "amount": "",
        "document_url": detail_url or BIDS_PAGE_URL,
        "source_url": detail_url or BIDS_PAGE_URL,
        "matched_keywords": ";".join(matched),
        "relevance_score": str(relevance_score(matched, status, search_text, due_date)),
        "raw_json": compact_raw_json(raw),
        "last_checked_at": now_iso(),
    }


def fetch_document_links(bid_id: str, *, progress: Callable[[str], None] | None = None) -> list[dict[str, str]]:
    if not bid_id:
        return []
    url = BID_DOCUMENTS_URL + "?" + urllib.parse.urlencode({"id": bid_id, "currentCount": "0"})
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html, */*; q=0.01",
        "Referer": BIDS_PAGE_URL,
        "X-Requested-With": "XMLHttpRequest",
    }
    result = fetch_url(url, headers=headers, timeout=60, byte_limit=200_000, user_agent=USER_AGENT)
    if not result.ok:
        emit(progress, f"DE bid document list failed for {bid_id}: {result.error or result.status_code}")
        return []
    parser = LinkParser(result.final_url or url)
    parser.feed(result.body_text())
    return parser.links


def bid_detail_page_url(bid_id: str) -> str:
    if not bid_id:
        return ""
    return urllib.parse.urljoin(BASE_URL, f"Bids/Details/{urllib.parse.quote(bid_id)}")


def agency_name(row: dict[str, Any]) -> str:
    code = clean_text(row.get("AgencyCode"), 80)
    if not code:
        return ""
    names = {
        "DOE": "Delaware Department of Education",
        "DOT": "Delaware Department of Transportation",
        "DSCYF": "Delaware Department of Services for Children, Youth and Their Families",
        "DHSS": "Delaware Department of Health and Social Services",
        "NAT": "Delaware Department of Natural Resources and Environmental Control",
        "DOC": "Delaware Department of Correction",
        "DTI": "Delaware Department of Technology and Information",
    }
    return names.get(code.upper(), code)


def document_type(source_record_id: str, title: str) -> str:
    text = " ".join([source_record_id, title]).upper()
    if code_matches(text, "RFI"):
        return "Delaware Request for Information"
    if code_matches(text, "RFP"):
        return "Delaware Request for Proposal"
    if code_matches(text, "RFQ"):
        return "Delaware Request for Quote"
    if code_matches(text, "ITB"):
        return "Delaware Invitation to Bid"
    return "Delaware Bid Solicitation"


def useful_keyword_match(matches: list[str], text: str) -> bool:
    ambiguous_terms = {"claims", "eligibility", "enrollment", "cms"}
    matched_terms = {match.lower() for match in matches if match}
    if not matched_terms or not matched_terms <= ambiguous_terms:
        return True
    context_terms = [
        "department of health",
        "health and social services",
        "medicaid",
        "medicare",
        "medical",
        "behavioral health",
        "managed care",
        "provider",
        "chip",
        "mmis",
        "health care",
    ]
    return any(term_matches(text, term) for term in context_terms)


def relevance_score(matches: list[str], status: str, text: str, due_date: str) -> int:
    score = min(50, len([match for match in matches if match]) * 10)
    if any(term_matches(text, term) for term in ["Medicaid", "MMIS", "Department of Health", "Health and Social Services"]):
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


def is_open_or_recent(posted_date: str, due_date: str, days_back: int) -> bool:
    due = parse_date(due_date)
    if due and due >= dt.date.today():
        return True
    if days_back <= 0:
        return True
    posted = parse_date(posted_date)
    return not posted or (dt.date.today() - posted).days <= days_back


def valid_rows(value: Any) -> list[dict[str, Any]]:
    return [row for row in value or [] if isinstance(row, dict)]


def attrs_dict(attrs: list[tuple[str, str | None]]) -> dict[str, str]:
    return {key.lower(): value or "" for key, value in attrs}


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
