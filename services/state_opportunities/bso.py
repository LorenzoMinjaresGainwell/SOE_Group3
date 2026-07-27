from __future__ import annotations

import datetime as dt
import time
import urllib.parse
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any, Callable

from services.state_http import fetch_url
from services.state_normalization import clean_text, compact_raw_json, iso_date, keyword_hits, parse_date, stable_id

BSO_OPEN_BIDS_PATH = "view/search/external/advancedSearchBid.xhtml?openBids=true"
BSO_BID_RESULTS_TBODY_ID = "bidSearchResultsForm:bidResultId_data"
USER_AGENT = "soe-group3-bso-opportunities/0.1"


@dataclass(frozen=True)
class BsoOpportunityConfig:
    state: str
    source_name: str
    base_url: str
    source_key: str = ""
    source_note: str = "Public BSO open-bids page parsed from initial JSF-rendered results; no login/session postback used."

    @property
    def open_bids_url(self) -> str:
        return urllib.parse.urljoin(self.base_url.rstrip("/") + "/", BSO_OPEN_BIDS_PATH.lstrip("/"))


@dataclass(frozen=True)
class BsoBidRow:
    cells: list["BsoCell"]

    def cell_text(self, index: int) -> str:
        return self.cells[index].text if index < len(self.cells) else ""

    def cell_href(self, index: int, base_url: str) -> str:
        if index >= len(self.cells):
            return ""
        for href in self.cells[index].hrefs:
            if href and href != "#" and not href.lower().startswith("javascript:"):
                return urllib.parse.urljoin(base_url, href)
        return ""


class BsoCell:
    def __init__(self) -> None:
        self.parts: list[str] = []
        self.hrefs: list[str] = []

    @property
    def text(self) -> str:
        return clean_text(" ".join(self.parts), 2000)


class BsoResultsParser(HTMLParser):
    def __init__(self, tbody_id: str = BSO_BID_RESULTS_TBODY_ID) -> None:
        super().__init__()
        self.tbody_id = tbody_id
        self.in_results = False
        self.depth = 0
        self.current_row: list[BsoCell] | None = None
        self.current_cell: BsoCell | None = None
        self.rows: list[BsoBidRow] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        data = attrs_dict(attrs)
        if tag == "tbody" and data.get("id") == self.tbody_id:
            self.in_results = True
            self.depth = 1
            return
        if self.in_results and tag in {"tbody", "table"}:
            self.depth += 1
        if not self.in_results:
            return
        if tag == "tr":
            self.current_row = []
        elif tag in {"td", "th"} and self.current_row is not None:
            self.current_cell = BsoCell()
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
        if not self.in_results:
            return
        if tag in {"td", "th"} and self.current_cell is not None and self.current_row is not None:
            self.current_row.append(self.current_cell)
            self.current_cell = None
        elif tag == "tr" and self.current_row is not None:
            if self.current_row:
                self.rows.append(BsoBidRow(self.current_row))
            self.current_row = None
        elif tag in {"tbody", "table"}:
            self.depth -= 1
            if self.depth <= 0:
                self.in_results = False


def fetch_bso_open_bid_opportunities(
    *,
    config: BsoOpportunityConfig,
    keywords: list[str],
    days_back: int,
    max_records: int,
    progress: Callable[[str], None] | None = None,
) -> list[dict[str, str]]:
    limit = max(1, max_records)
    html = http_text(config.open_bids_url, referer=config.base_url)
    rows = parse_open_bid_rows(html)
    emit(progress, f"{config.state} BSO open bids: {len(rows)} public rows on first page")

    records: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in rows:
        record = normalize_bid_row(row, config=config, keywords=keywords)
        if not record.get("source_record_id") or record["id"] in seen:
            continue
        if false_keyword_hit(record) or not useful_keyword_match(record["matched_keywords"].split(";"), record["raw_json"]):
            continue
        if not is_open_or_recent(record["posted_date"], record["due_date"], days_back):
            continue
        if keywords and not record["matched_keywords"]:
            continue
        seen.add(record["id"])
        records.append(record)

    return sorted(records, key=record_sort_key, reverse=True)[:limit]


