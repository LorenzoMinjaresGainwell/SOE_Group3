from __future__ import annotations

import datetime as dt
import re
import time
import urllib.parse
from html.parser import HTMLParser
from typing import Any, Callable

from services.state_http import fetch_url
from services.state_normalization import clean_text, compact_raw_json, iso_date, keyword_hits, parse_date, stable_id, term_matches

BASE_URL = "https://wwwcfprd.doa.louisiana.gov/osp/lapac/"
SEARCH_URL = urllib.parse.urljoin(BASE_URL, "srchopen.cfm")
CONTACT_URL = urllib.parse.urljoin(BASE_URL, "dspBidContact.cfm")
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
SOURCE_NOTE = "Official LaPAC open-bid CFM search table; contact popup is fetched only for keyword-matched rows."
OPEN_SEARCH_PARAMS = {
    "deptno": "all",
    "catno": "all",
    "compareDate": "O",
    "dateStart": "",
    "dateEnd": "",
    "keywords": "",
    "keywordsCheck": "all",
}


class GridCell:
    def __init__(self) -> None:
        self.parts: list[str] = []
        self.hrefs: list[str] = []

    @property
    def text(self) -> str:
        return clean_text(" ".join(self.parts), 2000)


class TableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.current_row: list[GridCell] | None = None
        self.current_cell: GridCell | None = None
        self.rows: list[list[GridCell]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        data = attrs_dict(attrs)
        if tag == "tr":
            self.current_row = []
        elif tag in {"td", "th"} and self.current_row is not None:
            self.current_cell = GridCell()
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
    html, final_url = http_text(open_search_url(), referer=SEARCH_URL)
    rows = parse_lapac_rows(html)
    emit(progress, f"LA LaPAC open bids: {len(rows)} public rows")

    records: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in rows:
        base = normalize_lapac_row(row, final_url=final_url, keywords=keywords)
        if not base.get("source_record_id") or base["id"] in seen:
            continue
        if base["status"].lower() == "cancelled":
            continue
        if not is_open_or_recent(base["posted_date"], base["due_date"], days_back):
            continue
        if keywords and not base["matched_keywords"]:
            continue
        if not useful_keyword_match(base["matched_keywords"].split(";"), base["raw_json"]):
            continue

        raw = json_from_raw(base["raw_json"])
        contact = fetch_contact(raw.get("source_record_id", ""), progress=progress)
        raw["contact"] = contact
        agency = agency_from_contact(contact, raw.get("agency_code", ""))
        if agency:
            base["agency"] = agency
            score_text = " ".join([base["title"], agency, raw_text(raw)])
            matches = keyword_hits(score_text, keywords)
            base["matched_keywords"] = ";".join(matches or base["matched_keywords"].split(";"))
            base["relevance_score"] = str(relevance_score(base["matched_keywords"].split(";"), base["status"], score_text, base["due_date"]))
        base["raw_json"] = compact_raw_json(raw)
        seen.add(base["id"])
        records.append(base)

    return sorted(records, key=record_sort_key, reverse=True)[: max(1, max_records)]


def parse_lapac_rows(html: str) -> list[dict[str, Any]]:
    parser = TableParser()
    parser.feed(html)
    parsed: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None

    for row in parser.rows:
        if is_bid_row(row):
            current = {
                "source_record_id": clean_text(row[0].text, 180),
                "description_text": row[1].text,
                "document_hrefs": public_hrefs(row[1].hrefs),
                "posted_date": row[2].text,
                "due_date": row[3].text,
                "agency_code": clean_text(row[4].text, 80),
                "contact_hrefs": row[4].hrefs,
                "addenda": [],
            }
            parsed.append(current)
        elif current is not None and len(row) >= 2 and row[0].hrefs:
            current["addenda"].append(
                {
                    "description": clean_text(row[0].text, 1000),
                    "issued_date": iso_date(row[1].text),
                    "document_urls": [absolute_url(href) for href in public_hrefs(row[0].hrefs)],
                }
            )

    return parsed


def normalize_lapac_row(row: dict[str, Any], *, final_url: str, keywords: list[str]) -> dict[str, str]:
    source_record_id = clean_text(row.get("source_record_id"), 180)
    description = clean_text(row.get("description_text"), 2000)
    title = title_from_description(description) or source_record_id
    posted_date = iso_date(row.get("posted_date"))
    due_date = iso_date(row.get("due_date"))
    status = status_from_row(description, due_date)
    document_urls = [absolute_url(href) for href in row.get("document_hrefs") or []]
    addenda = row.get("addenda") or []
    search_text = " ".join(
        [
            source_record_id,
            title,
            description,
            clean_text(row.get("agency_code"), 80),
            " ".join(clean_text(item.get("description"), 500) for item in addenda),
        ]
    )
    matched = keyword_hits(search_text, keywords)
    raw = {
        "source_key": "la_lapac",
        "source_note": SOURCE_NOTE,
        "source_record_id": source_record_id,
        "description": description,
        "agency_code": clean_text(row.get("agency_code"), 80),
        "posted_date_raw": clean_text(row.get("posted_date"), 80),
        "due_date_raw": clean_text(row.get("due_date"), 120),
        "document_urls": document_urls,
        "addenda": addenda,
    }

    return {
        "id": stable_id("LA", source_record_id, prefix="la-lapac-bid"),
        "state": "LA",
        "source": "Louisiana LaPAC Open Bids",
        "source_record_id": source_record_id,
        "title": clean_text(title, 500),
        "agency": clean_text(row.get("agency_code"), 180),
        "document_type": document_type(source_record_id, title, description),
        "posted_date": posted_date,
        "due_date": due_date,
        "status": status,
        "amount": "",
        "document_url": document_urls[0] if document_urls else open_search_url(),
        "source_url": final_url or open_search_url(),
        "matched_keywords": ";".join(matched),
        "relevance_score": str(relevance_score(matched, status, search_text, due_date)),
        "raw_json": compact_raw_json(raw),
        "last_checked_at": now_iso(),
    }


def is_bid_row(row: list[GridCell]) -> bool:
    if len(row) < 5:
        return False
    source_record_id = clean_text(row[0].text, 120)
    if not source_record_id or source_record_id.lower() == "bid number":
        return False
    if not row[1].hrefs:
        return False
    return parse_date(row[2].text) is not None or parse_date(row[3].text) is not None


def title_from_description(value: Any) -> str:
    text = clean_text(value, 2000)
    marker = re.search(r"(?i)\s+(?:Original:|Attachments:|Bid Cancelled:)", text)
    if marker:
        text = text[: marker.start()]
    return clean_text(text, 500)


def status_from_row(description: str, due_date: str) -> str:
    lower = description.lower()
    if "bid cancelled" in lower or "cancellation" in lower:
        return "Cancelled"
    due = parse_date(due_date)
    if due and due < dt.date.today():
        return "Closed"
    return "Open"


def document_type(source_record_id: str, title: str, description: str) -> str:
    text = " ".join([source_record_id, title, description]).upper()
    if code_matches(text, "RFI"):
        return "LaPAC Request for Information"
    if code_matches(text, "RFP"):
        return "LaPAC Request for Proposal"
    if code_matches(text, "RFQ"):
        return "LaPAC Request for Quote"
    if code_matches(text, "IFB"):
        return "LaPAC Invitation for Bid"
    if "ADDENDUM" in text:
        return "LaPAC Solicitation with Addendum"
    return "LaPAC Solicitation"


def fetch_contact(source_record_id: str, *, progress: Callable[[str], None] | None = None) -> dict[str, str]:
    if not source_record_id:
        return {}
    url = CONTACT_URL + "?" + urllib.parse.urlencode({"bidno": source_record_id})
    try:
        html, _final_url = http_text(url, referer=open_search_url())
    except Exception as exc:
        emit(progress, f"LA contact lookup failed for {source_record_id}: {exc}")
        return {}
    parser = TableParser()
    parser.feed(html)
    fields: dict[str, str] = {}
    for row in parser.rows:
        if len(row) < 2:
            continue
        key = clean_text(row[0].text, 80).strip(":")
        value = clean_text(row[1].text, 500)
        if key and value:
            fields[key] = value
    return fields


def agency_from_contact(contact: dict[str, str], agency_code: Any) -> str:
    department = clean_text(contact.get("Department"), 180)
    section = clean_text(contact.get("Section"), 180)
    code = clean_text(agency_code, 80)
    if department == "*** State Procurement ***" and section:
        return section
    return department or section or code


def useful_keyword_match(matches: list[str], text: str) -> bool:
    generic_terms = {"claims", "eligibility", "enrollment"}
    matched_terms = {match.lower() for match in matches if match}
    if not matched_terms or not matched_terms <= generic_terms:
        return True
    context_terms = [
        "department of health",
        "louisiana department of health",
        "health",
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
        "ldh",
        "dhh",
    ]
    return any(term_matches(text, term) for term in context_terms)


def relevance_score(matches: list[str], status: str, text: str, due_date: str) -> int:
    score = min(50, len([match for match in matches if match]) * 10)
    if any(term_matches(text, term) for term in ["Medicaid", "MMIS", "Louisiana Department of Health", "LDH", "Department of Health"]):
        score += 25
    if any(term_matches(text, term) for term in ["eligibility", "claims", "enrollment", "managed care", "interoperability", "FHIR", "prior authorization", "provider data"]):
        score += 15
    if any(term_matches(text, term) for term in ["RFP", "RFI", "RFQ", "software", "data", "cloud", "platform", "services"]):
        score += 12
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


def open_search_url() -> str:
    return SEARCH_URL + "?" + urllib.parse.urlencode(OPEN_SEARCH_PARAMS)


def http_text(url: str, *, referer: str) -> tuple[str, str]:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": referer,
    }
    last_error = ""
    for attempt in range(3):
        result = fetch_url(url, headers=headers, timeout=60, byte_limit=2_000_000, user_agent=USER_AGENT)
        if result.ok:
            return result.body_text(), result.final_url
        last_error = result.error or f"HTTP {result.status_code}"
        time.sleep(1 + attempt)
    raise RuntimeError(f"LA LaPAC request failed for {url}: {last_error}")


def public_hrefs(hrefs: list[str]) -> list[str]:
    return [href for href in hrefs if href and not href.lower().startswith("javascript:")]


def absolute_url(href: str) -> str:
    return urllib.parse.urljoin(BASE_URL, href)


def attrs_dict(attrs: list[tuple[str, str | None]]) -> dict[str, str]:
    return {key: value or "" for key, value in attrs}


def code_matches(text: str, code: str) -> bool:
    return re.search(rf"(?<![A-Z0-9]){re.escape(code)}(?![A-Z0-9])", text, re.IGNORECASE) is not None


def json_from_raw(value: str) -> dict[str, Any]:
    import json

    try:
        parsed = json.loads(value or "{}")
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def raw_text(value: Any) -> str:
    return compact_raw_json(value, limit=2000)


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
