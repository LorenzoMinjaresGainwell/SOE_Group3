from __future__ import annotations

import datetime as dt
import html
import re
import urllib.parse
from typing import Any, Callable

from services.state_http import fetch_url
from services.state_normalization import clean_text, compact_raw_json, iso_date, keyword_hits, parse_date, stable_id, term_matches

BASE_URL = "https://app.az.gov/"
PUBLIC_RFP_URL = urllib.parse.urljoin(BASE_URL, "page.aspx/en/rfp/request_browse_public")
USER_AGENT = "Mozilla/5.0 soe-group3-az-app-opportunities/0.1"
SOURCE_NOTE = (
    "Official Arizona Procurement Portal public RFP browse table at /page.aspx/en/rfp/request_browse_public; "
    "row detail links redirect to browser_check/reCAPTCHA from CLI, so only public table fields are normalized."
)
TABLE_ID = "body_x_grid_grd"
TAG_RE = re.compile(r"(?is)<[^>]+>")


def fetch_opportunities(
    *,
    keywords: list[str],
    days_back: int,
    max_records: int,
    progress: Callable[[str], None] | None = None,
) -> list[dict[str, str]]:
    rows = fetch_public_rows()
    emit(progress, f"AZ APP public browse table: {len(rows)} public rows on first page")

    records: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in rows:
        record = normalize_row(row, keywords=keywords)
        if not record.get("source_record_id") or record["id"] in seen:
            continue
        if not is_open_or_recent(record["posted_date"], record["due_date"], record["status"], days_back):
            continue
        if keywords and not record["matched_keywords"]:
            continue
        if false_keyword_hit(record) or not useful_keyword_match(record["matched_keywords"].split(";"), record["raw_json"]):
            continue
        seen.add(record["id"])
        records.append(record)

    return sorted(records, key=record_sort_key, reverse=True)[: max(1, max_records)]


def fetch_public_rows() -> list[dict[str, str]]:
    result = fetch_url(
        PUBLIC_RFP_URL,
        headers={"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"},
        timeout=60,
        byte_limit=2_000_000,
        user_agent=USER_AGENT,
    )
    result.raise_for_status()
    return parse_public_rows(result.body_text())


def parse_public_rows(page_html: str) -> list[dict[str, str]]:
    table_match = re.search(rf'(?is)<table\b[^>]*id=["\']{re.escape(TABLE_ID)}["\'][^>]*>(.*?)</table>', page_html)
    if not table_match:
        return []

    rows: list[dict[str, str]] = []
    for attrs, row_html in re.findall(r"(?is)<tr\b([^>]*)>(.*?)</tr>", table_match.group(1)):
        cells = [strip_html(cell, 1000) for cell in re.findall(r"(?is)<t[dh]\b[^>]*>(.*?)</t[dh]>", row_html)]
        if len(cells) < 8 or cells[1].lower() == "code":
            continue
        row_id = first_match(attrs, r'data-id=["\']([^"\']+)') or first_match(attrs, r'id=["\'][^"\']*tr_(\d+)')
        hrefs = [urllib.parse.urljoin(PUBLIC_RFP_URL, href) for href in re.findall(r"(?is)<a\b[^>]*href=[\"']([^\"']+)", row_html)]
        detail_url = next((href for href in hrefs if "/bpm/process_manage_extranet/" in href), hrefs[0] if hrefs else "")
        rows.append(
            {
                "row_id": row_id,
                "detail_url": detail_url,
                "code": cells[1],
                "label": cells[2],
                "publication_begin": cells[3],
                "commodity": cells[4],
                "agency": cells[5],
                "publication_end": cells[6],
                "status": cells[7],
                "rfx_awarded": cells[8] if len(cells) > 8 else "",
                "remaining_time": cells[9] if len(cells) > 9 else "",
                "begin_date": cells[10] if len(cells) > 10 else "",
                "end_date": cells[11] if len(cells) > 11 else "",
            }
        )
    return rows


def normalize_row(row: dict[str, str], *, keywords: list[str]) -> dict[str, str]:
    source_record_id = clean_text(row.get("code") or row.get("row_id"), 180)
    title = clean_text(row.get("label") or source_record_id, 500)
    agency = clean_text(row.get("agency"), 180)
    commodity = clean_text(row.get("commodity"), 200)
    status = clean_text(row.get("status") or "Open", 80)
    posted_date = iso_date(row.get("publication_begin") or row.get("begin_date"))
    due_date = iso_date(row.get("end_date") or row.get("publication_end"))
    detail_url = clean_text(row.get("detail_url"), 500)
    search_text = expand_related_terms(" ".join([source_record_id, title, agency, commodity, status]))
    matched = keyword_hits(search_text, keywords)
    raw = {
        "source_key": "az_app",
        "source_note": SOURCE_NOTE,
        "detail_url_browser_check": detail_url,
        "public_table_row": row,
    }

    return {
        "id": stable_id("AZ", source_record_id or row.get("row_id"), prefix="az-app-rfp"),
        "state": "AZ",
        "source": "Arizona Procurement Portal Public RFPs",
        "source_record_id": source_record_id,
        "title": title,
        "agency": agency,
        "document_type": document_type(source_record_id, title),
        "posted_date": posted_date,
        "due_date": due_date,
        "status": status,
        "amount": "",
        "document_url": detail_url or PUBLIC_RFP_URL,
        "source_url": PUBLIC_RFP_URL,
        "matched_keywords": ";".join(matched),
        "relevance_score": str(relevance_score(matched, status, search_text, due_date)),
        "raw_json": compact_raw_json(raw, limit=10000),
        "last_checked_at": now_iso(),
    }


