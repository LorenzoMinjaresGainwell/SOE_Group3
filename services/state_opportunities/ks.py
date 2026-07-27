from __future__ import annotations

import datetime as dt
import http.cookiejar
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

from services.state_normalization import clean_text, compact_raw_json, iso_date, keyword_hits, parse_date, stable_id, term_matches

ADMIN_URL = "https://admin.ks.gov/offices/procurement-contracts/bidding--contracts"
PAGE_URL = "https://supplier.sok.ks.gov/psc/sokfsprdsup/SUPPLIER/ERP/c/SCP_PUBLIC_MENU_FL.SCP_PUB_BID_CMP_FL.GBL"
USER_AGENT = "Mozilla/5.0 soe-group3-ks-esupplier-opportunities/0.1"
SOURCE_NAME = "Kansas eSupplier Public Bid Solicitations"
SOURCE_NOTE = (
    "Official Kansas Department of Administration Bidding & Contracts page links the eSupplier public bid component. "
    "The public component returns current solicitation rows after normal cookie handling; no login, CAPTCHA, or browser automation used."
)
ROW_RE = re.compile(r"(?is)<tr\b[^>]*id=['\"]SCP_PUB_AUC_VW\$0_row_(\d+)['\"][^>]*>(.*?)</tr>")
TAG_RE = re.compile(r"(?is)<[^>]+>")
MAX_HTML_BYTES = 2_000_000


def fetch_opportunities(
    *,
    keywords: list[str],
    days_back: int,
    max_records: int,
    progress: Callable[[str], None] | None = None,
) -> list[dict[str, str]]:
    html, final_url = fetch_listing_html()
    rows = parse_rows(html)
    emit(progress, f"KS eSupplier public bid solicitations: {len(rows)} public rows")

    records: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in rows:
        record = normalize_row(row, source_url=final_url, keywords=keywords)
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


def fetch_listing_html() -> tuple[str, str]:
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": ADMIN_URL,
    }
    last_error: Exception | None = None
    for attempt in range(3):
        request = urllib.request.Request(PAGE_URL, headers=headers)
        try:
            with opener.open(request, timeout=60) as response:
                body = response.read(MAX_HTML_BYTES + 1)
                html = body[:MAX_HTML_BYTES].decode("utf-8", "replace")
                final_url = response.geturl()
                if "cmd=login" in final_url or "SCP_PUB_AUC_VW" not in html:
                    raise RuntimeError(f"KS eSupplier returned non-public page: {final_url}")
                return html, final_url
        except (urllib.error.URLError, TimeoutError, OSError, RuntimeError) as exc:
            last_error = exc
            time.sleep(1 + attempt)
    raise RuntimeError(f"KS eSupplier public bid request failed: {last_error}")


