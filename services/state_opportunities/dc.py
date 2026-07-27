from __future__ import annotations

import datetime as dt
import json
import re
import urllib.parse
from typing import Any, Callable

from services.state_http import fetch_url
from services.state_normalization import clean_text, compact_raw_json, iso_date, keyword_hits, parse_date, stable_id, term_matches

BASE_URL = "https://contracts.ocp.dc.gov/"
SEARCH_PAGE_URL = urllib.parse.urljoin(BASE_URL, "solicitations/search")
RESULTS_ENDPOINT = urllib.parse.urljoin(BASE_URL, "api/solicitations/search")
DETAILS_ENDPOINT = urllib.parse.urljoin(BASE_URL, "api/solicitations/details")
USER_AGENT = "Mozilla/5.0 soe-group3-dc-ocp-opportunities/0.1"
SOURCE_NAME = "DC OCP Open Solicitations"
SOURCE_NOTE = (
    "Official DC OCP Transparency Angular API; open solicitations require POST "
    "FilterBy Type=0 and Status=Open to /api/solicitations/search."
)
MAX_DETAIL_ROWS = 80


def fetch_opportunities(
    *,
    keywords: list[str],
    days_back: int,
    max_records: int,
    progress: Callable[[str], None] | None = None,
) -> list[dict[str, str]]:
    rows = fetch_open_rows()
    emit(progress, f"DC OCP open solicitations: {len(rows)} public rows")

    records: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, row in enumerate(rows[:MAX_DETAIL_ROWS]):
        details = fetch_details(row.get("solicitationNumber"), progress=progress) if index < MAX_DETAIL_ROWS else {}
        record = normalize_row(row, details=details, keywords=keywords)
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


def fetch_open_rows() -> list[dict[str, Any]]:
    payload = {
        "FilterBy": [
            {"id": 0, "name": "Type", "value": 0},
            {"id": 1, "name": "Status", "value": "Open"},
        ],
        "OrderBy": [],
    }
    result = fetch_url(
        RESULTS_ENDPOINT,
        method="POST",
        json_data=payload,
        headers={
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Origin": BASE_URL.rstrip("/"),
            "Referer": SEARCH_PAGE_URL,
        },
        timeout=60,
        byte_limit=2_000_000,
        user_agent=USER_AGENT,
    )
    result.raise_for_status()
    data = json.loads(result.body_text())
    if data.get("errorMessage"):
        raise RuntimeError(f"DC OCP search failed: {data.get('errorMessage')}")
    rows = data.get("results")
    return [row for row in rows or [] if isinstance(row, dict)]


def fetch_details(solicitation_number: Any, *, progress: Callable[[str], None] | None = None) -> dict[str, Any]:
    raw_id = clean_text(solicitation_number, 240)
    if not raw_id:
        return {}
    url = DETAILS_ENDPOINT + "?" + urllib.parse.urlencode({"id": raw_id})
    result = fetch_url(
        url,
        headers={"Accept": "application/json, text/plain, */*", "Referer": SEARCH_PAGE_URL},
        timeout=40,
        byte_limit=250_000,
        user_agent=USER_AGENT,
    )
    if not result.ok:
        emit(progress, f"DC OCP detail failed for {display_id(raw_id)}: {result.error or result.status_code}")
        return {}
    try:
        data = json.loads(result.body_text())
    except json.JSONDecodeError:
        emit(progress, f"DC OCP detail JSON parse failed for {display_id(raw_id)}")
        return {}
    return data if isinstance(data, dict) else {}


def normalize_row(row: dict[str, Any], *, details: dict[str, Any], keywords: list[str]) -> dict[str, str]:
    raw_id = clean_text(row.get("solicitationNumber"), 240)
    source_record_id = display_id(raw_id)
    title = clean_text(row.get("title") or details.get("title") or source_record_id, 500)
    agency = "; ".join(clean_text(item, 180) for item in row.get("agencyNames") or details.get("agencyNames") or [] if clean_text(item))
    status = clean_text(details.get("status") or row.get("status") or "Open", 80).title()
    due_date = iso_date(row.get("closingDate") or details.get("closingDate"))
    posted_date = iso_date(details.get("openDate"))
    specialist = specialist_text(row.get("specialist") or details.get("specialist"))
    detail_url = ui_detail_url(raw_id)
    documents = details.get("documents") if isinstance(details.get("documents"), list) else []
    document_url = first_document_url(documents) or detail_url
    search_text = expand_related_terms(
        " ".join(
            [
                source_record_id,
                title,
                agency,
                clean_text(row.get("marketType") or details.get("marketType"), 160),
                clean_text(details.get("synopsis"), 3000),
                " ".join(clean_text(item, 200) for item in details.get("commodities") or []),
                specialist,
            ]
        )
    )
    matched = keyword_hits(search_text, keywords)
    raw = {
        "source_key": "dc_ocp_contracts",
        "source_note": SOURCE_NOTE,
        "search_endpoint": RESULTS_ENDPOINT,
        "detail_endpoint": DETAILS_ENDPOINT,
        "row": row,
        "details": details,
    }

    return {
        "id": stable_id("DC", source_record_id, prefix="dc-ocp-solicitation"),
        "state": "DC",
        "source": SOURCE_NAME,
        "source_record_id": source_record_id,
        "title": title,
        "agency": agency,
        "document_type": document_type(source_record_id, title, details.get("solicitationType")),
        "posted_date": posted_date,
        "due_date": due_date,
        "status": status,
        "amount": "",
        "document_url": document_url,
        "source_url": detail_url or SEARCH_PAGE_URL,
        "matched_keywords": ";".join(matched),
        "relevance_score": str(relevance_score(matched, status, search_text, due_date)),
        "raw_json": compact_raw_json(raw, limit=10000),
        "last_checked_at": now_iso(),
    }