def document_type(source_record_id: str, title: str) -> str:
    text = " ".join([source_record_id, title]).upper()
    if "REQUEST FOR INFORMATION" in text or code_matches(text, "RFI"):
        return "Arizona Procurement Portal Request for Information"
    if "REQUEST FOR PROPOS" in text or code_matches(text, "RFP"):
        return "Arizona Procurement Portal Request for Proposal"
    if "REQUEST FOR QUAL" in text or code_matches(text, "RFQ"):
        return "Arizona Procurement Portal Request for Qualifications"
    if "REQUEST FOR QUOTE" in text:
        return "Arizona Procurement Portal Request for Quote"
    if "INVITATION TO BID" in text or code_matches(text, "IFB") or code_matches(text, "ITB"):
        return "Arizona Procurement Portal Invitation to Bid"
    return "Arizona Procurement Portal Opportunity"


def expand_related_terms(text: str) -> str:
    expanded = text
    if any(term_matches(text, term) for term in ["AHCCCS", "Arizona Health Care Cost Containment System"]):
        expanded += " Medicaid managed care MMIS eligibility claims"
    if any(term_matches(text, term) for term in ["Department of Health Services", "DHS"]):
        expanded += " health Medicaid Medicare behavioral health"
    if any(term_matches(text, term) for term in ["Department of Economic Security", "DES"]):
        expanded += " eligibility enrollment human services"
    if term_matches(text, "RHTP"):
        expanded += " rural health rural health transformation"
    return expanded


def useful_keyword_match(matches: list[str], text: str) -> bool:
    ambiguous_terms = {"claims", "eligibility", "enrollment", "cms", "workforce"}
    matched_terms = {match.lower() for match in matches if match}
    if not matched_terms or not matched_terms <= ambiguous_terms:
        return True
    context_terms = [
        "ahcccs",
        "arizona health care cost containment system",
        "department of health services",
        "department of economic security",
        "human services",
        "healthcare",
        "health care",
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


def false_keyword_hit(record: dict[str, str]) -> bool:
    text = " ".join([record.get("title", ""), record.get("agency", ""), record.get("raw_json", "")])
    return term_matches(text, "commissary") and not term_matches(text, "MMIS")


def relevance_score(matches: list[str], status: str, text: str, due_date: str) -> int:
    score = min(50, len([match for match in matches if match]) * 10)
    if any(term_matches(text, term) for term in ["Medicaid", "MMIS", "AHCCCS", "Department of Health Services"]):
        score += 30
    if any(term_matches(text, term) for term in ["eligibility", "claims", "enrollment", "managed care", "provider data"]):
        score += 18
    if any(term_matches(text, term) for term in ["interoperability", "FHIR", "prior authorization", "telehealth", "quality measures"]):
        score += 15
    if any(term_matches(text, term) for term in ["rural health", "rural health transformation", "critical access hospital"]):
        score += 25
    if any(term_matches(text, term) for term in ["RFP", "RFI", "RFQ", "software", "data", "analytics", "contact center"]):
        score += 10
    if "open" in status.lower():
        score += 10
    due = parse_date(due_date)
    if due and due >= dt.date.today():
        score += 5
    return min(score, 100)


def is_open_or_recent(posted_date: str, due_date: str, status: str, days_back: int) -> bool:
    if "open" in status.lower():
        return True
    due = parse_date(due_date)
    if due and due >= dt.date.today():
        return True
    if days_back <= 0:
        return True
    posted = parse_date(posted_date)
    return not posted or (dt.date.today() - posted).days <= days_back


def strip_html(value: str, limit: int) -> str:
    return clean_text(html.unescape(TAG_RE.sub(" ", value)), limit)


def first_match(text: str, pattern: str) -> str:
    match = re.search(pattern, text, re.IGNORECASE)
    return clean_text(match.group(1), 120) if match else ""


def code_matches(text: str, code: str) -> bool:
    return re.search(rf"(?<![A-Z0-9]){re.escape(code)}(?![A-Z0-9])", text, re.IGNORECASE) is not None


def record_sort_key(row: dict[str, str]) -> tuple[int, str, str]:
    return (int_or_zero(row.get("relevance_score")), row.get("due_date", ""), row.get("posted_date", ""))


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
