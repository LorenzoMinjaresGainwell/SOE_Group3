from __future__ import annotations

import datetime as dt
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from typing import Any, Callable

from services.state_normalization import clean_text, compact_raw_json, iso_date, keyword_hits, parse_date, stable_id, term_matches

RIDOP_URL = "https://ridop.ri.gov/vendor-resources/all-solicitations"
STATE_SEARCH_URL = "https://purchasing.ri.gov/bidding/bidsearch.aspx"
STATE_LISTING_URL = "https://purchasing.ri.gov/bidding/Bidlisting.aspx"
EXTERNAL_SEARCH_URL = "https://purchasing.ri.gov/bidding/externalbidsearch.aspx"
EXTERNAL_LISTING_URL = "https://purchasing.ri.gov/bidding/ExternalBidListing.aspx"
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
SOURCE_NOTE = "Official RIDOP all-solicitations page links to legacy RIVIP ASP.NET listing routes; state active route is scanned and external active rows are parsed."
MAX_SCAN_ROWS = 500


class FormParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.in_aspnet_form = False
        self.fields: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        data = attrs_dict(attrs)
        if tag == "form":
            self.in_aspnet_form = data.get("id") == "aspnetForm" or data.get("name") == "aspnetForm"
            return
        if not self.in_aspnet_form or tag != "input":
            return
        name = data.get("name")
        input_type = data.get("type", "").lower()
        if name and input_type != "button":
            self.fields[name] = data.get("value", "")

    def handle_endtag(self, tag: str) -> None:
        if tag == "form":
            self.in_aspnet_form = False


class GridCell:
    def __init__(self) -> None:
        self.parts: list[str] = []
        self.hrefs: list[str] = []

    @property
    def text(self) -> str:
        return clean_text(" ".join(self.parts), 2000)


class TableParser(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__()
        self.base_url = base_url
        self.tables: list[list[list[GridCell]]] = []
        self.current_table: list[list[GridCell]] | None = None
        self.current_row: list[GridCell] | None = None
        self.current_cell: GridCell | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        data = attrs_dict(attrs)
        if tag == "table":
            self.current_table = []
            self.tables.append(self.current_table)
        elif tag == "tr" and self.current_table is not None:
            self.current_row = []
        elif tag in {"td", "th"} and self.current_row is not None:
            self.current_cell = GridCell()
        elif tag == "a" and self.current_cell is not None:
            href = data.get("href")
            if href:
                self.current_cell.hrefs.append(urllib.parse.urljoin(self.base_url, href))
        elif tag == "br" and self.current_cell is not None:
            self.current_cell.parts.append(" ")

    def handle_data(self, data: str) -> None:
        if self.current_cell is not None:
            self.current_cell.parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"td", "th"} and self.current_cell is not None and self.current_row is not None:
            self.current_row.append(self.current_cell)
            self.current_cell = None
        elif tag == "tr" and self.current_row is not None and self.current_table is not None:
            if self.current_row:
                self.current_table.append(self.current_row)
            self.current_row = None
        elif tag == "table":
            self.current_table = None


def fetch_opportunities(
    *,
    keywords: list[str],
    days_back: int,
    max_records: int,
    progress: Callable[[str], None] | None = None,
) -> list[dict[str, str]]:
    state_rows = fetch_listing_rows(kind="state", progress=progress)
    external_rows = fetch_listing_rows(kind="external", progress=progress)
    rows = (state_rows + external_rows)[:MAX_SCAN_ROWS]
    emit(
        progress,
        f"RI RIVIP active solicitations: {len(rows)} public rows (state {len(state_rows)}, external {len(external_rows)})",
    )

    records: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in rows:
        record = normalize_listing_row(row, keywords=keywords)
        if not record.get("source_record_id") or record["id"] in seen:
            continue
        if not is_open_or_recent(record["status"], record["posted_date"], record["due_date"], days_back):
            continue
        if keywords and not record["matched_keywords"]:
            continue
        if not useful_keyword_match(record["matched_keywords"].split(";"), record["raw_json"]):
            continue
        seen.add(record["id"])
        records.append(record)

    return sorted(records, key=record_sort_key, reverse=True)[: max(1, max_records)]


