from __future__ import annotations

import datetime as dt
import json
import re
import time
import urllib.parse
from typing import Any, Callable

from services.state_http import fetch_url
from services.state_normalization import clean_id, clean_text, compact_raw_json, iso_date, keyword_hits, parse_date, stable_id, term_matches

BID_BOARD_URL = "https://missouribuys.mo.gov/bid-board/movers"
LISTING_URL = "https://ewqg.fa.us8.oraclecloud.com/fscmUI/redwood/negotiation-abstracts/view/abstractlisting?prcBuId=300000005255687&ojSpLang=en"
REST_URL = "https://ewqg.fa.us8.oraclecloud.com/fscmRestApi/resources/latest/supplierNegotiationAbstracts"
PROCUREMENT_BU_ID = "300000005255687"
USER_AGENT = "soe-group3-mo-missouribuys-opportunities/0.1"
SOURCE_NAME = "MissouriBUYS MOVERS Solicitation Abstracts"
SOURCE_NOTE = (
    "Official MissouriBUYS MOVERS Oracle Fusion Cloud public REST resource supplierNegotiationAbstracts; "
    "queried with RowFinderByBU and active solicitation status."
)
PAGE_SIZE = 100
MAX_SCAN_ROWS = 1000


def fetch_opportunities(
    *,
    keywords: list[str],
    days_back: int,
    max_records: int,
    progress: Callable[[str], None] | None = None,
) -> list[dict[str, str]]:
    rows = fetch_active_rows(max_records=max_records, progress=progress)
    emit(progress, f"MO MissouriBUYS active abstracts: {len(rows)} public rows")

    records: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in rows:
        record = normalize_row(row, keywords=keywords)
        if not record.get("source_record_id") or record["id"] in seen:
            continue
        if record["status"].lower() in {"canceled", "cancelled", "closed"}:
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


