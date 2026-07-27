from __future__ import annotations

import datetime as dt
import re
import time
import urllib.parse
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any, Callable

from services.state_http import fetch_url
from services.state_normalization import clean_text, compact_raw_json, iso_date, keyword_hits, parse_date, stable_id, term_matches

USER_AGENT = "soe-group3-jaggaer-opportunities/0.1"
SOURCE_NOTE = "Public JAGGAER/SciQuest PublicEvent open-events page parsed from initial server-rendered results; no login or JS state replay used."


@dataclass(frozen=True)
class JaggaerPublicEventConfig:
    state: str
    source_name: str
    public_event_url: str
    source_key: str
    source_note: str = SOURCE_NOTE


class JaggaerCell:
    def __init__(self) -> None:
        self.parts: list[str] = []
        self.hrefs: list[tuple[str, str]] = []

    @property
    def text(self) -> str:
        return clean_text(" ".join(self.parts), 3000)


class JaggaerResultsParser(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__()
        self.base_url = base_url
        self.current_row: list[JaggaerCell] | None = None
        self.current_cell: JaggaerCell | None = None
        self.current_link: list[str] | None = None
        self.current_link_href = ""
        self.current_link_label = ""
        self.rows: list[list[JaggaerCell]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        data = attrs_dict(attrs)
        if tag == "tr":
            self.current_row = []
        elif tag in {"td", "th"} and self.current_row is not None:
            self.current_cell = JaggaerCell()
        elif tag == "a" and self.current_cell is not None:
            href = data.get("href") or ""
            if href:
                self.current_link = []
                self.current_link_href = urllib.parse.urljoin(self.base_url, href)
                self.current_link_label = data.get("name") or data.get("id") or ""
        elif tag == "br" and self.current_cell is not None:
            self.current_cell.parts.append(" ")

    def handle_data(self, data: str) -> None:
        if self.current_cell is not None:
            self.current_cell.parts.append(data)
        if self.current_link is not None:
            self.current_link.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self.current_link is not None and self.current_cell is not None:
            label = clean_text(" ".join(self.current_link) or self.current_link_label, 500)
            self.current_cell.hrefs.append((self.current_link_href, label))
            self.current_link = None
            self.current_link_href = ""
            self.current_link_label = ""
        elif tag in {"td", "th"} and self.current_cell is not None and self.current_row is not None:
            self.current_row.append(self.current_cell)
            self.current_cell = None
        elif tag == "tr" and self.current_row is not None:
            if self.current_row:
                self.rows.append(self.current_row)
            self.current_row = None


def fetch_jaggaer_public_event_opportunities(
    *,
    config: JaggaerPublicEventConfig,
    keywords: list[str],
    days_back: int,
    max_records: int,
    progress: Callable[[str], None] | None = None,
) -> list[dict[str, str]]:
    html, final_url = http_text(config.public_event_url)
    rows = parse_public_event_rows(html, final_url)
    emit(progress, f"{config.state} JAGGAER PublicEvent open events: {len(rows)} public rows")

    records: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in rows:
        record = normalize_public_event_row(row, config=config, source_url=final_url, keywords=keywords)
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


def http_text(url: str) -> tuple[str, str]:
    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": url,
    }
    last_error = ""
    for attempt in range(3):
        result = fetch_url(url, headers=headers, timeout=60, byte_limit=1_000_000, user_agent=USER_AGENT)
        if result.ok and result.body:
            return result.body_text(), result.final_url
        last_error = result.error or f"HTTP {result.status_code}"
        time.sleep(1 + attempt)
    raise RuntimeError(f"JAGGAER PublicEvent request failed for {url}: {last_error}")


def parse_public_event_rows(html: str, base_url: str) -> list[list[JaggaerCell]]:
    parser = JaggaerResultsParser(base_url)
    parser.feed(html)
    return [row for row in parser.rows if event_link(row)]


def normalize_public_event_row(
    row: list[JaggaerCell],
    *,
    config: JaggaerPublicEventConfig,
    source_url: str,
    keywords: list[str],
) -> dict[str, str]:
    status = row[0].text if row else ""
    details = row[1] if len(row) > 1 else JaggaerCell()
    detail_text = details.text
    document_url, title = event_link(row)
    title = title or title_from_text(detail_text)
    description = description_from_text(detail_text, title)
    posted_raw = label_value(detail_text, "Open", ["Close", "Type", "Number", "Contact", "Details"])
    due_raw = label_value(detail_text, "Close", ["Type", "Number", "Contact", "Details"])
    event_type = label_value(detail_text, "Type", ["Number", "Contact", "Details"])
    source_record_id = label_value(detail_text, "Number", ["Contact", "Details"]) or stable_source_record_id(document_url, title)
    contact = label_value(detail_text, "Contact", ["Details", "View as PDF"])
    pdf_urls = [href for href, label in details.hrefs if "event.pdf" in href.lower() or "PDF" in label.upper()]
    search_text = " ".join([source_record_id, title, description, event_type, status, contact])
    matched = keyword_hits(search_text, keywords)
    posted_date = iso_date(posted_raw)
    due_date = iso_date(due_raw)
    raw = {
        "source_key": config.source_key,
        "source_note": config.source_note,
        "source_record_id": source_record_id,
        "title": title,
        "description": description,
        "status": status,
        "open_raw": posted_raw,
        "close_raw": due_raw,
        "type": event_type,
        "contact": contact,
        "event_url": strip_transient_query(document_url),
        "pdf_url_count": len(pdf_urls),
    }

    return {
        "id": stable_id(config.state, source_record_id or title, prefix=f"{config.state.lower()}-jaggaer-event"),
        "state": config.state,
        "source": config.source_name,
        "source_record_id": source_record_id,
        "title": clean_text(title or source_record_id, 500),
        "agency": "",
        "document_type": document_type(event_type, source_record_id, title),
        "posted_date": posted_date,
        "due_date": due_date,
        "status": clean_text(status or status_from_due_date(due_date), 80),
        "amount": "",
        "document_url": strip_transient_query(document_url) or source_url,
        "source_url": source_url,
        "matched_keywords": ";".join(matched),
        "relevance_score": str(relevance_score(matched, status, search_text, due_date)),
        "raw_json": compact_raw_json(raw),
        "last_checked_at": now_iso(),
    }


def event_link(row: list[JaggaerCell]) -> tuple[str, str]:
    for cell in row:
        for href, label in cell.hrefs:
            if "ViewSourcingEvent" in href:
                return href, clean_text(label, 500)
    return "", ""


def label_value(text: str, label: str, stop_labels: list[str]) -> str:
    stops = "|".join(re.escape(stop) for stop in stop_labels)
    pattern = rf"\b{re.escape(label)}\b\s+(.*?)(?=\s+(?:{stops})\b|$)"
    match = re.search(pattern, text, re.IGNORECASE)
    return clean_text(match.group(1), 500) if match else ""


def title_from_text(text: str) -> str:
    marker = re.search(r"\bOpen\s+\d{1,2}/\d{1,2}/\d{2,4}", text, re.IGNORECASE)
    prefix = text[: marker.start()].strip() if marker else text
    return clean_text(prefix.split("  ", 1)[0], 500)


def description_from_text(text: str, title: str) -> str:
    marker = re.search(r"\bOpen\s+\d{1,2}/\d{1,2}/\d{2,4}", text, re.IGNORECASE)
    prefix = text[: marker.start()].strip() if marker else text
    for _ in range(2):
        if title and prefix.lower().startswith(title.lower()):
            prefix = prefix[len(title) :].strip(" -:")
    return clean_text(prefix, 1500)


def stable_source_record_id(document_url: str, title: str) -> str:
    token = query_value(document_url, "AuthToken")
    if token:
        return stable_id(token, prefix="jaggaer-token", max_length=80)
    return clean_text(title, 160)


def query_value(url: str, key: str) -> str:
    values = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).get(key)
    return clean_text(values[0], 200) if values else ""


