from __future__ import annotations

import datetime as dt
import re
import urllib.parse
from html.parser import HTMLParser
from typing import Any, Callable

from services.state_http import fetch_url
from services.state_normalization import clean_text, compact_raw_json, iso_date, keyword_hits, parse_date, stable_id, term_matches

PAGE_URL = "https://das.nebraska.gov/materiel/bid-opportunities.html"
USER_AGENT = "soe-group3-ne-das-opportunities/0.1"
SOURCE_NAME = "Nebraska DAS Materiel Bid Opportunities"
SOURCE_NOTE = (
    "Official Nebraska DAS Materiel Bid Opportunities static HTML table. The prior BuySpeed/BSO host candidate "
    "nebraska.buyspeed.com failed DNS from CLI, and the official DAS page links this public table instead."
)
EXPECTED_HEADERS = ["posted", "description", "category", "opening", "type", "buyer", "solicitation", "agency", "updated"]


class TableCell:
    def __init__(self) -> None:
        self.parts: list[str] = []
        self.hrefs: list[str] = []

    @property
    def text(self) -> str:
        return clean_text(" ".join(self.parts), 4000)


class NebraskaTableParser(HTMLParser):
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
    rows = fetch_bid_rows()
    emit(progress, f"NE DAS Materiel bid opportunities: {len(rows)} public rows")

    records: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in rows:
        record = normalize_row(row, keywords=keywords)
        if not record.get("source_record_id") or record["id"] in seen:
            continue
        if not is_open_or_recent(record["posted_date"], record["due_date"], row.get("opening", ""), days_back):
            continue
        if keywords and not record["matched_keywords"]:
            continue
        if false_keyword_hit(record) or not useful_keyword_match(record["matched_keywords"].split(";"), record["raw_json"]):
            continue
        seen.add(record["id"])
        records.append(record)

    return sorted(records, key=record_sort_key, reverse=True)[: max(1, max_records)]


def fetch_bid_rows() -> list[dict[str, Any]]:
    result = fetch_url(
        PAGE_URL,
        headers={"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"},
        timeout=60,
        byte_limit=2_000_000,
        user_agent=USER_AGENT,
    )
    result.raise_for_status()
    parser = NebraskaTableParser()
    parser.feed(result.body_text())

    rows: list[dict[str, Any]] = []
    for table in parser.tables:
        if not table:
            continue
        headers: list[str] = []
        for cells in table:
            texts = [cell.text for cell in cells]
            normalized_headers = normalize_headers(texts)
            if is_header_row(normalized_headers):
                headers = normalized_headers
                continue
            if not headers or len(cells) < len(headers):
                continue
            row = {headers[index]: cells[index].text for index in range(len(headers))}
            row["links"] = {headers[index]: absolute_links(cells[index].hrefs) for index in range(len(headers))}
            if row.get("description") and row.get("solicitation"):
                rows.append(row)
    return rows


def normalize_headers(values: list[str]) -> list[str]:
    normalized: list[str] = []
    for value in values:
        lower = clean_text(value).lower()
        if lower.startswith("category"):
            normalized.append("category")
        elif lower.startswith("opening"):
            normalized.append("opening")
        elif lower in {"pco/buyer", "buyer"}:
            normalized.append("buyer")
        elif lower.startswith("solicitation"):
            normalized.append("solicitation")
        elif lower in {"updated", "last updated", "last revised"}:
            normalized.append("updated")
        else:
            normalized.append(lower)
    return normalized


def is_header_row(headers: list[str]) -> bool:
    return len(headers) >= 8 and headers[0] == "posted" and "description" in headers and "solicitation" in headers


