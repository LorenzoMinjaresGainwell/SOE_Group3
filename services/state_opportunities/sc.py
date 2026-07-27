from __future__ import annotations

import datetime as dt
import re
import time
import urllib.parse
from html.parser import HTMLParser
from typing import Any, Callable

from services.state_http import fetch_url
from services.state_normalization import amount_string, clean_text, compact_raw_json, iso_date, keyword_hits, parse_date, stable_id, term_matches

BASE_URL = "https://scbo.sc.gov/"
SEARCH_URL = urllib.parse.urljoin(BASE_URL, "adsearch")
DETAIL_URL = urllib.parse.urljoin(BASE_URL, "online-edition")
PRINT_URL = urllib.parse.urljoin(BASE_URL, "printad")
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
SOURCE_NOTE = "Official SCBO /adsearch keyword results with public /online-edition?s= detail pages and printable /printad?a= record."
SEARCH_RESULT_RE = re.compile(
    r"(?is)<div class=[\"']src_hres[^\"']*[\"']>\s*"
    r"<div class=[\"']src_rcol[^\"']*[\"']>.*?</div>\s*"
    r"<div class=[\"']src_rcol[^\"']*[\"']>\s*<a\s+href=[\"']([^\"']+)[\"'][^>]*title=[\"']([^\"']*)[\"'][^>]*>(.*?)</a>\s*</div>\s*"
    r"<div class=[\"']src_rcol[^\"']*[\"']>(.*?)</div>\s*</div>"
)
TAG_RE = re.compile(r"(?is)<[^>]+>")
DETAIL_SCAN_LIMIT = 180


class DetailCell:
    def __init__(self, class_name: str) -> None:
        self.class_name = class_name
        self.parts: list[str] = []
        self.hrefs: list[str] = []

    @property
    def text(self) -> str:
        return clean_text(" ".join(self.parts), 3000)


class DetailParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.cells: list[DetailCell] = []
        self.current: DetailCell | None = None
        self.div_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        data = attrs_dict(attrs)
        if tag == "div" and self.current is None and "adata_itm" in data.get("class", ""):
            self.current = DetailCell(data.get("class", ""))
            self.div_depth = 0
            return
        if self.current is not None and tag == "div":
            self.div_depth += 1
        if self.current is not None and tag == "a":
            href = data.get("href")
            if href:
                self.current.hrefs.append(absolute_url(href))
        if self.current is not None and tag == "br":
            self.current.parts.append(" ")

    def handle_data(self, data: str) -> None:
        if self.current is not None:
            self.current.parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self.current is None or tag != "div":
            return
        if self.div_depth > 0:
            self.div_depth -= 1
            return
        self.cells.append(self.current)
        self.current = None


def fetch_opportunities(
    *,
    keywords: list[str],
    days_back: int,
    max_records: int,
    progress: Callable[[str], None] | None = None,
) -> list[dict[str, str]]:
    terms = prioritized_search_terms(keywords) or ["health"]
    candidates: dict[str, dict[str, Any]] = {}
    candidate_cap = min(max(60, max_records * 3), DETAIL_SCAN_LIMIT)

    for term in terms:
        for row in fetch_search_results(term):
            merge_candidate(candidates, row)
            if len(candidates) >= candidate_cap:
                break
        if len(candidates) >= candidate_cap:
            break

    records: list[dict[str, str]] = []
    seen: set[str] = set()
    scanned = 0
    for candidate in list(candidates.values())[:candidate_cap]:
        scanned += 1
        try:
            record = fetch_detail_record(candidate, keywords=keywords)
        except Exception as exc:
            emit(progress, f"SC detail lookup failed for {candidate.get('source_record_id', '')}: {exc}")
            continue
        if not record.get("source_record_id") or record["id"] in seen:
            continue
        if record["status"].lower() == "cancelled":
            continue
        if not is_open_or_recent(record["posted_date"], record["due_date"], days_back):
            continue
        if keywords and not record["matched_keywords"]:
            continue
        if not useful_keyword_match(record["matched_keywords"].split(";"), record["raw_json"]):
            continue
        seen.add(record["id"])
        records.append(record)

    emit(progress, f"SC SCBO adsearch: {len(candidates)} candidates, {scanned} details scanned")
    return sorted(records, key=record_sort_key, reverse=True)[: max(1, max_records)]