def fetch_active_rows(*, max_records: int, progress: Callable[[str], None] | None = None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    offset = 0
    max_to_scan = min(MAX_SCAN_ROWS, max(PAGE_SIZE, max(1, max_records) * 20))

    while len(rows) < max_to_scan:
        payload = fetch_page(offset=offset, limit=min(PAGE_SIZE, max_to_scan - len(rows)))
        batch = valid_rows(payload.get("items"))
        emit(progress, f"MO MissouriBUYS REST offset={offset}: {len(batch)} rows")
        if not batch:
            break
        rows.extend(batch)
        if not payload.get("hasMore") or len(batch) < PAGE_SIZE:
            break
        offset += len(batch)
        time.sleep(0.15)

    return rows[:max_to_scan]


def fetch_page(*, offset: int, limit: int) -> dict[str, Any]:
    params = {
        "finder": f"RowFinderByBU;ProcurementBUId={PROCUREMENT_BU_ID}",
        "q": 'NegotiationStatusCode="ACTIVE"',
        "limit": str(limit),
        "offset": str(offset),
        "onlyData": "true",
        "orderBy": "CloseDate:asc",
    }
    url = REST_URL + "?" + urllib.parse.urlencode(params)
    result = fetch_url(
        url,
        headers={"Accept": "application/json", "Referer": LISTING_URL},
        timeout=60,
        byte_limit=2_000_000,
        user_agent=USER_AGENT,
    )
    result.raise_for_status()
    data = json.loads(result.body_text())
    if not isinstance(data, dict):
        raise RuntimeError("MO MissouriBUYS REST response root was not an object")
    return data


def normalize_row(row: dict[str, Any], *, keywords: list[str]) -> dict[str, str]:
    auction_header_id = clean_id(row.get("AuctionHeaderId"))
    source_record_id = clean_text(row.get("Negotiation") or auction_header_id, 180)
    title = clean_text(row.get("NegotiationTitle") or source_record_id, 500)
    agency = agency_name(row)
    status = clean_text(row.get("NegotiationStatus") or row.get("NegotiationStatusCode") or "Active", 80)
    posted_date = iso_date(row.get("PostingDate") or row.get("PublishDate") or row.get("OpenDate"))
    due_date = iso_date(row.get("CloseDate"))
    detail_url = detail_page_url(row)
    search_text = expand_related_terms(row_search_text(row))
    matched = keyword_hits(search_text, keywords)
    raw = {
        "source_key": "mo_missouribuys",
        "source_note": SOURCE_NOTE,
        "bid_board_url": BID_BOARD_URL,
        "listing_url": LISTING_URL,
        "rest_resource": REST_URL,
        "row": row,
    }

    return {
        "id": stable_id("MO", auction_header_id or source_record_id, prefix="mo-missouribuys-opportunity"),
        "state": "MO",
        "source": SOURCE_NAME,
        "source_record_id": source_record_id,
        "title": title,
        "agency": agency,
        "document_type": document_type(row, source_record_id, title),
        "posted_date": posted_date,
        "due_date": due_date,
        "status": status,
        "amount": "",
        "document_url": detail_url,
        "source_url": LISTING_URL,
        "matched_keywords": ";".join(matched),
        "relevance_score": str(relevance_score(matched, status, search_text, due_date)),
        "raw_json": compact_raw_json(raw, limit=10000),
        "last_checked_at": now_iso(),
    }


def agency_name(row: dict[str, Any]) -> str:
    synopsis = clean_text(row.get("Synopsis"), 1000)
    match = re.search(r"on behalf of (?:the )?([^.;]+)", synopsis, re.IGNORECASE)
    if match:
        return clean_text(match.group(1), 180)
    return clean_text(row.get("ProcurementBUName"), 180)


def detail_page_url(row: dict[str, Any]) -> str:
    auction_header_id = clean_id(row.get("AuctionHeaderId"))
    procurement_bu_id = clean_id(row.get("ProcurementBUId") or row.get("filterPrcBUId") or PROCUREMENT_BU_ID)
    params = {
        "auctionHeaderId": auction_header_id,
        "prcBuId": procurement_bu_id,
        "ojSpLang": "en",
    }
    return "https://ewqg.fa.us8.oraclecloud.com/fscmUI/redwood/negotiation-abstracts/view/abstract?" + urllib.parse.urlencode(params)


def row_search_text(row: dict[str, Any]) -> str:
    return " ".join(
        clean_text(part, 4000)
        for part in [
            row.get("AuctionHeaderId"),
            row.get("Negotiation"),
            row.get("NegotiationTitle"),
            row.get("NegotiationType"),
            row.get("NegotiationStatus"),
            row.get("ProcurementBUName"),
            row.get("BuyerName"),
            row.get("BuyerEmailAddress"),
            row.get("Synopsis"),
            row.get("AmendmentDescription"),
        ]
        if part
    )


def document_type(row: dict[str, Any], source_record_id: str, title: str) -> str:
    negotiation_type = clean_text(row.get("NegotiationType"), 160)
    text = " ".join([source_record_id, title, negotiation_type]).upper()
    if "REQUEST FOR INFORMATION" in text or code_matches(text, "RFI"):
        return "MissouriBUYS Request for Information"
    if "REQUEST FOR PROPOS" in text or code_matches(text, "RFP"):
        return "MissouriBUYS Request for Proposal"
    if "REQUEST FOR QUOT" in text or code_matches(text, "RFQ"):
        return "MissouriBUYS Request for Quote"
    if "INVITATION" in text or code_matches(text, "IFB"):
        return "MissouriBUYS Invitation for Bid"
    if negotiation_type:
        return f"MissouriBUYS {negotiation_type}"
    return "MissouriBUYS Solicitation"


def expand_related_terms(text: str) -> str:
    expanded = text
    if term_matches(text, "MO HealthNet") or term_matches(text, "MHD"):
        expanded += " Medicaid managed care eligibility claims human services"
    if any(term_matches(text, term) for term in ["Health", "Healthcare", "Medical", "Hospital", "Behavioral"]):
        expanded += " health care medical provider"
    if term_matches(text, "MHD"):
        expanded += " MO HealthNet Medicaid"
    return expanded


def useful_keyword_match(matches: list[str], text: str) -> bool:
    generic_terms = {"claims", "eligibility", "enrollment", "provider", "provider data", "workforce", "cms", "interoperability"}
    matched_terms = {match.lower() for match in matches if match}
    if not matched_terms or not matched_terms <= generic_terms:
        return True
    context_terms = [
        "mo healthnet",
        "department of social services",
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
        "dss",
    ]
    return any(term_matches(text, term) for term in context_terms)


def false_keyword_hit(record: dict[str, str]) -> bool:
    text = " ".join([record.get("title", ""), record.get("agency", ""), record.get("raw_json", "")])
    return "mmis" in {item.lower() for item in record.get("matched_keywords", "").split(";") if item} and term_matches(text, "commissary") and not term_matches(text, "MMIS")


def relevance_score(matches: list[str], status: str, text: str, due_date: str) -> int:
    score = min(50, len([match for match in matches if match]) * 10)
    if any(term_matches(text, term) for term in ["Medicaid", "MMIS", "MO HealthNet", "Department of Social Services"]):
        score += 30
    if any(term_matches(text, term) for term in ["managed care", "eligibility", "claims", "provider data"]):
        score += 18
    if any(term_matches(text, term) for term in ["interoperability", "FHIR", "prior authorization", "quality measures", "care management"]):
        score += 15
    if any(term_matches(text, term) for term in ["rural health", "critical access hospital", "hospital"]):
        score += 20
    if any(term_matches(text, term) for term in ["RFP", "RFI", "RFQ", "software", "system", "data", "platform", "services"]):
        score += 10
    if status.lower() == "active":
        score += 10
    due = parse_date(due_date)
    if due and due >= dt.date.today():
        score += 5
    return min(score, 100)


def is_open_or_recent(posted_date: str, due_date: str, days_back: int) -> bool:
    due = parse_date(due_date)
    if due and due >= dt.date.today():
        return True
    if days_back <= 0:
        return True
    posted = parse_date(posted_date)
    return not posted or (dt.date.today() - posted).days <= days_back


def valid_rows(value: Any) -> list[dict[str, Any]]:
    return [row for row in value or [] if isinstance(row, dict)]


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
