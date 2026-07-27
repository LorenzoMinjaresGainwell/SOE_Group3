from __future__ import annotations

import datetime as dt
import re
import urllib.parse
from typing import Any, Callable

from services.state_http import fetch_url
from services.state_normalization import amount_string, clean_text, compact_raw_json, iso_date, keyword_hits, parse_date, stable_id, term_matches

BASE_URL = "https://www.vermontbusinessregistry.com/"
CATALOG_URL = urllib.parse.urljoin(BASE_URL, "BidSystem/")
STATE_BIDS_URL = urllib.parse.urljoin(BASE_URL, "BidSearch.aspx?type=5")
USER_AGENT = "Mozilla/5.0 soe-group3-vt-bid-system-opportunities/0.1"
SOURCE_NAME = "Vermont Business Registry Open State Bids"
SOURCE_NOTE = (
    "Official Vermont Business Registry and Bid System. The catalog /BidSystem/ URL returns 404, but the official home page "
    "links BidSearch.aspx?type=5 for Open State Bids; detail pages and bid attachments are public."
)
ROW_RE = re.compile(
    r"(?is)<a\b[^>]*href=[\"']javascript:openPrintView\('\s*(?P<href>BidPreview\.aspx\?BidID=(?P<bid_id>\d+))'[^\"']*[\"'][^>]*>(?P<title>.*?)</a>"
    r".*?<span\b[^>]*id=[\"']lblOrganization[\"'][^>]*>(?P<agency>.*?)</span>"
    r".*?Close Date:&nbsp;</span>\s*<span\b[^>]*id=[\"']lblCloseDate[\"'][^>]*>(?P<due_date>.*?)</span>"
)
TAG_RE = re.compile(r"(?is)<[^>]+>")
MAX_DETAIL_ROWS = 120


def fetch_opportunities(
    *,
    keywords: list[str],
    days_back: int,
    max_records: int,
    progress: Callable[[str], None] | None = None,
) -> list[dict[str, str]]:
    html, final_url = http_text(STATE_BIDS_URL, referer=BASE_URL)
    rows = parse_listing_rows(html, final_url=final_url or STATE_BIDS_URL)
    emit(progress, f"VT Business Registry open state bids: {len(rows)} public rows")

    records: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in rows[:MAX_DETAIL_ROWS]:
        row = dict(row)
        try:
            row["detail"] = fetch_detail(row["detail_url"])
        except Exception as exc:
            emit(progress, f"VT bid detail lookup failed for {row.get('source_record_id', '')}: {exc}")
            row["detail"] = {}
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