def fetch_listing_rows(*, kind: str, progress: Callable[[str], None] | None = None) -> list[dict[str, Any]]:
    if kind == "external":
        search_url = EXTERNAL_SEARCH_URL
        listing_url = EXTERNAL_LISTING_URL
        updates = {
            "ctl00$ContentPlaceHolder1$ddl_ExBiddingGroup": "All External Bidding Groups",
            "ctl00$ContentPlaceHolder1$lstbox_ExBidStatus": "Active(Scheduled)",
            "ctl00$ContentPlaceHolder1$txtbox_ExBidNumber": "",
            "ctl00$ContentPlaceHolder1$txtbox_ExKeywords": "",
            "ctl00$ContentPlaceHolder1$txtbox_ExOpeningAfter": "",
            "ctl00$ContentPlaceHolder1$txtbox_ExOpeningBefore": "",
            "ctl00$ContentPlaceHolder1$btn_ExSearch": "Search",
        }
    else:
        search_url = STATE_SEARCH_URL
        listing_url = STATE_LISTING_URL
        updates = {
            "ctl00$ContentPlaceHolder1$lstbox_BidStatus": "Active(Scheduled)",
            "ctl00$ContentPlaceHolder1$txtbox_BidNumber": "",
            "ctl00$ContentPlaceHolder1$txtbox_Keywords": "",
            "ctl00$ContentPlaceHolder1$txtbox_OpeningAfter": "",
            "ctl00$ContentPlaceHolder1$txtbox_OpeningBefore": "",
            "ctl00$ContentPlaceHolder1$btn_search": "Search",
        }

    try:
        html, final_url = post_search(search_url, listing_url, updates)
    except Exception as exc:
        emit(progress, f"RI {kind} solicitation search failed: {exc}")
        return []
    return parse_listing_rows(html, final_url=final_url, kind=kind)[:MAX_SCAN_ROWS]


def post_search(search_url: str, listing_url: str, updates: dict[str, str]) -> tuple[str, str]:
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor())
    html, final_search_url = open_text(opener, search_url, data=None, referer=RIDOP_URL)
    parser = FormParser()
    parser.feed(html)
    fields = dict(parser.fields)
    fields.update(updates)
    body = urllib.parse.urlencode(fields).encode("utf-8")
    return open_text(opener, listing_url, data=body, referer=final_search_url)


def open_text(
    opener: urllib.request.OpenerDirector,
    url: str,
    *,
    data: bytes | None,
    referer: str,
    timeout: int = 60,
) -> tuple[str, str]:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": referer,
    }
    if data is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    request = urllib.request.Request(url, data=data, headers=headers, method="POST" if data is not None else "GET")
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            with opener.open(request, timeout=timeout) as response:
                return response.read().decode("utf-8", "replace"), response.geturl()
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = exc
            time.sleep(1 + attempt)
    raise RuntimeError(f"request failed for {url}: {last_error}")


def parse_listing_rows(html: str, *, final_url: str, kind: str) -> list[dict[str, Any]]:
    parser = TableParser(final_url)
    parser.feed(html)
    parsed: list[dict[str, Any]] = []

    for table in parser.tables:
        rows = [row for row in table if any(cell.text or cell.hrefs for cell in row)]
        if len(rows) < 2:
            continue
        headers = [cell.text for cell in rows[0]]
        normalized_headers = [normalize_header(header) for header in headers]
        if "solicitation_number" not in normalized_headers or "bid_title" not in normalized_headers:
            continue
        for row in rows[1:]:
            if len(row) < len(headers):
                continue
            values: dict[str, Any] = {normalized_headers[index]: cell_payload(row[index]) for index in range(len(headers))}
            if not clean_text(values.get("solicitation_number", {}).get("text"), 120):
                continue
            values["source_kind"] = kind
            values["headers"] = headers
            values["listing_url"] = final_url
            parsed.append(values)
    return parsed


