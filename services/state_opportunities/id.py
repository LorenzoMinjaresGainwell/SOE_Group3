from __future__ import annotations

import datetime as dt
import json
import re
from typing import Any, Callable

from services.state_http import fetch_url
from services.state_normalization import clean_text, compact_raw_json, iso_date, keyword_hits, parse_date, stable_id, term_matches

PAGE_URL = "https://purchasing.idaho.gov/open-and-future-solicitations/"
API_URL = "https://purchasing.idaho.gov/wp-json/wm4/v1/procurement-report"
SUPPLIER_URL = "https://sms-idaho-prd.tam.inforgov.com/fsm/SupplyManagementSupplier/page/XiSupplyManagementSupplierPage?csk.SupplierGroup=LUMA"
USER_AGENT = "soe-group3-id-purchasing-opportunities/0.1"
SOURCE_NAME = "Idaho Division of Purchasing Open and Future Solicitations"
SOURCE_NOTE = (
    "Official Division of Purchasing WordPress REST endpoint /wp-json/wm4/v1/procurement-report; "
    "the state page populates the table from this JSON route. The linked LUMA/Infor supplier page returned 200 with 0 bytes from CLI."
)


def fetch_opportunities(
    *,
    keywords: list[str],
    days_back: int,
    max_records: int,
    progress: Callable[[str], None] | None = None,
) -> list[dict[str, str]]:
    payload = fetch_report()
    rows = valid_rows(payload.get("data"))
    emit(progress, f"ID Division of Purchasing procurement report: {len(rows)} public rows")

    records: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in rows:
        record = normalize_row(row, keywords=keywords, generated_at=clean_text(payload.get("generated_at")))
        if not record.get("source_record_id") or record["id"] in seen:
            continue
        if record["status"].lower() in {"completed", "dropped", "cancelled", "canceled"}:
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


def fetch_report() -> dict[str, Any]:
    result = fetch_url(
        API_URL,
        headers={"Accept": "application/json", "Referer": PAGE_URL},
        timeout=60,
        byte_limit=2_000_000,
        user_agent=USER_AGENT,
    )
    result.raise_for_status()
    data = json.loads(result.body_text())
    if not isinstance(data, dict):
        raise RuntimeError("ID procurement report response root was not an object")
    return data


def valid_rows(value: Any) -> list[dict[str, Any]]:
    return [row for row in value or [] if isinstance(row, dict)]


def normalize_row(row: dict[str, Any], *, keywords: list[str], generated_at: str) -> dict[str, str]:
    title = clean_text(row.get("name"), 500)
    source_record_id = solicitation_id(title) or title
    agency = clean_text(row.get("agency"), 180)
    description = clean_text(row.get("project_description"), 2000)
    posted_date = labeled_date(description, ["Anticipated Post Date", "Post Date"]) or iso_date(row.get("project_start_date") or row.get("project_created_date"))
    close_date = labeled_date(description, ["Anticipated Close Date", "Close Date", "Closed Date"])
    award_date = labeled_date(description, ["Anticipated Award Date", "Award Date"])
    due_date = iso_date(row.get("project_due_date")) or close_date
    status = clean_text(row.get("status") or status_from_due_date(due_date), 80)
    search_text = expand_related_terms(" ".join([source_record_id, title, agency, description, status]))
    matched = keyword_hits(search_text, keywords)
    raw = {
        "source_key": "id_purchasing",
        "source_note": SOURCE_NOTE,
        "page_url": PAGE_URL,
        "api_url": API_URL,
        "supplier_url": SUPPLIER_URL,
        "generated_at": generated_at,
        "post_date": posted_date,
        "close_date": close_date,
        "award_date": award_date,
        "row": row,
    }

    return {
        "id": stable_id("ID", source_record_id or title, prefix="id-dop-solicitation"),
        "state": "ID",
        "source": SOURCE_NAME,
        "source_record_id": source_record_id,
        "title": title or source_record_id,
        "agency": agency,
        "document_type": document_type(source_record_id, title),
        "posted_date": posted_date,
        "due_date": due_date,
        "status": status,
        "amount": "",
        "document_url": PAGE_URL,
        "source_url": PAGE_URL,
        "matched_keywords": ";".join(matched),
        "relevance_score": str(relevance_score(matched, status, search_text, due_date)),
        "raw_json": compact_raw_json(raw, limit=10000),
        "last_checked_at": now_iso(),
    }


