from __future__ import annotations

import datetime as dt
import html
import re
import time
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from typing import Any, Callable

from services.state_normalization import clean_text, compact_raw_json, keyword_hits, parse_date, stable_id, term_matches

PUBLIC_VIEW_URL = "https://rfp.alabama.gov/PublicView.aspx"
COMPTROLLER_SOURCE_URL = "https://comptroller.alabama.gov/rfp-database/"
USER_AGENT = "Mozilla/5.0 soe-group3-al-rfp-opportunities/0.1"
SOURCE_NAME = "Alabama RFP Public View"
SOURCE_NOTE = (
    "Official Alabama Department of Finance Comptroller RFP database PublicView.aspx; "
    "open rows are selected by the ASP.NET Status=Open form filter."
)


class FormParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.fields: dict[str, str] = {}
        self.current_select = ""
        self.select_has_selected = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        data = attrs_dict(attrs)
        if tag == "input":
            name = data.get("name", "")
            input_type = data.get("type", "text").lower()
            if name and input_type not in {"submit", "button", "image", "reset"}:
                self.fields[name] = html.unescape(data.get("value", ""))
            return
        if tag == "select":
            self.current_select = data.get("name", "")
            self.select_has_selected = False
            return
        if tag == "option" and self.current_select:
            value = html.unescape(data.get("value", ""))
            if "selected" in data or (not self.select_has_selected and self.current_select not in self.fields):
                self.fields[self.current_select] = value
                self.select_has_selected = "selected" in data

    def handle_endtag(self, tag: str) -> None:
        if tag == "select":
            self.current_select = ""
            self.select_has_selected = False


class TableCell:
    def __init__(self) -> None:
        self.parts: list[str] = []
        self.hrefs: list[str] = []

    @property
    def text(self) -> str:
        return clean_text(" ".join(self.parts), 3000)


class TableParser(HTMLParser):
    def __init__(self, table_id: str, base_url: str) -> None:
        super().__init__()
        self.table_id = table_id
        self.base_url = base_url
        self.in_table = False
        self.depth = 0
        self.current_row: list[TableCell] | None = None
        self.current_cell: TableCell | None = None
        self.rows: list[list[TableCell]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        data = attrs_dict(attrs)
        if tag == "table" and data.get("id") == self.table_id:
            self.in_table = True
            self.depth = 1
            return
        if self.in_table and tag == "table":
            self.depth += 1
        if not self.in_table:
            return
        if tag == "tr":
            self.current_row = []
        elif tag in {"td", "th"} and self.current_row is not None:
            self.current_cell = TableCell()
        elif tag == "a" and self.current_cell is not None:
            href = data.get("href")
            if href:
                self.current_cell.hrefs.append(urllib.parse.urljoin(self.base_url, html.unescape(href)))
        elif tag == "br" and self.current_cell is not None:
            self.current_cell.parts.append(" ")

    def handle_data(self, data: str) -> None:
        if self.current_cell is not None:
            self.current_cell.parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if not self.in_table:
            return
        if tag in {"td", "th"} and self.current_cell is not None and self.current_row is not None:
            self.current_row.append(self.current_cell)
            self.current_cell = None
            return
        if tag == "tr" and self.current_row is not None:
            if self.current_row:
                self.rows.append(self.current_row)
            self.current_row = None
            return
        if tag == "table":
            self.depth -= 1
            if self.depth <= 0:
                self.in_table = False


def fetch_opportunities(
    *,
    keywords: list[str],
    days_back: int,
    max_records: int,
    progress: Callable[[str], None] | None = None,
) -> list[dict[str, str]]:
    html_text = fetch_open_search_page()
    legacy_rows = parse_legacy_rows(html_text)
    staars_rows = parse_staars_rows(html_text)
    rows = legacy_rows + staars_rows
    emit(progress, f"AL RFP public view open rows: {len(rows)} public rows ({len(legacy_rows)} legacy, {len(staars_rows)} STAARS)")

    records: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in rows:
        record = normalize_row(row, keywords=keywords)
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


def fetch_open_search_page() -> str:
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor())
    initial = request_text(opener, PUBLIC_VIEW_URL)
    fields = parse_form_fields(initial)
    fields.update(
        {
            "__EVENTTARGET": "",
            "__EVENTARGUMENT": "",
            "ctl00$MyContent$ddlStatus": "1",
            "ctl00$MyContent$bttnSearch": "Search",
        }
    )
    body = urllib.parse.urlencode(fields).encode("utf-8")
    return request_text(opener, PUBLIC_VIEW_URL, data=body, referer=PUBLIC_VIEW_URL)