def normalize_listing_row(row: dict[str, Any], *, keywords: list[str]) -> dict[str, str]:
    source_kind = clean_text(row.get("source_kind"), 40) or "state"
    solicitation = row_value(row, "solicitation_number")
    title = row_value(row, "bid_title") or solicitation
    status = row_value(row, "solicitation_status") or "Active(Scheduled)"
    due_date = iso_date(row_value(row, "opening_time"))
    posting_group = row_value(row, "posting_group")
    posting_entity = row_value(row, "posting_entity")
    contact = row_value(row, "contact_person")
    contact_phone = row_value(row, "contact_phone")
    agency = posting_entity or ("Rhode Island Division of Purchases" if source_kind == "state" else posting_group)
    solicitation_links = row_links(row, "solicitation_number")
    title_links = row_links(row, "bid_title")
    document_url = first_url(solicitation_links) or first_url(title_links) or row.get("listing_url", "")
    source_url = first_url(title_links) or row.get("listing_url", "") or document_url
    search_text = " ".join([solicitation, title, status, posting_group, posting_entity, contact, contact_phone])
    matched = keyword_hits(search_text, keywords)
    raw = {
        "source_key": "ri_ocean_state_procures",
        "source_note": SOURCE_NOTE,
        "source_kind": source_kind,
        "row": row,
        "document_url": document_url,
        "detail_url": source_url,
    }

    return {
        "id": stable_id("RI", source_kind, solicitation, title, prefix="ri-rivip-bid"),
        "state": "RI",
        "source": "Rhode Island RIVIP Active Solicitations",
        "source_record_id": solicitation,
        "title": title,
        "agency": agency,
        "document_type": document_type(solicitation, title),
        "posted_date": "",
        "due_date": due_date,
        "status": status,
        "amount": "",
        "document_url": document_url,
        "source_url": source_url,
        "matched_keywords": ";".join(matched),
        "relevance_score": str(relevance_score(matched, status, search_text, due_date)),
        "raw_json": compact_raw_json(raw),
        "last_checked_at": now_iso(),
    }


def cell_payload(cell: GridCell) -> dict[str, Any]:
    return {"text": cell.text, "hrefs": list(dict.fromkeys(cell.hrefs))}


def row_value(row: dict[str, Any], key: str) -> str:
    value = row.get(key) or {}
    return clean_text(value.get("text") if isinstance(value, dict) else value, 500)


def row_links(row: dict[str, Any], key: str) -> list[str]:
    value = row.get(key) or {}
    if not isinstance(value, dict):
        return []
    return [clean_text(url, 700) for url in value.get("hrefs") or [] if clean_text(url, 700)]


def first_url(urls: list[str]) -> str:
    return urls[0] if urls else ""


def normalize_header(header: str) -> str:
    value = clean_text(header).lower()
    value = re.sub(r"[^a-z0-9]+", "_", value).strip("_")
    aliases = {
        "solicitation": "solicitation_number",
        "solicitation_no": "solicitation_number",
        "bid_number": "solicitation_number",
        "title": "bid_title",
        "bid_description": "bid_title",
        "opening_date": "opening_time",
    }
    return aliases.get(value, value)


def document_type(source_record_id: str, title: str) -> str:
    text = " ".join([source_record_id, title]).upper()
    if code_matches(text, "RFI"):
        return "Rhode Island Request for Information"
    if code_matches(text, "RFP"):
        return "Rhode Island Request for Proposal"
    if code_matches(text, "RFQ"):
        return "Rhode Island Request for Quote"
    if code_matches(text, "IFB"):
        return "Rhode Island Invitation for Bid"
    return "Rhode Island Solicitation"


def useful_keyword_match(matches: list[str], text: str) -> bool:
    ambiguous_terms = {"claims", "eligibility", "enrollment", "cms"}
    matched_terms = {match.lower() for match in matches if match}
    if not matched_terms or not matched_terms <= ambiguous_terms:
        return True
    context_terms = [
        "health",
        "human services",
        "executive office of health and human services",
        "medicaid",
        "medicare",
        "medical",
        "behavioral health",
        "managed care",
        "provider",
        "chip",
        "mmis",
    ]
    return any(term_matches(text, term) for term in context_terms)


def relevance_score(matches: list[str], status: str, text: str, due_date: str) -> int:
    score = min(50, len([match for match in matches if match]) * 10)
    if any(term_matches(text, term) for term in ["Medicaid", "MMIS", "health and human services", "department of health"]):
        score += 25
    if any(term_matches(text, term) for term in ["eligibility", "claims", "enrollment", "managed care", "interoperability", "FHIR", "prior authorization", "provider data"]):
        score += 15
    if term_matches(text, "rural health"):
        score += 25
    if any(term_matches(text, term) for term in ["RFP", "RFI", "RFQ", "software", "system", "services", "maintenance"]):
        score += 12
    if status.lower().startswith("active"):
        score += 10
    due = parse_date(due_date)
    if due and due >= dt.date.today():
        score += 8
    return min(score, 100)


def is_open_or_recent(status: str, posted_date: str, due_date: str, days_back: int) -> bool:
    if status.lower().startswith("active") or status.lower() in {"open", "scheduled"}:
        return True
    due = parse_date(due_date)
    if due and due >= dt.date.today():
        return True
    if days_back <= 0:
        return True
    posted = parse_date(posted_date)
    return not posted or (dt.date.today() - posted).days <= days_back


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
