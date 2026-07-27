from __future__ import annotations

import datetime as dt
import re
import urllib.parse
from html.parser import HTMLParser
from typing import Any, Callable

from services.state_http import fetch_url
from services.state_normalization import clean_text, compact_raw_json, iso_date, keyword_hits, parse_date, stable_id, term_matches

PAGE_URL = "https://dgs.maryland.gov/Pages/Procurement/BidsAwards.aspx"
EMMA_URL = "https://emma.maryland.gov/page.aspx/en/rfp/request_browse_public"
USER_AGENT = "soe-group3-md-dgs-opportunities/0.1"
SOURCE_NAME = "Maryland DGS Open Bids and Contract Awards"
SOURCE_NOTE = (
    "Official Maryland DGS SharePoint page exposes a public Open Bids and Contract Awards table. "
    "eMMA public solicitation routes remain browser-check/reCAPTCHA gated, so this adapter uses the DGS public table only."
)
LABELS = [
    "Description",
    "Category",
    "Contract Type",
    "Status",
    "Procurement Officer",
    "Vendor",
    "Bid No",
    "ITB/Project No",
    "Bid Closing Date",
    "Pre-Bid",
    "MBE",
    "SBR",
    "Maryland Owned",
    "Veteran Owned",
    "BPO No",
    "Award Start Date",
    "Award End Date",
]
LABEL_RE = re.compile(rf"^({'|'.join(re.escape(label) for label in LABELS)}):\s*(.*)$", re.IGNORECASE)


class TableCell:
    def __init__(self) -> None:
        self.parts: list[str] = []
        self.hrefs: list[tuple[str, str]] = []

    @property
    def text(self) -> str:
        return "".join(self.parts)


class MarylandBidsParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.in_table = False
        self.table_depth = 0
        self.current_row: list[TableCell] | None = None
        self.current_cell: TableCell | None = None
        self.current_link_href = ""
        self.current_link_text: list[str] = []
        self.rows: list[TableCell] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        data = attrs_dict(attrs)
        if tag == "table" and data.get("id") == "DGSBidAward":
            self.in_table = True
            self.table_depth = 1
            return
        if self.in_table and tag == "table":
            self.table_depth += 1
        if not self.in_table:
            return
        if tag == "tr":
            self.current_row = []
            return
        if tag in {"td", "th"} and self.current_row is not None:
            self.current_cell = TableCell()
            return
        if tag in {"br", "b", "div"} and self.current_cell is not None:
            self.current_cell.parts.append("\n")
        if tag == "a" and self.current_cell is not None:
            href = data.get("href") or ""
            if href:
                self.current_link_href = href
                self.current_link_text = []

    def handle_data(self, data: str) -> None:
        if self.current_cell is not None:
            self.current_cell.parts.append(data)
        if self.current_link_href:
            self.current_link_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if not self.in_table:
            return
        if tag == "a" and self.current_link_href and self.current_cell is not None:
            self.current_cell.hrefs.append((self.current_link_href, clean_text(" ".join(self.current_link_text), 500)))
            self.current_link_href = ""
            self.current_link_text = []
            return
        if tag in {"td", "th"} and self.current_cell is not None and self.current_row is not None:
            self.current_row.append(self.current_cell)
            self.current_cell = None
            return
        if tag == "tr" and self.current_row is not None:
            for cell in self.current_row:
                if clean_text(cell.text):
                    self.rows.append(cell)
            self.current_row = None
            return
        if tag == "table":
            self.table_depth -= 1
            if self.table_depth <= 0:
                self.in_table = False


def fetch_opportunities(
    *,
    keywords: list[str],
    days_back: int,
    max_records: int,
    progress: Callable[[str], None] | None = None,
) -> list[dict[str, str]]:
    rows = fetch_bid_award_rows()
    open_rows = [row for row in rows if clean_text(row.get("Status"), 80).lower() == "open"]
    emit(progress, f"MD DGS BidsAwards table: {len(rows)} public rows; {len(open_rows)} open rows")

    records: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in open_rows:
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


def fetch_bid_award_rows() -> list[dict[str, Any]]:
    result = fetch_url(
        PAGE_URL,
        headers={"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"},
        timeout=60,
        byte_limit=2_000_000,
        user_agent=USER_AGENT,
    )
    result.raise_for_status()
    parser = MarylandBidsParser()
    parser.feed(result.body_text())

    rows: list[dict[str, Any]] = []
    for cell in parser.rows:
        values = label_values(cell.text)
        if not values.get("Description") and not values.get("Status"):
            continue
        values["links"] = absolute_links(cell.hrefs)
        rows.append(values)
    return rows


