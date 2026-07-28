from __future__ import annotations

import datetime as dt
import json
import re
import time
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from typing import Any, Callable

from services.state_normalization import clean_text, compact_raw_json, iso_date, keyword_hits, parse_date, stable_id

ENTRY_URL = "https://ssl.doas.state.ga.us/PRSapp/PR_index.jsp"
BASE_URL = "https://ssl.doas.state.ga.us/gpr/"
EVENT_SEARCH_URL = urllib.parse.urljoin(BASE_URL, "eventSearch")
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
SOURCE_NOTE = "Official GPR DataTables JSON endpoint from public search page; detail pages parsed only for attachment links."
EVENT_COLUMNS = ["", "esourceNumber", "title", "agencyName", "postingDateStr", "closingDateStr", "endingIn", "status"]
EVENT_TYPE_LABELS = {
    "RFI": "GPR Request for Information",
    "RFP": "GPR Request for Proposal",
    "RFQ": "GPR Request for Quote",
    "RFQC": "GPR Request for Qualified Contractor",
    "NTC": "GPR Notice of Intent",
    "SS": "GPR Sole Source Notice",
    "NWA": "GPR Notice of Award",
    "NONST": "GPR Non-State Agency Solicitation",
    "CON": "GPR Construction Solicitation",
}