def normalize_row(row: dict[str, Any], *, keywords: list[str]) -> dict[str, str]:
    source_record_id = clean_text(row.get("solicitation"), 180)
    title = clean_text(row.get("description") or source_record_id, 500)
    agency = clean_text(row.get("agency"), 180)
    category = clean_text(row.get("category"), 500)
    buyer = clean_text(row.get("buyer"), 300)
    posted_date = iso_date(row.get("posted"))
    due_date = iso_date(row.get("opening"))
    updated_date = iso_date(row.get("updated"))
    status = status_from_opening(row.get("opening", ""), due_date)
    document_url = document_link(row) or PAGE_URL
    search_text = expand_related_terms(" ".join([source_record_id, title, agency, category, buyer, row.get("type", "")]))
    matched = keyword_hits(search_text, keywords)
    raw = {
        "source_key": "ne_materiel_purchasing",
        "source_note": SOURCE_NOTE,
        "page_url": PAGE_URL,
        "updated_date": updated_date,
        "row": row,
    }

    return {
        "id": stable_id("NE", source_record_id or title, prefix="ne-das-bid"),
        "state": "NE",
        "source": SOURCE_NAME,
        "source_record_id": source_record_id,
        "title": title,
        "agency": agency,
        "document_type": document_type(row.get("type", ""), source_record_id, title),
        "posted_date": posted_date,
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


def document_link(row: dict[str, Any]) -> str:
    links = row.get("links") if isinstance(row.get("links"), dict) else {}
    for key in EXPECTED_HEADERS:
        for link in links.get(key, []):
            if link and not link.startswith("mailto:"):
                return str(link)
    return ""


def absolute_links(hrefs: list[str]) -> list[str]:
    return [urllib.parse.urljoin(PAGE_URL, href) for href in hrefs if href]


def document_type(event_type: Any, source_record_id: str, title: str) -> str:
    kind = clean_text(event_type, 160)
    text = " ".join([kind, source_record_id, title]).upper()
    if "REQUEST FOR INFORMATION" in text or code_matches(text, "RFI"):
        return "Nebraska DAS Request for Information"
    if "REQUEST FOR PROPOS" in text or code_matches(text, "RFP"):
        return "Nebraska DAS Request for Proposal"
    if "REQUEST FOR QUALIFICATION" in text or code_matches(text, "RFQ"):
        return "Nebraska DAS Request for Qualifications"
    if "INVITATION" in text or code_matches(text, "ITB"):
        return "Nebraska DAS Invitation to Bid"
    if kind:
        return f"Nebraska DAS {kind}"
    return "Nebraska DAS Bid Opportunity"


def expand_related_terms(text: str) -> str:
    expanded = text
    if any(term_matches(text, term) for term in ["Department of Health and Human Services", "DHHS", "Medicaid", "MMIS"]):
        expanded += " Medicaid MMIS managed care eligibility claims human services provider health care"
    if any(term_matches(text, term) for term in ["Health", "Medical", "Hospital", "Behavioral", "Child Support"]):
        expanded += " health care medical provider human services"
    if any(term_matches(text, term) for term in ["Rural", "Vocational Rehabilitation", "Workforce"]):
        expanded += " rural health workforce"
    return expanded


def useful_keyword_match(matches: list[str], text: str) -> bool:
    generic_terms = {"claims", "eligibility", "enrollment", "provider", "provider data", "workforce", "cms", "interoperability"}
    matched_terms = {match.lower() for match in matches if match}
    if not matched_terms or not matched_terms <= generic_terms:
        return True
    context_terms = [
        "department of health and human services",
        "dhhs",
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
    ]
    return any(term_matches(text, term) for term in context_terms)


def false_keyword_hit(record: dict[str, str]) -> bool:
    text = " ".join([record.get("title", ""), record.get("agency", ""), record.get("raw_json", "")])
    return "mmis" in {item.lower() for item in record.get("matched_keywords", "").split(";") if item} and term_matches(text, "commissary") and not term_matches(text, "MMIS")


def relevance_score(matches: list[str], status: str, text: str, due_date: str) -> int:
    score = min(50, len([match for match in matches if match]) * 10)
    if any(term_matches(text, term) for term in ["Medicaid", "MMIS", "Department of Health and Human Services", "DHHS"]):
        score += 30
    if any(term_matches(text, term) for term in ["managed care", "eligibility", "claims", "provider data", "child support"]):
        score += 18
    if any(term_matches(text, term) for term in ["interoperability", "FHIR", "prior authorization", "system", "software"]):
        score += 15
    if any(term_matches(text, term) for term in ["rural health", "vocational rehabilitation", "workforce"]):
        score += 20
    if any(term_matches(text, term) for term in ["RFP", "RFI", "RFQ", "ITB", "data", "platform", "services"]):
        score += 10
    if status.lower() in {"open", "continuous"}:
        score += 10
    due = parse_date(due_date)
    if due and due >= dt.date.today():
        score += 5
    return min(score, 100)


def is_open_or_recent(posted_date: str, due_date: str, opening_raw: str, days_back: int) -> bool:
    if clean_text(opening_raw).lower() == "continuous":
        return True
    due = parse_date(due_date)
    if due and due >= dt.date.today():
        return True
    if days_back <= 0:
        return True
    posted = parse_date(posted_date)
    return not posted or (dt.date.today() - posted).days <= days_back


def status_from_opening(opening_raw: Any, due_date: str) -> str:
    if clean_text(opening_raw).lower() == "continuous":
        return "Continuous"
    due = parse_date(due_date)
    if due and due < dt.date.today():
        return "Closed"
    return "Open"


def record_sort_key(row: dict[str, str]) -> tuple[int, str, str]:
    return (int_or_zero(row.get("relevance_score")), row.get("due_date", ""), row.get("posted_date", ""))


def code_matches(text: str, code: str) -> bool:
    return re.search(rf"(?<![A-Z0-9]){re.escape(code)}(?![A-Z0-9])", text, re.IGNORECASE) is not None


def attrs_dict(attrs: list[tuple[str, str | None]]) -> dict[str, str]:
    return {key: value or "" for key, value in attrs}


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