def fetch_search_results(term: str) -> list[dict[str, Any]]:
    html, _final_url = http_text(SEARCH_URL, method="POST", data={"trm": term, "tsubmit": "submit"}, referer=SEARCH_URL)
    rows: list[dict[str, Any]] = []
    for href, title_attr, link_html, type_html in SEARCH_RESULT_RE.findall(html):
        detail_url = absolute_url(href)
        source_record_id = query_value(detail_url, "s")
        title = clean_text(title_attr or strip_html(link_html, 500), 500)
        document_type = clean_text(strip_html(type_html, 160), 160)
        if not source_record_id or not title:
            continue
        rows.append(
            {
                "source_record_id": source_record_id,
                "title": title,
                "document_type": document_type,
                "detail_url": detail_url,
                "search_terms": [term] if term else [],
            }
        )
    return rows


def fetch_detail_record(candidate: dict[str, Any], *, keywords: list[str]) -> dict[str, str]:
    detail_url = clean_text(candidate.get("detail_url"), 500)
    html, final_url = http_text(detail_url, method="GET", data=None, referer=SEARCH_URL)
    parsed = parse_detail(html)
    fields = parsed["fields"]
    field_hrefs = parsed["field_hrefs"]
    detail_text = " ".join(parsed["cell_texts"])

    source_record_id = clean_text(candidate.get("source_record_id"), 80)
    solicitation_number = first_field(fields, "solicitation_number", "project_number")
    title = first_field(fields, "ad_title", "project_name") or clean_text(candidate.get("title"), 500)
    if not title and first_field(fields, "vendor_name"):
        title = first_field(fields, "vendor_name")
    agency = first_field(fields, "purchasing_agent_entity", "agency_owner", "notice_of_intent_to_sole_source_by")
    posted_date = iso_date(first_field(fields, "ad_publish_date", "notice_publish_date"))
    due_date = iso_date(first_field(fields, "bid_submittal_due_date", "quote_due_date_time"))
    base_type = clean_text(candidate.get("document_type"), 160)
    doc_type = document_type(base_type, title, detail_text)
    status = status_from_row(doc_type, due_date)
    amount = amount_string(fields.get("contract_amount"))
    document_url = choose_document_url(field_hrefs, parsed["all_hrefs"], source_record_id)
    search_text = " ".join([source_record_id, solicitation_number, title, agency, doc_type, detail_text])
    matched = merge_matches(keyword_hits(search_text, keywords), candidate.get("search_terms") or [], keywords)
    raw = {
        "source_key": "sc_scbo",
        "source_note": SOURCE_NOTE,
        "detail_url": final_url or detail_url,
        "print_url": print_url(source_record_id),
        "search_terms": candidate.get("search_terms") or [],
        "search_result_title": clean_text(candidate.get("title"), 500),
        "search_result_type": base_type,
        "fields": fields,
        "field_hrefs": field_hrefs,
        "all_hrefs": parsed["all_hrefs"],
    }

    return {
        "id": stable_id("SC", source_record_id, solicitation_number, prefix="sc-scbo-ad"),
        "state": "SC",
        "source": "South Carolina Business Opportunities",
        "source_record_id": source_record_id if not solicitation_number else f"{source_record_id}; {solicitation_number}",
        "title": clean_text(title or source_record_id, 500),
        "agency": clean_text(agency, 180),
        "document_type": doc_type,
        "posted_date": posted_date,
        "due_date": due_date,
        "status": status,
        "amount": amount,
        "document_url": document_url,
        "source_url": final_url or detail_url,
        "matched_keywords": ";".join(matched),
        "relevance_score": str(relevance_score(matched, status, search_text, due_date)),
        "raw_json": compact_raw_json(raw),
        "last_checked_at": now_iso(),
    }


def parse_detail(html: str) -> dict[str, Any]:
    parser = DetailParser()
    parser.feed(html)
    fields: dict[str, str] = {}
    field_hrefs: dict[str, list[str]] = {}
    all_hrefs: list[str] = []
    cell_texts = [cell.text for cell in parser.cells if cell.text]
    for cell in parser.cells:
        all_hrefs.extend(cell.hrefs)

    index = 0
    while index < len(parser.cells):
        cell = parser.cells[index]
        if not is_label_cell(cell):
            index += 1
            continue
        key = normalize_label(cell.text)
        value = ""
        hrefs: list[str] = []
        if index + 1 < len(parser.cells) and not is_label_cell(parser.cells[index + 1]):
            value = parser.cells[index + 1].text
            hrefs = parser.cells[index + 1].hrefs
            index += 2
        else:
            index += 1
        if key and (value or hrefs):
            fields.setdefault(key, value)
            if hrefs:
                field_hrefs.setdefault(key, dedupe(hrefs))
    return {"fields": fields, "field_hrefs": field_hrefs, "all_hrefs": dedupe(all_hrefs), "cell_texts": cell_texts}


