from __future__ import annotations

import datetime as dt
import re
import urllib.parse
from html.parser import HTMLParser
from typing import Any, Callable

from services.state_http import fetch_url
from services.state_normalization import clean_text, compact_raw_json, iso_date, keyword_hits, parse_date, stable_id, term_matches

PAGE_URL = "https://www.in.gov/idoa/procurement/current-business-opportunities/"
BASE_URL = "https://www.in.gov/"
USER_AGENT = "soe-group3-in-idoa-opportunities/0.1"
SOURCE_NAME = "Indiana IDOA Current Business Opportunities"
SOURCE_NOTE = "Official IDOA Current Business Opportunities public HTML/DataTables page; bid documents are linked ZIP files in the Event Name column."
EXPECTED_HEADERS = ["event name", "agency", "event id", "event description", "response due by", "contact"]


class TableCell:
    def __init__(self) -> None:
        self.parts: list[str] = []
        self.hrefs: list[str] = []

    @property
    def text(self) -> str:
        return clean_text(" ".join(self.parts), 4000)


class IndianaTableParser(HTMLParser):
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
    rows = fetch_current_rows()
    emit(progress, f"IN IDOA current opportunities: {len(rows)} public rows")

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


def fetch_current_rows() -> list[dict[str, Any]]:
    result = fetch_url(
        PAGE_URL,
        headers={"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"},
        timeout=60,
        byte_limit=1_000_000,
        user_agent=USER_AGENT,
    )
    result.raise_for_status()
    parser = IndianaTableParser()
    parser.feed(result.body_text())

    rows: list[dict[str, Any]] = []
    for table in parser.tables:
        if not table:
            continue
        headers = [clean_text(cell.text).lower() for cell in table[0]]
        if headers[: len(EXPECTED_HEADERS)] != EXPECTED_HEADERS:
            continue
        for cells in table[1:]:
            if len(cells) < len(EXPECTED_HEADERS):
                continue
            row = {EXPECTED_HEADERS[index]: cells[index].text for index in range(len(EXPECTED_HEADERS))}
            row["links"] = {EXPECTED_HEADERS[index]: absolute_links(cells[index].hrefs) for index in range(len(EXPECTED_HEADERS))}
            rows.append(row)
    return rows


def normalize_row(row: dict[str, Any], *, keywords: list[str]) -> dict[str, str]:
    source_record_id = clean_text(row.get("event id"), 160)
    title = clean_event_title(row.get("event name")) or source_record_id
    agency = clean_text(row.get("agency"), 180)
    description = clean_text(row.get("event description"), 3000)
    due_date = iso_date(row.get("response due by"))
    contact = clean_text(row.get("contact"), 500)
    document_url = document_link(row) or PAGE_URL
    status = "Open" if not due_date or not is_past(due_date) else "Closed"
    search_text = expand_related_terms(" ".join([source_record_id, title, agency, description, contact]))
    matched = keyword_hits(search_text, keywords)
    raw = {
        "source_key": "in_idoa_procurement",
        "source_note": SOURCE_NOTE,
        "row": row,
        "document_url": document_url,
    }

    return {
        "id": stable_id("IN", source_record_id or title, prefix="in-idoa-opportunity"),
        "state": "IN",
        "source": SOURCE_NAME,
        "source_record_id": source_record_id or title,
        "title": title,
        "agency": agency,
        "document_type": document_type(source_record_id, title, description),
        "posted_date": "",
        "due_date": due_date,
        "status": status,
        "amount": "",
        "document_url": document_url,
        "source_url": PAGE_URL,
        "matched_keywords": ";".join(matched),
        "relevance_score": str(relevance_score(matched, status, search_text, due_date)),
        "raw_json": compact_raw_json(raw, limit=10000),
        "last_checked_at": now_iso(),
    }


def clean_event_title(value: Any) -> str:
    title = clean_text(value, 500)
    return re.sub(r"\s+Bid Documents\s*$", "", title, flags=re.IGNORECASE).strip()