def parse_listing_rows(html: str, *, final_url: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for match in ROW_RE.finditer(html):
        detail_url = urllib.parse.urljoin(BASE_URL, clean_text(match.group("href"), 240))
        row = {
            "source_record_id": clean_text(match.group("bid_id"), 80),
            "title": strip_html(match.group("title"), 500),
            "agency": strip_html(match.group("agency"), 180),
            "due_date": strip_html(match.group("due_date"), 80),
            "detail_url": detail_url,
            "listing_url": final_url,
        }
        if row["source_record_id"] and row["title"]:
            rows.append(row)
    return rows


def fetch_detail(url: str) -> dict[str, Any]:
    html, final_url = http_text(url, referer=STATE_BIDS_URL)
    lines = html_lines(html)
    fields = detail_fields(lines)
    return {
        "final_url": final_url or url,
        "fields": fields,
        "attachments": attachment_links(html, final_url or url),
        "detail_text": " ".join(lines[:250]),
    }


def normalize_row(row: dict[str, Any], *, keywords: list[str]) -> dict[str, str]:
    detail = row.get("detail") if isinstance(row.get("detail"), dict) else {}
    fields = detail.get("fields") if isinstance(detail.get("fields"), dict) else {}
    attachments = detail.get("attachments") if isinstance(detail.get("attachments"), list) else []
    source_record_id = clean_text(row.get("source_record_id"), 80)
    title = clean_text(row.get("title") or source_record_id, 500)
    agency = clean_text(row.get("agency"), 180)
    posted_date = iso_date(fields.get("Request Date"))
    open_date = iso_date(fields.get("Open Date"))
    due_date = iso_date(fields.get("Closing Date") or row.get("due_date"))
    amount = amount_string(fields.get("Est. Dollar Value"))
    bid_type = clean_text(fields.get("Bid Type"), 120)
    description = clean_text(fields.get("Bid Description"), 2500)
    keywords_text = clean_text(fields.get("Keywords"), 500)
    special = clean_text(fields.get("Special Instructions"), 1500)
    search_text = expand_related_terms(" ".join([source_record_id, title, agency, bid_type, keywords_text, description, special]))
    matched = keyword_hits(search_text, keywords)
    document_url = first_attachment_url(attachments) or clean_text(row.get("detail_url"), 500) or STATE_BIDS_URL
    status = status_from_due_date(due_date)
    raw = {
        "source_key": "vt_bid_system",
        "source_note": SOURCE_NOTE,
        "catalog_url": CATALOG_URL,
        "state_bids_url": STATE_BIDS_URL,
        "listing_row": row_without_detail(row),
        "open_date": open_date,
        "detail": detail,
    }

    return {
        "id": stable_id("VT", source_record_id, prefix="vt-bid-system"),
        "state": "VT",
        "source": SOURCE_NAME,
        "source_record_id": source_record_id,
        "title": title,
        "agency": agency,
        "document_type": document_type(bid_type, source_record_id, title),
        "posted_date": posted_date or open_date,
        "due_date": due_date,
        "status": status,
        "amount": amount if amount != "0" else "",
        "document_url": document_url,
        "source_url": clean_text(row.get("detail_url") or STATE_BIDS_URL, 500),
        "matched_keywords": ";".join(matched),
        "relevance_score": str(relevance_score(matched, status, search_text, due_date)),
        "raw_json": compact_raw_json(raw, limit=10000),
        "last_checked_at": now_iso(),
    }


def detail_fields(lines: list[str]) -> dict[str, str]:
    labels = [
        "Request Date",
        "Open Date",
        "Closing Date",
        "Intent To Bid Deadline",
        "Est. Dollar Value",
        "RFQ Number",
        "Bid Type",
        "Locations",
        "Keywords",
        "Bid Description",
        "Special Instructions",
        "Contact Information",
    ]
    fields: dict[str, str] = {}
    label_set = {label.lower(): label for label in labels}
    index = 0
    while index < len(lines):
        label = label_set.get(lines[index].rstrip(":").lower())
        if not label:
            index += 1
            continue
        values: list[str] = []
        index += 1
        while index < len(lines):
            next_label = label_set.get(lines[index].rstrip(":").lower())
            if next_label or lines[index] in {"Phone:", "Fax:", "Email:", "For additional information:", "Bid Attachments:", "Site Visit:", "Date:"}:
                break
            if lines[index]:
                values.append(lines[index])
            index += 1
        fields[label] = clean_text(" ".join(values), 3000)
    return fields


def attachment_links(html: str, base_url: str) -> list[dict[str, str]]:
    links: list[dict[str, str]] = []
    for href, text in re.findall(r"(?is)<a\b[^>]*href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", html):
        url = urllib.parse.urljoin(base_url, clean_text(href, 500))
        title = strip_html(text, 240)
        if "bidAttachments/" not in url and not re.search(r"\.(pdf|docx?|xlsx?|zip)(?:$|[?#])", url, re.IGNORECASE):
            continue
        links.append({"title": title or url.rsplit("/", 1)[-1], "url": url})
    return links


def first_attachment_url(attachments: list[Any]) -> str:
    for item in attachments:
        if isinstance(item, dict) and clean_text(item.get("url")):
            return clean_text(item.get("url"), 500)
    return ""


def html_lines(html: str) -> list[str]:
    text = re.sub(r"(?i)<br\s*/?>", "\n", html)
    text = re.sub(r"(?i)</(?:tr|td|div|span|p|li|h\d)>", "\n", text)
    text = TAG_RE.sub("\n", text)
    return [clean_text(part, 3000) for part in text.splitlines() if clean_text(part)]


def row_without_detail(row: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in row.items() if key != "detail"}


def document_type(bid_type: str, source_record_id: str, title: str) -> str:
    kind = clean_text(bid_type, 120)
    text = " ".join([kind, source_record_id, title]).upper()
    if "REQUEST FOR INFORMATION" in text or code_matches(text, "RFI"):
        return "Vermont Request for Information"
    if "REQUEST FOR PROPOS" in text or code_matches(text, "RFP"):
        return "Vermont Request for Proposal"
    if "REQUEST FOR QUOTE" in text or code_matches(text, "RFQ"):
        return "Vermont Request for Quote"
    if "INVITATION" in text and "BID" in text:
        return "Vermont Invitation to Bid"
    if kind:
        return f"Vermont {kind}"
    return "Vermont Bid"


def status_from_due_date(due_date: str) -> str:
    due = parse_date(due_date)
    if due and due < dt.date.today():
        return "Closed"
    return "Open"


def expand_related_terms(text: str) -> str:
    expanded = text
    if any(term_matches(text, term) for term in ["Health", "Human Services", "Medicaid", "Medicare", "MMIS", "Mental Health"]):
        expanded += " Medicaid MMIS managed care eligibility claims human services provider health care behavioral health"
    if any(term_matches(text, term) for term in ["hospital", "community", "data", "system", "software"]):
        expanded += " health care services program"
    return expanded


def useful_keyword_match(matches: list[str], text: str) -> bool:
    generic_terms = {"claims", "eligibility", "enrollment", "provider", "provider data", "workforce", "cms"}
    matched_terms = {match.lower() for match in matches if match}
    if not matched_terms or not matched_terms <= generic_terms:
        return True
    context_terms = [
        "agency of human services",
        "human services",
        "healthcare",
        "health care",
        "health",
        "medicaid",
        "medicare",
        "medical",
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
    if any(term_matches(text, term) for term in ["managed care", "eligibility", "claims", "provider data", "behavioral health"]):
        score += 18
    if any(term_matches(text, term) for term in ["interoperability", "FHIR", "prior authorization", "data", "system"]):
        score += 12
    if any(term_matches(text, term) for term in ["RFP", "RFI", "RFQ", "services", "software", "program"]):
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
    return (int_or_zero(row.get("relevance_score")), row.get("due_date", ""), row.get("posted_date", ""))


def http_text(url: str, *, referer: str) -> tuple[str, str]:
    result = fetch_url(
        url,
        headers={
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": referer,
        },
        timeout=60,
        byte_limit=2_000_000,
        user_agent=USER_AGENT,
    )
    result.raise_for_status()
    return result.body_text(), result.final_url


def strip_html(value: Any, limit: int = 1000) -> str:
    return clean_text(TAG_RE.sub(" ", str(value or "")), limit)


def code_matches(text: str, code: str) -> bool:
    return re.search(rf"(?<![A-Z0-9]){re.escape(code)}(?![A-Z0-9])", text, re.IGNORECASE) is not None


def int_or_zero(value: str | None) -> int:
    try:
        return int(float(value or 0))
    except ValueError:
        return 0


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def emit(progress: Callable[[str], None] | None, message: str) -> None:
    if progress:
        progress(message)