def display_id(value: Any) -> str:
    text = clean_text(value, 240)
    text = re.sub(r"[\x00-\x1f]+", "-", text)
    return clean_text(text.strip("-"), 240)


def ui_detail_url(raw_id: str) -> str:
    if not raw_id:
        return SEARCH_PAGE_URL
    return urllib.parse.urljoin(BASE_URL, "solicitations/details?id=" + urllib.parse.quote(raw_id, safe=""))


def first_document_url(documents: list[Any]) -> str:
    for item in documents:
        if isinstance(item, dict):
            for key in ("url", "href", "link", "documentUrl"):
                value = clean_text(item.get(key), 500)
                if value.startswith("http"):
                    return value
    return ""


def specialist_text(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    return " ".join(clean_text(value.get(key), 180) for key in ("name", "email", "phone") if clean_text(value.get(key)))


def document_type(source_record_id: str, title: str, solicitation_type: Any) -> str:
    text = " ".join([source_record_id, title, clean_text(solicitation_type, 120)]).upper()
    if "REQUEST FOR QUALIFICATION" in text:
        return "DC Request for Qualifications"
    if "REQUEST FOR INFORMATION" in text or re.search(r"\bRFI\b", text):
        return "DC Request for Information"
    if "REQUEST FOR PROPOSAL" in text or re.search(r"\bRFP\b", text):
        return "DC Request for Proposal"
    if "INVITATION FOR BID" in text or re.search(r"\bIFB\b", text) or re.search(r"-B-", text):
        return "DC Invitation for Bid"
    if re.search(r"-Q-", text) or re.search(r"\bRFQ\b", text):
        return "DC Request for Quote"
    return "DC Solicitation"


def expand_related_terms(text: str) -> str:
    expanded = text
    if any(term_matches(text, term) for term in ["Health Care Finance", "DHCF", "Health Benefit Exchange", "DCHBX"]):
        expanded += " Medicaid health care provider managed care claims eligibility enrollment"
    if any(term_matches(text, term) for term in ["Behavioral Health", "DBH", "Health", "DOH", "Disability Services"]):
        expanded += " health care behavioral health provider"
    return expanded


def useful_keyword_match(matches: list[str], text: str) -> bool:
    generic_terms = {"claims", "eligibility", "enrollment", "provider", "provider data", "workforce", "cms", "interoperability"}
    matched_terms = {match.lower() for match in matches if match}
    if not matched_terms or not matched_terms <= generic_terms:
        return True
    context_terms = [
        "health care finance",
        "health benefit exchange",
        "department of health",
        "behavioral health",
        "medicaid",
        "medicare",
        "health care",
        "healthcare",
        "medical",
        "hospital",
        "managed care",
        "provider",
        "chip",
        "mmis",
        "dhcf",
        "doh",
        "dbh",
    ]
    return any(term_matches(text, term) for term in context_terms)


def relevance_score(matches: list[str], status: str, text: str, due_date: str) -> int:
    score = min(50, len([match for match in matches if match]) * 10)
    if any(term_matches(text, term) for term in ["Medicaid", "MMIS", "Health Care Finance", "DHCF", "Health Benefit Exchange"]):
        score += 25
    if any(term_matches(text, term) for term in ["eligibility", "claims", "enrollment", "managed care", "interoperability", "FHIR", "prior authorization", "provider data"]):
        score += 15
    if term_matches(text, "rural health"):
        score += 25
    if any(term_matches(text, term) for term in ["RFP", "RFI", "RFQ", "services", "system", "program", "software", "data"]):
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
