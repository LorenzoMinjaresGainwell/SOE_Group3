from __future__ import annotations

import datetime as dt
import re
import time
import urllib.parse
from html.parser import HTMLParser
from typing import Any, Callable

from services.state_http import fetch_url
from services.state_normalization import clean_text, compact_raw_json, iso_date, keyword_hits, parse_date, stable_id, term_matches

PAGE_URL = "https://www.tn.gov/generalservices/procurement/central-procurement-office--cpo-/supplier-information/request-for-proposals--rfp--opportunities1.html"
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
SOURCE_NAME = "Tennessee CPO RFP/RFI/RFQ Opportunities"
SOURCE_NOTE = (
    "Official Tennessee Central Procurement Office public RFP opportunities HTML table. "
    "Edison supplier portal redirects to Oracle IDCS login from CLI; this tn.gov page exposes current rows and document links without login."
)
MAX_SCAN_ROWS = 300


class TableCell:
    def __init__(self) -> None:
        self.parts: list[str] = []
        self.hrefs: list[str] = []

    @property
    def text(self) -> str:
        return clean_text(" ".join(self.parts), 4000)


class TableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tables: list[list[list[TableCell]]] = []
        self.current_table: list[list[TableCell]] | None = None
        self.current_row: list[TableCell] | None = None
        self.current_cell: TableCell | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        data = attrs_dict(attrs)
        if tag == "table":
            self.current_table = []
            return
        if self.current_table is not None and tag == "tr":
            self.current_row = []
            return
        if self.current_row is not None and tag in {"td", "th"}:
            self.current_cell = TableCell()
            return
        if tag == "a" and self.current_cell is not None:
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
            return
        if tag == "tr" and self.current_row is not None and self.current_table is not None:
            if self.current_row:
                self.current_table.append(self.current_row)
            self.current_row = None
            return
        if tag == "table" and self.current_table is not None:
            self.tables.append(self.current_table)
            self.current_table = None


def fetch_opportunities(
    *,
    keywords: list[str],
    days_back: int,
    max_records: int,
    progress: Callable[[str], None] | None = None,
) -> list[dict[str, str]]:
    rows = fetch_table_rows()[:MAX_SCAN_ROWS]
    emit(progress, f"TN CPO RFP opportunities: {len(rows)} public rows")

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
        if false_keyword_hit(record) or not useful_keyword_match(record["matched_keywords"].split(";"), record["raw_json"]):
            continue
        seen.add(record["id"])
        records.append(record)

    return sorted(records, key=record_sort_key, reverse=True)[: max(1, max_records)]


def fetch_table_rows() -> list[dict[str, Any]]:
    html, final_url = http_text(PAGE_URL)
    parser = TableParser()
    parser.feed(html)

    rows: list[dict[str, Any]] = []
    for table in parser.tables:
        for cells in table:
            if len(cells) < 3:
                continue
            label = cells[0].text
            dates = parse_date_pair(cells[1].text)
            title = cells[2].text
            if not label or label.lower().startswith("document id") or not title or not dates:
                continue
            rows.append(
                {
                    "document_label": label,
                    "date_text": cells[1].text,
                    "posted_date": dates[0],
                    "due_date": dates[1],
                    "title": title,
                    "document_urls": absolute_urls(cells[0].hrefs),
                    "row_links": absolute_urls([href for cell in cells for href in cell.hrefs]),
                    "source_url": final_url or PAGE_URL,
                }
            )
    return rows


def normalize_row(row: dict[str, Any], *, keywords: list[str]) -> dict[str, str]:
    label = clean_text(row.get("document_label"), 500)
    title = clean_text(row.get("title") or label, 500)
    source_record_id = solicitation_number(label) or solicitation_number(" ".join(row.get("document_urls") or [])) or label
    posted_date = clean_text(row.get("posted_date"), 40)
    due_date = clean_text(row.get("due_date"), 40)
    status = "Open" if not is_past(due_date) else "Closed"
    document_urls = [clean_text(url, 700) for url in row.get("document_urls") or [] if clean_text(url)]
    document_url = document_urls[0] if document_urls else PAGE_URL
    search_text = expand_related_terms(" ".join([source_record_id, label, title]))
    matched = keyword_hits(search_text, keywords)
    raw = dict(row)
    raw["source_key"] = "tn_edison_supplier"
    raw["source_note"] = SOURCE_NOTE

    return {
        "id": stable_id("TN", source_record_id, title, prefix="tn-cpo-opportunity"),
        "state": "TN",
        "source": SOURCE_NAME,
        "source_record_id": source_record_id,
        "title": title,
        "agency": "Tennessee Central Procurement Office",
        "document_type": document_type(label, title, document_url),
        "posted_date": posted_date,
        "due_date": due_date,
        "status": status,
        "amount": "",
        "document_url": document_url,
        "source_url": clean_text(row.get("source_url"), 700) or PAGE_URL,
        "matched_keywords": ";".join(matched),
        "relevance_score": str(relevance_score(matched, status, search_text, due_date)),
        "raw_json": compact_raw_json(raw, limit=10000),
        "last_checked_at": now_iso(),
    }