class GeorgiaGprClient:
    def __init__(self) -> None:
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor())
        self.initialized = False

    def prime(self) -> None:
        if self.initialized:
            return
        html, final_url = self._request_text(ENTRY_URL, referer=BASE_URL)
        if "/unsupported" in final_url or "/unsupported" in html[:3000]:
            raise RuntimeError("GA GPR returned unsupported-browser page")
        self.initialized = True

    def search_open_events(self, *, start: int = 0, length: int = 1000) -> dict[str, Any]:
        self.prime()
        payload = event_search_payload(start=start, length=length)
        text, _final_url = self._request_text(EVENT_SEARCH_URL, data=urllib.parse.urlencode(payload).encode("utf-8"), referer=BASE_URL)
        return json.loads(text)

    def detail_links(self, row: dict[str, Any]) -> tuple[str, list[str], list[str]]:
        detail_url = event_detail_url(row)
        if not detail_url:
            return "", [], []
        try:
            html, _final_url = self._request_text(detail_url, referer=BASE_URL)
        except Exception:
            return detail_url, [], []
        hrefs = [urllib.parse.urljoin(detail_url, href) for href in extract_hrefs(html)]
        public_events = [href for href in hrefs if "bids.sciquest.com/apps/Router/PublicEvent" in href]
        attachments = [href for href in hrefs if "downloadAttachment" in href]
        return detail_url, dedupe(public_events), dedupe(attachments)

    def _request_text(self, url: str, *, data: bytes | None = None, referer: str, timeout: int = 60, byte_limit: int = 5_000_000) -> tuple[str, str]:
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/javascript, */*; q=0.01" if data is not None else "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": referer,
        }
        if data is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"
            headers["X-Requested-With"] = "XMLHttpRequest"
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                request = urllib.request.Request(url, data=data, headers=headers)
                with self.opener.open(request, timeout=timeout) as response:
                    body = response.read(byte_limit + 1)
                    if len(body) > byte_limit:
                        raise RuntimeError(f"GA GPR response exceeded {byte_limit} bytes")
                    return body.decode("utf-8", "replace"), response.geturl()
            except (OSError, TimeoutError, json.JSONDecodeError) as exc:
                last_error = exc
                time.sleep(1 + attempt)
        raise RuntimeError(f"GA GPR request failed for {url}: {last_error}")


class LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        href = attrs_dict(attrs).get("href")
        if href:
            self.hrefs.append(href)


def fetch_opportunities(
    *,
    keywords: list[str],
    days_back: int,
    max_records: int,
    progress: Callable[[str], None] | None = None,
) -> list[dict[str, str]]:
    client = GeorgiaGprClient()
    rows = fetch_all_open_rows(client, max_records=max_records)
    emit(progress, f"GA GPR open events: {len(rows)} public rows")

    records: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in rows:
        base_record = normalize_event_row(row, keywords=keywords)
        if not base_record.get("source_record_id") or base_record["id"] in seen:
            continue
        if not is_open_or_recent(base_record["posted_date"], base_record["due_date"], days_back):
            continue
        if not useful_keyword_match(base_record["matched_keywords"].split(";"), base_record["raw_json"]):
            continue
        if keywords and not base_record["matched_keywords"]:
            continue

        detail_url, public_event_urls, attachment_urls = client.detail_links(row)
        raw = json.loads(base_record["raw_json"] or "{}")
        raw["detail_url"] = detail_url
        raw["public_event_urls"] = public_event_urls
        raw["attachment_urls"] = attachment_urls
        document_urls = public_event_urls or attachment_urls
        base_record["document_url"] = document_urls[0] if document_urls else detail_url
        base_record["raw_json"] = compact_raw_json(raw)
        seen.add(base_record["id"])
        records.append(base_record)

    return sorted(records, key=record_sort_key, reverse=True)[: max(1, max_records)]


def fetch_all_open_rows(client: GeorgiaGprClient, *, max_records: int) -> list[dict[str, Any]]:
    page_size = 1000
    first = client.search_open_events(start=0, length=page_size)
    rows = valid_rows(first.get("data"))
    total = int_or_zero(first.get("recordsFiltered") or first.get("recordsTotal"))
    max_to_scan = min(max(total, len(rows)), max(1000, max_records * 20))
    start = len(rows)
    while start < max_to_scan:
        payload = client.search_open_events(start=start, length=page_size)
        batch = valid_rows(payload.get("data"))
        if not batch:
            break
        rows.extend(batch)
        start += len(batch)
    return rows[:max_to_scan]


def event_search_payload(*, start: int, length: int) -> dict[str, str]:
    payload = {
        "draw": "1",
        "start": str(start),
        "length": str(length),
        "responseType": "ALL",
        "eventStatus": "OPEN",
        "eventIdTitle": "",
        "govType": "ALL",
        "govEntity": "",
        "catType": "ALL",
        "eventProcessType": "ALL",
        "dateRangeType": "",
        "rangeStartDate": "",
        "rangeEndDate": "",
        "isReset": "false",
        "persisted": "",
        "refreshSearchData": "false",
        "order[0][column]": "5",
        "order[0][dir]": "asc",
        "search[value]": "",
        "search[regex]": "false",
    }
    for index, column in enumerate(EVENT_COLUMNS):
        payload[f"columns[{index}][data]"] = column
        payload[f"columns[{index}][name]"] = ""
        payload[f"columns[{index}][searchable]"] = "true"
        payload[f"columns[{index}][orderable]"] = "false" if index in {0, 6, 7} else "true"
        payload[f"columns[{index}][search][value]"] = ""
        payload[f"columns[{index}][search][regex]"] = "false"
    return payload


def valid_rows(value: Any) -> list[dict[str, Any]]:
    return [row for row in value or [] if isinstance(row, dict)]


def normalize_event_row(row: dict[str, Any], *, keywords: list[str]) -> dict[str, str]:
    source_record_id = clean_text(row.get("esourceNumber") or row.get("esourceNumberKey"), 160)
    title = clean_text(row.get("title") or source_record_id, 500)
    agency = clean_text(row.get("agencyName"), 180)
    posted_date = iso_date(row.get("postingDate") or row.get("postingDateStr"))
    due_date = iso_date(row.get("closingDate") or row.get("closingDateStr"))
    status = clean_text(row.get("status") or "Open", 80)
    process_type = event_process_type(row)
    search_text = " ".join([source_record_id, title, agency, process_type, clean_text(row.get("bidProcessType"), 120), status])
    matched = keyword_hits(search_text, keywords)
    detail_url = event_detail_url(row)
    raw = dict(row)
    raw["source_key"] = "ga_procurement_registry"
    raw["source_note"] = SOURCE_NOTE

    return {
        "id": stable_id("GA", source_record_id, prefix="ga-gpr-event"),
        "state": "GA",
        "source": "Georgia Procurement Registry Open Events",
        "source_record_id": source_record_id,
        "title": title,
        "agency": agency,
        "document_type": document_type(process_type, source_record_id, title),
        "posted_date": posted_date,
        "due_date": due_date,
        "status": status,
        "amount": "",
        "document_url": detail_url,
        "source_url": BASE_URL,
        "matched_keywords": ";".join(matched),
        "relevance_score": str(relevance_score(matched, status, search_text, due_date)),
        "raw_json": compact_raw_json(raw),
        "last_checked_at": now_iso(),
    }


def event_detail_url(row: dict[str, Any]) -> str:
    event_number = clean_text(row.get("esourceNumberKey") or row.get("esourceNumber"), 180)
    source_type = clean_text(row.get("sourceId"), 80)
    if not event_number or not source_type:
        return ""
    return urllib.parse.urljoin(BASE_URL, "eventDetails?" + urllib.parse.urlencode({"eSourceNumber": event_number, "sourceSystemType": source_type}))


def event_process_type(row: dict[str, Any]) -> str:
    values = [clean_text(row.get("bidProcessType"), 120), clean_text(row.get("esourceNumber"), 160)]
    text = " ".join(values).upper()
    for code in EVENT_TYPE_LABELS:
        if re.search(rf"(?:^|[-\s]){re.escape(code)}(?:[-\s]|$)", text):
            return code
    return ""


def document_type(process_type: str, source_record_id: str, title: str) -> str:
    if process_type in EVENT_TYPE_LABELS:
        return EVENT_TYPE_LABELS[process_type]
    text = " ".join([source_record_id, title]).lower()
    if term_matches(text, "RFI") or "request for information" in text:
        return "GPR Request for Information"
    if term_matches(text, "RFP") or "request for proposal" in text:
        return "GPR Request for Proposal"
    if term_matches(text, "RFQ") or "request for quote" in text:
        return "GPR Request for Quote"
    return "GPR Solicitation"


def extract_hrefs(html: str) -> list[str]:
    parser = LinkParser()
    parser.feed(html)
    return parser.hrefs


def useful_keyword_match(matches: list[str], text: str) -> bool:
    generic_terms = {"claims", "eligibility", "enrollment"}
    matched_terms = {match.lower() for match in matches if match}
    if not matched_terms or not matched_terms <= generic_terms:
        return True
    context_terms = [
        "community health",
        "human services",
        "healthcare",
        "health care",
        "medicaid",
        "medicare",
        "medical",
        "health",
        "hospital",
        "behavioral",
        "managed care",
        "provider",
        "chip",
        "mmis",
    ]
    return any(term_matches(text, term) for term in context_terms)


def relevance_score(matches: list[str], status: str, text: str, due_date: str) -> int:
    score = min(50, len(matches) * 10)
    if any(term_matches(text, term) for term in ["Medicaid", "MMIS", "Community Health", "Human Services", "Health Care", "Healthcare"]):
        score += 25
    if any(term_matches(text, term) for term in ["eligibility", "claims", "enrollment", "managed care", "interoperability", "FHIR", "prior authorization", "provider data"]):
        score += 15
    if term_matches(text, "rural health"):
        score += 25
    if any(term_matches(text, term) for term in ["RFP", "RFI", "RFQ", "software", "data", "cloud", "platform", "services"]):
        score += 12
    if status.lower() in {"open", "posted", "upcoming"}:
        score += 10
    parsed_due = parse_date(due_date)
    if parsed_due and parsed_due >= dt.date.today():
        score += 8
    return min(score, 100)


def is_open_or_recent(posted_date: str, due_date: str, days_back: int) -> bool:
    due = parse_date(due_date)
    if due and due >= dt.date.today():
        return True
    if not posted_date:
        return True
    posted = parse_date(posted_date)
    return not posted or days_back <= 0 or (dt.date.today() - posted).days <= days_back


def record_sort_key(row: dict[str, str]) -> tuple[int, str, str]:
    return (int_or_zero(row.get("relevance_score")), row.get("due_date", ""), row.get("title", ""))


def term_matches(text: Any, term: str) -> bool:
    return bool(keyword_hits(str(text or ""), [term]))


def int_or_zero(value: Any) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


def attrs_dict(attrs: list[tuple[str, str | None]]) -> dict[str, str]:
    return {key: value or "" for key, value in attrs}


def dedupe(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def emit(progress: Callable[[str], None] | None, message: str) -> None:
    if progress:
        progress(message)