def request_text(opener: urllib.request.OpenerDirector, url: str, *, data: bytes | None = None, referer: str = "") -> str:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    if data is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    if referer:
        headers["Referer"] = referer
    request = urllib.request.Request(url, data=data, headers=headers)
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            with opener.open(request, timeout=60) as response:
                return response.read().decode("utf-8", "replace")
        except (OSError, TimeoutError) as exc:
            last_error = exc
            time.sleep(1 + attempt)
    raise RuntimeError(f"AL RFP public view request failed: {last_error}")


def parse_form_fields(html_text: str) -> dict[str, str]:
    parser = FormParser()
    parser.feed(html_text)
    return parser.fields


def parse_legacy_rows(html_text: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for cells in table_rows(html_text, "MyContent_GridViewRFP"):
        if len(cells) < 9 or cells[2].text.lower() == "agency" or is_pager_row(cells):
            continue
        status = clean_text(cells[5].text, 80)
        if status.lower() != "open":
            continue
        rows.append(
            {
                "source_table": "legacy_rfp",
                "source_record_id": cells[0].text,
                "title": strip_description(cells[1].text),
                "agency": cells[2].text,
                "agency_link": first_http_href(cells[3].hrefs) or cells[3].text,
                "agency_number": cells[4].text,
                "status": status,
                "category": cells[6].text,
                "subcategory": cells[7].text,
                "document_url": first_http_href(cells[3].hrefs) or PUBLIC_VIEW_URL,
            }
        )
    return rows


def parse_staars_rows(html_text: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for cells in table_rows(html_text, "MyContent_gvSTAARSRFP"):
        if len(cells) < 6 or cells[2].text.lower() == "agency" or is_pager_row(cells):
            continue
        status = clean_text(cells[3].text, 80)
        if status.lower() != "open":
            continue
        rows.append(
            {
                "source_table": "staars_rfp",
                "source_record_id": cells[0].text,
                "title": strip_description(cells[1].text),
                "agency": cells[2].text,
                "status": status.title(),
                "commodity": cells[4].text,
                "document_url": first_http_href(cells[0].hrefs) or PUBLIC_VIEW_URL,
            }
        )
    return rows


def table_rows(html_text: str, table_id: str) -> list[list[TableCell]]:
    parser = TableParser(table_id, PUBLIC_VIEW_URL)
    parser.feed(html_text)
    return parser.rows


def normalize_row(row: dict[str, Any], *, keywords: list[str]) -> dict[str, str]:
    source_record_id = clean_text(row.get("source_record_id"), 180)
    title = clean_text(row.get("title") or source_record_id, 500)
    agency = clean_text(row.get("agency"), 180)
    status = clean_text(row.get("status") or "Open", 80).title()
    document_url = clean_text(row.get("document_url"), 500) or PUBLIC_VIEW_URL
    source_table = clean_text(row.get("source_table"), 80)
    category_text = " ".join(clean_text(row.get(key), 500) for key in ("category", "subcategory", "commodity") if clean_text(row.get(key)))
    search_text = expand_related_terms(" ".join([source_record_id, title, agency, status, category_text, clean_text(row.get("agency_number"), 160)]))
    matched = keyword_hits(search_text, keywords)
    raw = dict(row)
    raw["source_key"] = "al_alabama_buys"
    raw["source_note"] = SOURCE_NOTE
    raw["source_page"] = PUBLIC_VIEW_URL
    raw["discovery_page"] = COMPTROLLER_SOURCE_URL

    return {
        "id": stable_id("AL", source_table, source_record_id, prefix="al-rfp-opportunity"),
        "state": "AL",
        "source": SOURCE_NAME,
        "source_record_id": source_record_id,
        "title": title,
        "agency": agency,
        "document_type": document_type(source_record_id, title, source_table),
        "posted_date": "",
        "due_date": "",
        "status": status,
        "amount": "",
        "document_url": document_url,
        "source_url": PUBLIC_VIEW_URL,
        "matched_keywords": ";".join(matched),
        "relevance_score": str(relevance_score(matched, status, search_text)),
        "raw_json": compact_raw_json(raw),
        "last_checked_at": now_iso(),
    }


def strip_description(value: Any) -> str:
    return clean_text(re.sub(r"^\s*Description\s*:\s*", "", clean_text(value, 1000), flags=re.IGNORECASE), 500)


def first_http_href(hrefs: list[str]) -> str:
    for href in hrefs:
        if href.lower().startswith(("http://", "https://")):
            return href
    return ""


def is_pager_row(cells: list[TableCell]) -> bool:
    texts = [cell.text for cell in cells if cell.text]
    return bool(texts) and all(text == "..." or text.isdigit() for text in texts)


def document_type(source_record_id: str, title: str, source_table: str) -> str:
    text = " ".join([source_record_id, title]).upper()
    if "REQUEST FOR QUALIFICATION" in text:
        return "Alabama Request for Qualifications"
    if "REQUEST FOR INFORMATION" in text or re.search(r"\bRFI\b", text):
        return "Alabama Request for Information"
    if "REQUEST FOR PROPOSAL" in text or re.search(r"\bRFP\b", text):
        return "Alabama Request for Proposal"
    if re.search(r"\bRFQ\b", text):
        return "Alabama Request for Quote"
    if "staars" in source_table:
        return "Alabama STAARS RFP"
    return "Alabama RFP"


def expand_related_terms(text: str) -> str:
    expanded = text
    if term_matches(text, "Medicaid"):
        expanded += " health care provider managed care claims eligibility enrollment"
    if any(term_matches(text, term) for term in ["Public Health", "Mental Health", "Human Services", "Health"]):
        expanded += " health care behavioral health provider"
    if any(term_matches(text, term) for term in ["Claims Processing", "Dental", "Vision", "Pharmacy", "Opioid"]):
        expanded += " health care medical provider claims"
    return expanded


def useful_keyword_match(matches: list[str], text: str) -> bool:
    generic_terms = {"claims", "eligibility", "enrollment", "provider", "provider data", "workforce", "cms", "interoperability"}
    matched_terms = {match.lower() for match in matches if match}
    if not matched_terms or not matched_terms <= generic_terms:
        return True
    context_terms = [
        "medicaid",
        "public health",
        "mental health",
        "human services",
        "health care",
        "healthcare",
        "medical",
        "hospital",
        "behavioral",
        "managed care",
        "provider",
        "chip",
        "mmis",
        "pharmacy",
        "claims processing",
        "dental",
        "vision",
    ]
    return any(term_matches(text, term) for term in context_terms)


def relevance_score(matches: list[str], status: str, text: str) -> int:
    score = min(50, len([match for match in matches if match]) * 10)
    if any(term_matches(text, term) for term in ["Medicaid", "MMIS", "Health Care", "Public Health", "Mental Health"]):
        score += 25
    if any(term_matches(text, term) for term in ["eligibility", "claims", "enrollment", "managed care", "interoperability", "FHIR", "prior authorization", "provider data"]):
        score += 15
    if term_matches(text, "rural health"):
        score += 25
    if any(term_matches(text, term) for term in ["RFP", "RFQ", "services", "system", "program", "software", "data"]):
        score += 12
    if status.lower() == "open":
        score += 10
    return min(score, 100)


def is_open_or_recent(posted_date: str, due_date: str, days_back: int) -> bool:
    due = parse_date(due_date)
    if due and due >= dt.date.today():
        return True
    if not due_date and not posted_date:
        return True
    if days_back <= 0:
        return True
    posted = parse_date(posted_date)
    return not posted or (dt.date.today() - posted).days <= days_back


def record_sort_key(row: dict[str, str]) -> tuple[int, str, str]:
    return (int_or_zero(row.get("relevance_score")), row.get("due_date", ""), row.get("title", ""))


def attrs_dict(attrs: list[tuple[str, str | None]]) -> dict[str, str]:
    return {name.lower(): value or "" for name, value in attrs}


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
