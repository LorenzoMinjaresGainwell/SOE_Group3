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

BASE_URL = "https://www.maine.gov/dafs/bbm/procurementservices/"
RFP_URL = urllib.parse.urljoin(BASE_URL, "vendors/rfps")
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
SOURCE_NOTE = "Official Maine Procurement Services RFP table; document links are captured from public table cells."


class TableCell:
    def __init__(self) -> None:
        self.parts: list[str] = []
        self.hrefs: list[str] = []

    @property
    def text(self) -> str:
        return clean_text(" ".join(self.parts), 2000)


class TableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.current_row: list[TableCell] | None = None
        self.current_cell: TableCell | None = None
        self.rows: list[list[TableCell]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        data = attrs_dict(attrs)
        if tag == "tr":
            self.current_row = []
        elif tag in {"td", "th"} and self.current_row is not None:
            self.current_cell = TableCell()
        elif tag == "a" and self.current_cell is not None:
            href = data.get("href")
            if href:
                self.current_cell.hrefs.append(href)
        elif tag == "br" and self.current_cell is not None:
            self.current_cell.parts.append(" ")

    def handle_data(self, data: str) -> None:
        if self.current_cell is not None:
            self.current_cell.parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"td", "th"} and self.current_cell is not None and self.current_row is not None:
            self.current_row.append(self.current_cell)
            self.current_cell = None
        elif tag == "tr" and self.current_row is not None:
            if self.current_row:
                self.rows.append(self.current_row)
            self.current_row = None


def fetch_opportunities(
    *,
    keywords: list[str],
    days_back: int,
    max_records: int,
    progress: Callable[[str], None] | None = None,
) -> list[dict[str, str]]:
    html, final_url = http_text(RFP_URL)
    rows = parse_rfp_rows(html)
    emit(progress, f"ME Procurement Services RFPs: {len(rows)} public rows")

    records: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in rows:
        record = normalize_rfp_row(row, source_url=final_url or RFP_URL, keywords=keywords)
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


def parse_rfp_rows(html: str) -> list[dict[str, Any]]:
    parser = TableParser()
    parser.feed(html)
    parsed: list[dict[str, Any]] = []

    for cells in parser.rows:
        if len(cells) < 7:
            continue
        if cells[0].text.lower() == "title" or cells[1].text.lower() in {"rfp #", "rfp"}:
            continue
        title_cell = cells[0]
        amendment_cell = cell_at(cells, 4)
        vendor_cell = cell_at(cells, 7)
        document_urls = absolute_urls(title_cell.hrefs + amendment_cell.hrefs + vendor_cell.hrefs)
        parsed.append(
            {
                "title": title_cell.text,
                "rfp_number": clean_text(cells[1].text, 160),
                "agency": clean_text(cells[2].text, 180),
                "posted_date": cells[3].text,
                "amendment_text": amendment_cell.text,
                "amendment_urls": absolute_urls(amendment_cell.hrefs),
                "due_date": cells[5].text,
                "status": cells[6].text,
                "awarded_vendor": vendor_cell.text,
                "award_urls": absolute_urls(vendor_cell.hrefs),
                "next_anticipated_release": cell_at(cells, 8).text,
                "document_urls": document_urls,
            }
        )
    return parsed


def normalize_rfp_row(row: dict[str, Any], *, source_url: str, keywords: list[str]) -> dict[str, str]:
    source_record_id = clean_text(row.get("rfp_number"), 160)
    title = clean_text(row.get("title") or source_record_id, 500)
    agency = clean_text(row.get("agency"), 180)
    posted_date = iso_date(row.get("posted_date"))
    due_date = iso_date(row.get("due_date"))
    status = clean_text(row.get("status") or status_from_dates(due_date), 120)
    document_urls = [clean_text(url, 500) for url in row.get("document_urls") or [] if clean_text(url)]
    search_text = " ".join(
        [
            source_record_id,
            title,
            agency,
            status,
            clean_text(row.get("amendment_text"), 1000),
            clean_text(row.get("awarded_vendor"), 500),
            clean_text(row.get("next_anticipated_release"), 120),
        ]
    )
    matched = keyword_hits(search_text, keywords)
    raw = dict(row)
    raw["source_key"] = "me_procurement_rfps"
    raw["source_note"] = SOURCE_NOTE

    return {
        "id": stable_id("ME", source_record_id, prefix="me-rfp"),
        "state": "ME",
        "source": "Maine Procurement Services RFPs",
        "source_record_id": source_record_id,
        "title": title,
        "agency": agency,
        "document_type": document_type(source_record_id, title, status),
        "posted_date": posted_date,
        "due_date": due_date,
        "status": status,
        "amount": "",
        "document_url": document_urls[0] if document_urls else RFP_URL,
        "source_url": source_url or RFP_URL,
        "matched_keywords": ";".join(matched),
        "relevance_score": str(relevance_score(matched, status, search_text, due_date)),
        "raw_json": compact_raw_json(raw),
        "last_checked_at": now_iso(),
    }


def document_type(source_record_id: str, title: str, status: str) -> str:
    text = " ".join([source_record_id, title, status]).upper()
    if code_matches(text, "RFI"):
        return "Maine Request for Information"
    if code_matches(text, "RFQ"):
        return "Maine Request for Quote"
    if "AWARD" in text:
        return "Maine RFP Award Notice"
    return "Maine Request for Proposal"


def status_from_dates(due_date: str) -> str:
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
        "department of health and human services",
        "health and human services",
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
        "dhhs",
    ]
    return any(term_matches(text, term) for term in context_terms)


def relevance_score(matches: list[str], status: str, text: str, due_date: str) -> int:
    score = min(50, len([match for match in matches if match]) * 10)
    if any(term_matches(text, term) for term in ["Medicaid", "MMIS", "Department of Health and Human Services", "DHHS"]):
        score += 25
    if any(term_matches(text, term) for term in ["eligibility", "claims", "enrollment", "managed care", "interoperability", "FHIR", "prior authorization", "provider data"]):
        score += 15
    if term_matches(text, "rural health"):
        score += 25
    if any(term_matches(text, term) for term in ["RFP", "RFI", "RFQ", "services", "system", "program"]):
        score += 12
    if "open" in status.lower():
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


def http_text(url: str) -> tuple[str, str]:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    last_error = ""
    for attempt in range(3):
        result = fetch_url(url, headers=headers, timeout=60, byte_limit=2_000_000, user_agent=USER_AGENT)
        if result.ok:
            return result.body_text(), result.final_url
        last_error = result.error or f"HTTP {result.status_code}"
        time.sleep(1 + attempt)
    raise RuntimeError(f"ME Procurement Services request failed for {url}: {last_error}")


def cell_at(cells: list[TableCell], index: int) -> TableCell:
    return cells[index] if index < len(cells) else TableCell()


def absolute_urls(hrefs: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for href in hrefs:
        if not href or href.lower().startswith("javascript:"):
            continue
        url = urllib.parse.urljoin(RFP_URL, clean_text(href, 500))
        if url in seen:
            continue
        seen.add(url)
        result.append(url)
    return result


def attrs_dict(attrs: list[tuple[str, str | None]]) -> dict[str, str]:
    return {key: value or "" for key, value in attrs}


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