def is_label_cell(cell: DetailCell) -> bool:
    text = clean_text(cell.text, 500)
    key = normalize_label(text)
    return text.endswith(":") or key in KNOWN_LABELS


LABEL_ALIASES = {
    "solicitation": "solicitation_number",
    "solicitation_number": "solicitation_number",
    "buyer_phone": "buyer_phone",
    "buyer_email": "buyer_email",
    "full_details_download": "full_details_download",
    "bid_submittal_due_date": "bid_submittal_due_date",
    "quote_due_date_time": "quote_due_date_time",
    "notice_publish_date": "notice_publish_date",
    "ad_publish_date": "ad_publish_date",
    "notice_of_intent_to_sole_source_by": "notice_of_intent_to_sole_source_by",
    "agency_s_justification_for_a_sole_source_procurement_may_be_viewed_or_immediately_obtained_at": "sole_source_justification_url",
}
KNOWN_LABELS = set(LABEL_ALIASES.values()) | {
    "ad_title",
    "project_name",
    "agency_owner",
    "purchasing_agent_entity",
    "vendor_name",
    "contract_amount",
    "agency_procurement_manager",
    "direct_inquiries_to",
    "description",
    "pre_bid_information",
    "project_number",
    "construction_cost_range",
    "project_location",
    "agency_project_coordinator",
    "email",
    "telephone",
}


def normalize_label(value: str) -> str:
    text = clean_text(value, 500).strip(": ")
    text = text.replace("#", " number ")
    key = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return LABEL_ALIASES.get(key, key)


def first_field(fields: dict[str, str], *keys: str) -> str:
    for key in keys:
        value = clean_text(fields.get(key), 500)
        if value:
            return value
    return ""


def choose_document_url(field_hrefs: dict[str, list[str]], all_hrefs: list[str], source_record_id: str) -> str:
    for key in ("full_details_download", "sole_source_justification_url"):
        for href in field_hrefs.get(key) or []:
            if useful_document_href(href):
                return href
    for href in all_hrefs:
        if useful_document_href(href):
            return href
    return print_url(source_record_id)


def useful_document_href(href: str) -> bool:
    lower = href.lower()
    if not href or lower.startswith(("mailto:", "javascript:")):
        return False
    if "scbo.sc.gov/files/" in lower or "/themes/" in lower:
        return False
    if lower.rstrip("/") in {BASE_URL.rstrip("/"), DETAIL_URL.rstrip("/")}: 
        return False
    if any(part in lower for part in ["/audit", "/legal", "/training", "/polsub", "google-analytics", "googletagmanager"]):
        return False
    if "/online-edition" in lower or "/adsearch" in lower or "/search" in lower:
        return False
    return True


def document_type(base_type: str, title: str, detail_text: str) -> str:
    text = " ".join([base_type, title, detail_text]).upper()
    if "SOLE SOURCE" in text:
        return "SCBO Sole Source/Emergency Notice"
    if code_matches(text, "RFI") or "REQUEST FOR INFORMATION" in text:
        return "SCBO Request for Information"
    if code_matches(text, "RFP") or "REQUEST FOR PROPOSAL" in text:
        return "SCBO Request for Proposal"
    if code_matches(text, "RFQ") or "REQUEST FOR QUOTE" in text:
        return "SCBO Request for Quote"
    if code_matches(text, "IFB") or "INVITATION FOR BID" in text:
        return "SCBO Invitation for Bid"
    return f"SCBO {base_type}" if base_type else "SCBO Advertisement"


def status_from_row(document_type_value: str, due_date: str) -> str:
    lower = document_type_value.lower()
    if "award" in lower:
        return "Awarded"
    if "sole source" in lower:
        return "Notice"
    due = parse_date(due_date)
    if due and due < dt.date.today():
        return "Closed"
    if due:
        return "Open"
    return "Published"