def parse_date_pair(value: Any) -> tuple[str, str] | None:
    matches = re.findall(r"\b\d{1,2}/\d{1,2}/\d{2,4}\b", clean_text(value))
    if not matches:
        return None
    posted = iso_date(matches[0])
    due = iso_date(matches[1] if len(matches) > 1 else matches[0])
    return (posted, due) if posted or due else None


def solicitation_number(value: Any) -> str:
    text = clean_text(value, 700)
    match = re.search(r"\b\d{5}-[A-Za-z0-9-]+\b", text)
    return match.group(0) if match else ""


def document_type(label: str, title: str, document_url: str) -> str:
    text = " ".join([label, title, document_url]).upper()
    if code_matches(text, "RFI") or "REQUEST FOR INFORMATION" in text:
        return "Tennessee Request for Information"
    if code_matches(text, "RFQ") or "REQUEST FOR QUOTE" in text:
        return "Tennessee Request for Quote"
    if code_matches(text, "RFP") or "REQUEST FOR PROPOSAL" in text:
        return "Tennessee Request for Proposal"
    if "SOLICITATION" in text:
        return "Tennessee Solicitation Notice"
    return "Tennessee CPO Opportunity"


def expand_related_terms(text: str) -> str:
    expanded = text
    if any(term_matches(text, term) for term in ["TennCare", "Medicaid", "MMIS"]):
        expanded += " Medicaid MMIS managed care eligibility claims provider health care"
    if any(term_matches(text, term) for term in ["Healthcare", "Health Care", "Health", "Medical", "Hospital", "Behavioral", "EHR"]):
        expanded += " health care healthcare medical provider behavioral health"
    if any(term_matches(text, term) for term in ["Federal Poverty Level", "FPL", "PARO"]):
        expanded += " Medicaid eligibility enrollment"
    if any(term_matches(text, term) for term in ["Call Center", "Computer Assisted", "Enterprise", "System", "Software", "Application"]):
        expanded += " system software platform services"
    if any(term_matches(text, term) for term in ["Rural Health", "RHTP", "Rural"]):
        expanded += " rural health rural health transformation"
    return expanded


def useful_keyword_match(matches: list[str], text: str) -> bool:
    generic_terms = {"claims", "eligibility", "enrollment", "provider", "provider data", "quality", "cms", "interoperability"}
    matched_terms = {match.lower() for match in matches if match}
    if not matched_terms or not matched_terms <= generic_terms:
        return True
    context_terms = [
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
        "tencare",
    ]
    return any(term_matches(text, term) for term in context_terms)


def false_keyword_hit(record: dict[str, str]) -> bool:
    text = " ".join([record.get("title", ""), record.get("agency", ""), record.get("raw_json", "")])
    return "mmis" in {item.lower() for item in record.get("matched_keywords", "").split(";") if item} and term_matches(text, "commissary") and not term_matches(text, "MMIS")


def relevance_score(matches: list[str], status: str, text: str, due_date: str) -> int:
    score = min(50, len([match for match in matches if match]) * 10)
    if any(term_matches(text, term) for term in ["Medicaid", "MMIS", "TennCare", "Healthcare", "Health Care"]):
        score += 25
    if any(term_matches(text, term) for term in ["eligibility", "claims", "enrollment", "managed care", "interoperability", "FHIR", "prior authorization", "provider data"]):
        score += 15
    if any(term_matches(text, term) for term in ["rural health", "rural health transformation", "critical access hospital"]):
        score += 25
    if any(term_matches(text, term) for term in ["RFP", "RFI", "RFQ", "software", "system", "application", "platform", "services"]):
        score += 10
    if status.lower() == "open":
        score += 10
    due = parse_date(due_date)
    if due and due >= dt.date.today():
        score += 5
    return min(score, 100)


def is_open_or_recent(posted_date: str, due_date: str, days_back: int) -> bool:
    due = parse_date(due_date)
    if due and due >= dt.date.today():
        return True
    if days_back <= 0:
        return True
    posted = parse_date(posted_date)
    return not posted or (dt.date.today() - posted).days <= days_back


def is_past(value: str) -> bool:
    parsed = parse_date(value)
    return bool(parsed and parsed < dt.date.today())


def record_sort_key(row: dict[str, str]) -> tuple[int, str, str]:
    return (int_or_zero(row.get("relevance_score")), row.get("due_date", ""), row.get("posted_date", ""))


def http_text(url: str) -> tuple[str, str]:
    headers = {
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
    raise RuntimeError(f"TN CPO opportunities request failed for {url}: {last_error}")


def absolute_urls(hrefs: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for href in hrefs:
        if not href or href.lower().startswith(("javascript:", "mailto:")):
            continue
        url = urllib.parse.urljoin(PAGE_URL, clean_text(href, 700))
        if url in seen:
            continue
        seen.add(url)
        result.append(url)
    return result


def code_matches(text: str, code: str) -> bool:
    return re.search(rf"(?<![A-Z0-9]){re.escape(code)}(?![A-Z0-9])", text, re.IGNORECASE) is not None


def attrs_dict(attrs: list[tuple[str, str | None]]) -> dict[str, str]:
    return {key.lower(): value or "" for key, value in attrs}


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