def solicitation_id(title: str) -> str:
    match = re.match(r"\s*([A-Z]{2,5}\s*\d{2,6}[A-Z0-9-]*|PADD\s*-?\s*[^-]+?)\s+-\s+", title, re.IGNORECASE)
    if match:
        return clean_text(match.group(1), 180)
    return clean_text(title.split(" - ", 1)[0], 180) if " - " in title else ""


def labeled_date(description: str, labels: list[str]) -> str:
    text = clean_text(description.replace("\xa0", " "), 3000)
    for label in labels:
        pattern = rf"\b{re.escape(label)}\s*:\s*(\d{{1,2}}/\d{{1,2}}/\d{{2,4}})"
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return iso_date(match.group(1))
    return ""


def document_type(source_record_id: str, title: str) -> str:
    text = " ".join([source_record_id, title]).upper()
    if code_matches(text, "RFI") or "REQUEST FOR INFORMATION" in text:
        return "Idaho DOP Request for Information"
    if code_matches(text, "RFP") or "REQUEST FOR PROPOS" in text:
        return "Idaho DOP Request for Proposal"
    if code_matches(text, "RFQ") or "REQUEST FOR QUOTE" in text:
        return "Idaho DOP Request for Quote"
    if code_matches(text, "ITB") or "INVITATION TO BID" in text:
        return "Idaho DOP Invitation to Bid"
    if code_matches(text, "PADD"):
        return "Idaho DOP Participating Addendum"
    return "Idaho DOP Solicitation"


def expand_related_terms(text: str) -> str:
    expanded = text
    if any(term_matches(text, term) for term in ["DHW", "Health and Welfare", "Medicaid", "MMIS"]):
        expanded += " Medicaid MMIS managed care eligibility claims human services provider health care"
    if any(term_matches(text, term) for term in ["Medical", "Medicare", "Visit Verification", "Actuarial", "Billing"]):
        expanded += " health care medical Medicare provider data"
    if any(term_matches(text, term) for term in ["Rural", "Senior Community", "Aging"]):
        expanded += " rural health workforce"
    return expanded


def useful_keyword_match(matches: list[str], text: str) -> bool:
    generic_terms = {"claims", "eligibility", "enrollment", "provider", "provider data", "workforce", "cms", "interoperability"}
    matched_terms = {match.lower() for match in matches if match}
    if not matched_terms or not matched_terms <= generic_terms:
        return True
    context_terms = [
        "dhw",
        "department of health and welfare",
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
    return "mmis" in {item.lower() for item in record.get("matched_keywords", "").split(";") if item} and term_matches(text, "commissary") and not term_matches(text, "MMIS")


def relevance_score(matches: list[str], status: str, text: str, due_date: str) -> int:
    score = min(50, len([match for match in matches if match]) * 10)
    if any(term_matches(text, term) for term in ["Medicaid", "MMIS", "DHW", "Health and Welfare"]):
        score += 30
    if any(term_matches(text, term) for term in ["managed care", "eligibility", "claims", "provider data", "visit verification"]):
        score += 18
    if any(term_matches(text, term) for term in ["interoperability", "FHIR", "prior authorization", "billing", "actuarial"]):
        score += 15
    if any(term_matches(text, term) for term in ["rural health", "senior community", "aging"]):
        score += 20
    if any(term_matches(text, term) for term in ["RFP", "RFI", "RFQ", "ITB", "software", "system", "services", "cloud", "data"]):
        score += 10
    if status.lower() in {"on track", "at risk", "off track", "open", "future"}:
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


def status_from_due_date(due_date: str) -> str:
    due = parse_date(due_date)
    if due and due < dt.date.today():
        return "Closed"
    return "Open/Future"


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