def parse_rows(html: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for index, block in ROW_RE.findall(html):
        row = {
            "title": field_text(block, "SCP_PUB_AUC_VW_AUC_NAME", index),
            "agency": field_text(block, "BUS_UNIT_AUC_VW_DESCR", index),
            "event_id": field_text(block, "SCP_PUB_AUC_VW_AUC_ID", index),
            "starts_at": field_text(block, "SCP_COSP_WK_FL_SCP_STRT_DATE_CHAR", index),
            "ends_at": field_text(block, "SCP_COSP_WK_FL_SCP_END_DATE_CHAR", index),
            "ends_in": field_text(block, "SCP_COSP_WK_FL_HTML_AREA_03", index),
            "row_index": index,
        }
        if row["event_id"] and row["title"]:
            rows.append(row)
    return rows


def field_text(block: str, field_id: str, index: str) -> str:
    pattern = rf"(?is)<(?P<tag>span|div)\b[^>]*id=['\"](?:win0div)?{re.escape(field_id)}\${re.escape(index)}['\"][^>]*>(.*?)</(?P=tag)>"
    match = re.search(pattern, block)
    return strip_html(match.group(2), 500) if match else ""


def normalize_row(row: dict[str, Any], *, source_url: str, keywords: list[str]) -> dict[str, str]:
    source_record_id = clean_text(row.get("event_id"), 160)
    title = clean_text(row.get("title") or source_record_id, 500)
    agency = clean_text(row.get("agency"), 180)
    posted_date = iso_date(row.get("starts_at"))
    due_date = iso_date(row.get("ends_at"))
    status = status_from_due_date(due_date)
    search_text = expand_related_terms(" ".join([source_record_id, title, agency, clean_text(row.get("ends_in"), 120)]))
    matched = keyword_hits(search_text, keywords)
    raw = {
        "source_key": "ks_esupplier",
        "source_note": SOURCE_NOTE,
        "admin_page_url": ADMIN_URL,
        "portal_url": PAGE_URL,
        "row": row,
    }

    return {
        "id": stable_id("KS", source_record_id or title, prefix="ks-esupplier-event"),
        "state": "KS",
        "source": SOURCE_NAME,
        "source_record_id": source_record_id,
        "title": title,
        "agency": agency,
        "document_type": document_type(source_record_id, title),
        "posted_date": posted_date,
        "due_date": due_date,
        "status": status,
        "amount": "",
        "document_url": PAGE_URL,
        "source_url": source_url or PAGE_URL,
        "matched_keywords": ";".join(matched),
        "relevance_score": str(relevance_score(matched, status, search_text, due_date)),
        "raw_json": compact_raw_json(raw, limit=10000),
        "last_checked_at": now_iso(),
    }


def document_type(source_record_id: str, title: str) -> str:
    text = " ".join([source_record_id, title]).upper()
    if code_matches(text, "RFI") or "REQUEST FOR INFORMATION" in text:
        return "Kansas Request for Information"
    if code_matches(text, "RFP") or "REQUEST FOR PROPOS" in text:
        return "Kansas Request for Proposal"
    if code_matches(text, "RFQ") or "REQUEST FOR QUOTE" in text:
        return "Kansas Request for Quote"
    if code_matches(text, "IFB") or code_matches(text, "ITB") or "INVITATION" in text:
        return "Kansas Invitation to Bid"
    return "Kansas Bid Solicitation"


def status_from_due_date(due_date: str) -> str:
    due = parse_date(due_date)
    if due and due < dt.date.today():
        return "Closed"
    return "Open"


def expand_related_terms(text: str) -> str:
    expanded = text
    if any(term_matches(text, term) for term in ["Health", "KDHE", "KDADS", "KanCare", "Medicaid", "MMIS"]):
        expanded += " Medicaid MMIS managed care eligibility claims human services provider health care behavioral health"
    if any(term_matches(text, term) for term in ["Children and Families", "DCF", "Aging", "Disability", "Library"]):
        expanded += " human services health care provider workforce"
    if any(term_matches(text, term) for term in ["Application", "Digital", "System", "Software", "Data"]):
        expanded += " system software data interoperability services"
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
        "children and families",
        "medicaid",
        "medicare",
        "medical",
        "behavioral",
        "managed care",
        "provider",
        "chip",
        "mmis",
        "kdhe",
        "kdads",
        "kancare",
    ]
    return any(term_matches(text, term) for term in context_terms)


def relevance_score(matches: list[str], status: str, text: str, due_date: str) -> int:
    score = min(50, len([match for match in matches if match]) * 10)
    if any(term_matches(text, term) for term in ["Medicaid", "MMIS", "Health", "KDHE", "KDADS", "KanCare"]):
        score += 28
    if any(term_matches(text, term) for term in ["managed care", "eligibility", "claims", "provider data", "behavioral health"]):
        score += 18
    if any(term_matches(text, term) for term in ["interoperability", "FHIR", "prior authorization", "application", "system", "software", "data"]):
        score += 15
    if any(term_matches(text, term) for term in ["RFP", "RFI", "RFQ", "ITB", "services", "digital", "library"]):
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
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def emit(progress: Callable[[str], None] | None, message: str) -> None:
    if progress:
        progress(message)