def label_values(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    current = ""
    parts: list[str] = []
    for line in normalized_lines(text):
        match = LABEL_RE.match(line)
        if match:
            if current:
                values[current] = clean_text(" ".join(parts), 1000)
            current = canonical_label(match.group(1))
            parts = [match.group(2)] if match.group(2) else []
            continue
        if current:
            parts.append(line)
    if current:
        values[current] = clean_text(" ".join(parts), 1000)
    return values


def normalized_lines(text: str) -> list[str]:
    return [clean_text(line, 1000) for line in text.split("\n") if clean_text(line)]


def canonical_label(label: str) -> str:
    for candidate in LABELS:
        if candidate.lower() == label.lower():
            return candidate
    return label


def normalize_row(row: dict[str, Any], *, keywords: list[str]) -> dict[str, str]:
    title = clean_text(row.get("Description"), 500)
    bid_no = clean_text(row.get("Bid No"), 180)
    project_no = clean_text(row.get("ITB/Project No"), 180)
    bpo_no = clean_text(row.get("BPO No"), 180)
    source_record_id = first_nonempty([bid_no, project_no, bpo_no, title])
    due_date = iso_date(row.get("Bid Closing Date"))
    status = clean_text(row.get("Status") or status_from_due_date(due_date), 80)
    category = clean_text(row.get("Category"), 250)
    contract_type = clean_text(row.get("Contract Type"), 120)
    procurement_officer = clean_text(row.get("Procurement Officer"), 180)
    document_url = first_link(row) or PAGE_URL
    search_text = expand_related_terms(" ".join([source_record_id, title, category, contract_type, procurement_officer, status]))
    matched = keyword_hits(search_text, keywords)
    raw = {
        "source_key": "md_emma",
        "source_note": SOURCE_NOTE,
        "page_url": PAGE_URL,
        "blocked_emma_url": EMMA_URL,
        "row": row,
    }

    return {
        "id": stable_id("MD", source_record_id, prefix="md-dgs-bid"),
        "state": "MD",
        "source": SOURCE_NAME,
        "source_record_id": source_record_id,
        "title": title or source_record_id,
        "agency": "Maryland Department of General Services",
        "document_type": document_type(source_record_id, title, contract_type),
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


def first_link(row: dict[str, Any]) -> str:
    links = row.get("links") if isinstance(row.get("links"), list) else []
    for item in links:
        if isinstance(item, dict):
            url = clean_text(item.get("url"), 500)
            if url and not url.startswith("mailto:"):
                return url
    return ""


def absolute_links(items: list[tuple[str, str]]) -> list[dict[str, str]]:
    links: list[dict[str, str]] = []
    for href, label in items:
        if href:
            links.append({"url": urllib.parse.urljoin(PAGE_URL, href), "label": clean_text(label, 300)})
    return links


def first_nonempty(values: list[str]) -> str:
    for value in values:
        text = clean_text(value, 180)
        if text:
            return text
    return ""


def document_type(source_record_id: str, title: str, contract_type: str) -> str:
    text = " ".join([source_record_id, title, contract_type]).upper()
    if code_matches(text, "RFI") or "REQUEST FOR INFORMATION" in text:
        return "Maryland DGS Request for Information"
    if code_matches(text, "RFP") or "REQUEST FOR PROPOS" in text:
        return "Maryland DGS Request for Proposal"
    if code_matches(text, "RFQ") or "REQUEST FOR QUOTE" in text:
        return "Maryland DGS Request for Quote"
    if code_matches(text, "IFB") or code_matches(text, "ITB") or "INVITATION" in text:
        return "Maryland DGS Invitation to Bid"
    return "Maryland DGS Open Bid"


def status_from_due_date(due_date: str) -> str:
    due = parse_date(due_date)
    if due and due < dt.date.today():
        return "Closed"
    return "Open"


def expand_related_terms(text: str) -> str:
    expanded = text
    if any(term_matches(text, term) for term in ["Health", "MDH", "DHS", "Medicaid", "MMIS"]):
        expanded += " Medicaid MMIS managed care eligibility claims human services provider health care behavioral health"
    if any(term_matches(text, term) for term in ["data", "system", "software", "application", "digital"]):
        expanded += " system software data interoperability services"
    if any(term_matches(text, term) for term in ["rural", "hospital", "workforce"]):
        expanded += " rural health workforce"
    return expanded


def false_keyword_hit(record: dict[str, str]) -> bool:
    text = " ".join([record.get("title", ""), record.get("agency", ""), record.get("raw_json", "")])
    return "mmis" in {item.lower() for item in record.get("matched_keywords", "").split(";") if item} and term_matches(text, "commissary") and not term_matches(text, "MMIS")


def useful_keyword_match(matches: list[str], text: str) -> bool:
    generic_terms = {"claims", "eligibility", "enrollment", "provider", "provider data", "workforce", "cms", "interoperability"}
    matched_terms = {match.lower() for match in matches if match}
    if not matched_terms or not matched_terms <= generic_terms:
        return True
    context_terms = [
        "health",
        "healthcare",
        "health care",
        "human services",
        "medicaid",
        "medicare",
        "medical",
        "behavioral",
        "managed care",
        "provider",
        "chip",
        "mmis",
        "mdh",
        "dhs",
    ]
    return any(term_matches(text, term) for term in context_terms)


def relevance_score(matches: list[str], status: str, text: str, due_date: str) -> int:
    score = min(50, len([match for match in matches if match]) * 10)
    if any(term_matches(text, term) for term in ["Medicaid", "MMIS", "Health", "Human Services", "MDH", "DHS"]):
        score += 28
    if any(term_matches(text, term) for term in ["managed care", "eligibility", "claims", "provider data", "behavioral health"]):
        score += 18
    if any(term_matches(text, term) for term in ["interoperability", "FHIR", "prior authorization", "system", "software", "data"]):
        score += 15
    if any(term_matches(text, term) for term in ["RFP", "RFI", "RFQ", "ITB", "services", "application", "digital"]):
        score += 10
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


def record_sort_key(row: dict[str, str]) -> tuple[int, str, str]:
    return (int_or_zero(row.get("relevance_score")), row.get("due_date", ""), row.get("title", ""))


def code_matches(text: str, code: str) -> bool:
    return re.search(rf"(?<![A-Z0-9]){re.escape(code)}(?![A-Z0-9])", text, re.IGNORECASE) is not None


def int_or_zero(value: str | None) -> int:
    try:
        return int(float(value or 0))
    except ValueError:
        return 0


def attrs_dict(attrs: list[tuple[str, str | None]]) -> dict[str, str]:
    return {key: value or "" for key, value in attrs}


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def emit(progress: Callable[[str], None] | None, message: str) -> None:
    if progress:
        progress(message)