def merge_candidate(candidates: dict[str, dict[str, Any]], row: dict[str, Any]) -> None:
    key = clean_text(row.get("source_record_id"), 80)
    if not key:
        return
    existing = candidates.get(key)
    if existing is None:
        candidates[key] = row
        return
    terms = list(existing.get("search_terms") or [])
    for term in row.get("search_terms") or []:
        if term and term not in terms:
            terms.append(term)
    existing["search_terms"] = terms


def http_text(url: str, *, method: str, data: dict[str, str] | None, referer: str) -> tuple[str, str]:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": referer,
    }
    last_error = ""
    for attempt in range(3):
        result = fetch_url(url, method=method, data=data, headers=headers, timeout=60, byte_limit=1_000_000, user_agent=USER_AGENT)
        if result.ok:
            return result.body_text(), result.final_url
        last_error = result.error or f"HTTP {result.status_code}"
        time.sleep(1 + attempt)
    raise RuntimeError(f"SC SCBO request failed for {url}: {last_error}")


def prioritized_search_terms(keywords: list[str]) -> list[str]:
    terms = dedupe([clean_text(keyword, 80) for keyword in keywords if clean_text(keyword, 80)])
    priority = [
        "medicaid",
        "mmis",
        "managed care",
        "provider data",
        "eligibility",
        "claims",
        "enrollment",
        "interoperability",
        "prior authorization",
        "fhir",
        "cms",
        "medicare",
        "quality measures",
        "behavioral health",
        "rural health",
        "telehealth",
    ]

    def key(term: str) -> tuple[int, int, str]:
        lower = term.lower()
        if lower in priority:
            return (0, priority.index(lower), lower)
        if " " in lower:
            return (1, 0, lower)
        return (2, 0, lower)

    return sorted(terms, key=key)


def merge_matches(direct_hits: list[str], search_terms: list[str], keywords: list[str]) -> list[str]:
    matches = list(direct_hits)
    keyword_lookup = {keyword.lower(): keyword for keyword in keywords}
    for term in search_terms:
        canonical = keyword_lookup.get(clean_text(term).lower(), clean_text(term))
        if canonical and canonical not in matches:
            matches.append(canonical)
    return sorted(matches, key=str.lower)


def useful_keyword_match(matches: list[str], text: str) -> bool:
    generic_terms = {"claims", "eligibility", "enrollment", "workforce"}
    matched_terms = {match.lower() for match in matches if match}
    if not matched_terms or not matched_terms <= generic_terms:
        return True
    context_terms = [
        "department of health and human services",
        "scdhhs",
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
        "disabilities",
    ]
    return any(term_matches(text, term) for term in context_terms)


def relevance_score(matches: list[str], status: str, text: str, due_date: str) -> int:
    score = min(50, len([match for match in matches if match]) * 10)
    if any(term_matches(text, term) for term in ["Medicaid", "MMIS", "Department of Health and Human Services", "SCDHHS", "Health Care", "Healthcare"]):
        score += 25
    if any(term_matches(text, term) for term in ["eligibility", "claims", "enrollment", "managed care", "interoperability", "FHIR", "prior authorization", "provider data"]):
        score += 15
    if term_matches(text, "rural health"):
        score += 25
    if any(term_matches(text, term) for term in ["RFP", "RFI", "RFQ", "software", "data", "cloud", "platform", "services"]):
        score += 12
    if status.lower() in {"open", "published", "notice"}:
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


def strip_html(value: str, limit: int) -> str:
    return clean_text(TAG_RE.sub(" ", value), limit)


def query_value(url: str, key: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    values = urllib.parse.parse_qs(parsed.query).get(key)
    return clean_text(values[0], 80) if values else ""


def print_url(source_record_id: str) -> str:
    return PRINT_URL + "?" + urllib.parse.urlencode({"a": source_record_id})


def absolute_url(href: str) -> str:
    return urllib.parse.urljoin(BASE_URL, clean_text(href, 500))


def attrs_dict(attrs: list[tuple[str, str | None]]) -> dict[str, str]:
    return {key: value or "" for key, value in attrs}


def code_matches(text: str, code: str) -> bool:
    return re.search(rf"(?<![A-Z0-9]){re.escape(code)}(?![A-Z0-9])", text, re.IGNORECASE) is not None


def dedupe(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


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