def document_link(row: dict[str, Any]) -> str:
    links = row.get("links") if isinstance(row.get("links"), dict) else {}
    event_links = [str(link) for link in links.get("event name", [])]
    for link in event_links:
        lower = link.lower()
        if "/proc/solicitations/files/" in lower or lower.endswith((".zip", ".pdf", ".doc", ".docx", ".xls", ".xlsx")):
            return link
    for key in EXPECTED_HEADERS:
        for link in links.get(key, []):
            if link and not link.startswith("mailto:"):
                return str(link)
    return ""


def absolute_links(hrefs: list[str]) -> list[str]:
    return [urllib.parse.urljoin(PAGE_URL, href) for href in hrefs if href]


def document_type(source_record_id: str, title: str, description: str) -> str:
    text = " ".join([source_record_id, title, description]).upper()
    if code_matches(text, "RFI") or "REQUEST FOR INFORMATION" in text:
        return "IDOA Request for Information"
    if code_matches(text, "RFP") or "REQUEST FOR PROPOSAL" in text:
        return "IDOA Request for Proposal"
    if code_matches(text, "RFQ") or "REQUEST FOR QUOTE" in text:
        return "IDOA Request for Quote"
    if code_matches(text, "RFS") or "REQUEST FOR SERVICES" in text:
        return "IDOA Request for Services"
    if "GRANT" in text:
        return "IDOA Grant Opportunity"
    return "IDOA Solicitation"


def expand_related_terms(text: str) -> str:
    expanded = text
    if any(term_matches(text, term) for term in ["FSSA", "Family and Social Services", "MO HealthNet"]):
        expanded += " Medicaid managed care human services eligibility claims"
    if any(term_matches(text, term) for term in ["Disability", "Rehab", "Health", "Medical", "Hospital", "Hospice"]):
        expanded += " health care medical provider"
    if term_matches(text, "MCR"):
        expanded += " managed care"
    return expanded


def useful_keyword_match(matches: list[str], text: str) -> bool:
    generic_terms = {"claims", "eligibility", "enrollment", "provider", "provider data", "workforce", "cms", "interoperability"}
    matched_terms = {match.lower() for match in matches if match}
    if not matched_terms or not matched_terms <= generic_terms:
        return True
    context_terms = [
        "family and social services",
        "fssa",
        "department of health",
        "disability and rehab",
        "medicaid",
        "medicare",
        "health care",
        "healthcare",
        "medical",
        "hospital",
        "behavioral",
        "managed care",
        "provider",
        "chip",
        "mmis",
    ]
    return any(term_matches(text, term) for term in context_terms)


def false_keyword_hit(record: dict[str, str]) -> bool:
    text = " ".join([record.get("title", ""), record.get("agency", ""), record.get("raw_json", "")])
    return "mmis" in {item.lower() for item in record.get("matched_keywords", "").split(";") if item} and term_matches(text, "commissary") and not term_matches(text, "MMIS")


def relevance_score(matches: list[str], status: str, text: str, due_date: str) -> int:
    score = min(50, len([match for match in matches if match]) * 10)
    if any(term_matches(text, term) for term in ["Medicaid", "MMIS", "FSSA", "Family and Social Services"]):
        score += 30
    if any(term_matches(text, term) for term in ["managed care", "eligibility", "claims", "provider data", "quality review"]):
        score += 18
    if any(term_matches(text, term) for term in ["interoperability", "FHIR", "prior authorization", "telehealth"]):
        score += 15
    if any(term_matches(text, term) for term in ["rural health", "critical access hospital", "hospice"]):
        score += 20
    if any(term_matches(text, term) for term in ["RFP", "RFI", "RFQ", "RFS", "software", "system", "platform", "services"]):
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
    return (int_or_zero(row.get("relevance_score")), row.get("due_date", ""), row.get("title", ""))


def code_matches(text: str, code: str) -> bool:
    return re.search(rf"(?<![A-Z0-9]){re.escape(code)}(?![A-Z0-9])", text, re.IGNORECASE) is not None


def int_or_zero(value: Any) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


def attrs_dict(attrs: list[tuple[str, str | None]]) -> dict[str, str]:
    return {key: value or "" for key, value in attrs}


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def emit(progress: Callable[[str], None] | None, message: str) -> None:
    if progress:
        progress(message)