def strip_transient_query(url: str) -> str:
    if not url:
        return ""
    parts = urllib.parse.urlsplit(url)
    query = [(key, value) for key, value in urllib.parse.parse_qsl(parts.query, keep_blank_values=True) if key.lower() != "tmstmp"]
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, urllib.parse.urlencode(query), parts.fragment))


def document_type(event_type: str, source_record_id: str, title: str) -> str:
    code = clean_text(event_type, 80).upper()
    text = " ".join([code, source_record_id, title]).upper()
    if code_matches(text, "RFI"):
        return "JAGGAER Request for Information"
    if code_matches(text, "RFP"):
        return "JAGGAER Request for Proposal"
    if code_matches(text, "RFQ"):
        return "JAGGAER Request for Quote"
    if code_matches(text, "RFB") or code_matches(text, "IFB"):
        return "JAGGAER Invitation for Bid"
    if code:
        return f"JAGGAER {code}"
    return "JAGGAER Sourcing Event"


def status_from_due_date(due_date: str) -> str:
    due = parse_date(due_date)
    if due and due < dt.date.today():
        return "Closed"
    return "Open"


def false_keyword_hit(record: dict[str, str]) -> bool:
    text = " ".join([record.get("title", ""), record.get("raw_json", "")])
    return "mmis" in {item.lower() for item in record.get("matched_keywords", "").split(";")} and term_matches(text, "commissary") and not term_matches(text, "MMIS")


def useful_keyword_match(matches: list[str], text: str) -> bool:
    generic_terms = {"claims", "eligibility", "enrollment"}
    matched_terms = {match.lower() for match in matches if match}
    if not matched_terms or not matched_terms <= generic_terms:
        return True
    context_terms = [
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
    score = min(50, len([match for match in matches if match]) * 10)
    if any(term_matches(text, term) for term in ["Medicaid", "MMIS", "Human Services", "Health Care", "Healthcare"]):
        score += 25
    if any(term_matches(text, term) for term in ["eligibility", "claims", "enrollment", "managed care", "interoperability", "FHIR", "prior authorization", "provider data"]):
        score += 15
    if term_matches(text, "rural health"):
        score += 25
    if any(term_matches(text, term) for term in ["RFP", "RFI", "RFQ", "RFB", "IFB", "software", "data", "cloud", "platform", "services"]):
        score += 12
    if status.lower() in {"open", "posted", "upcoming"}:
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