def http_text(url: str, *, referer: str, timeout: int = 60) -> str:
    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Referer": referer,
    }
    last_error: Exception | None = None
    for attempt in range(3):
        result = fetch_url(url, headers=headers, timeout=timeout, user_agent=USER_AGENT)
        if result.ok and result.body:
            return result.body_text()
        last_error = RuntimeError(f"status={result.status_code} error={result.error}")
        time.sleep(1 + attempt)
    raise RuntimeError(f"BSO request failed for {url}: {last_error}")


def parse_open_bid_rows(html: str) -> list[BsoBidRow]:
    parser = BsoResultsParser()
    parser.feed(html)
    return [row for row in parser.rows if row.cell_text(0) and "No records found" not in row.cell_text(0)]


def normalize_bid_row(row: BsoBidRow, *, config: BsoOpportunityConfig, keywords: list[str]) -> dict[str, str]:
    bid_id = row.cell_text(1) or row.cell_text(0)
    agency = row.cell_text(2)
    buyer = row.cell_text(5)
    title = row.cell_text(6) or bid_id
    opening_date = iso_date(row.cell_text(7))
    status = row.cell_text(10) or "Open"
    alternate_id = row.cell_text(11)
    document_url = row.cell_href(0, config.open_bids_url)
    if not document_url and bid_id:
        document_url = urllib.parse.urljoin(
            config.base_url.rstrip("/") + "/",
            "external/bidDetail.sda?" + urllib.parse.urlencode({"docId": bid_id, "external": "true", "parentUrl": "close"}),
        )
    search_text = " ".join([bid_id, agency, buyer, title, status, alternate_id])
    matched = keyword_hits(search_text, keywords)
    raw = {
        "source_key": config.source_key,
        "bid_solicitation_number": bid_id,
        "organization_name": agency,
        "buyer": buyer,
        "description": title,
        "bid_opening_date": row.cell_text(7),
        "status": status,
        "alternate_id": alternate_id,
        "source_note": config.source_note,
    }
    return {
        "id": stable_id(config.state, bid_id, prefix=f"{config.state.lower()}-bso-bid"),
        "state": config.state,
        "source": config.source_name,
        "source_record_id": bid_id,
        "title": title,
        "agency": agency,
        "document_type": document_type(title, alternate_id),
        "posted_date": "",
        "due_date": opening_date,
        "status": status,
        "amount": "",
        "document_url": document_url,
        "source_url": config.open_bids_url,
        "matched_keywords": ";".join(matched),
        "relevance_score": str(relevance_score(matched, status, search_text, opening_date)),
        "raw_json": compact_raw_json(raw),
        "last_checked_at": now_iso(),
    }


def document_type(title: str, alternate_id: str) -> str:
    text = " ".join([title, alternate_id]).lower()
    if "request for information" in text or term_matches(text, "RFI"):
        return "BSO Request for Information"
    if "request for proposal" in text or term_matches(text, "RFP"):
        return "BSO Request for Proposal"
    if "request for quote" in text or term_matches(text, "RFQ"):
        return "BSO Request for Quote"
    if "invitation for bid" in text or term_matches(text, "IFB"):
        return "BSO Invitation for Bid"
    return "BSO Bid Solicitation"


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
    score = min(50, len(matches) * 10)
    if any(term_matches(text, term) for term in ["Medicaid", "MMIS", "Human Services", "Health Care", "Healthcare"]):
        score += 25
    if any(term_matches(text, term) for term in ["eligibility", "claims", "enrollment", "managed care", "interoperability", "FHIR", "prior authorization", "provider data"]):
        score += 15
    if term_matches(text, "rural health"):
        score += 25
    if any(term_matches(text, term) for term in ["RFP", "RFI", "RFQ", "IFB", "software", "data", "cloud", "platform"]):
        score += 12
    if status.lower() in {"sent", "open", "posted", "addendum posted", "upcoming"}:
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


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def emit(progress: Callable[[str], None] | None, message: str) -> None:
    if progress:
        progress(message)
