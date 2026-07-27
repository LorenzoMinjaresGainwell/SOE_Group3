from __future__ import annotations

import datetime as dt
import json
import re
import time
import urllib.parse
from typing import Any, Callable

from services.state_http import fetch_url
from services.state_normalization import clean_text, compact_raw_json, iso_date, keyword_hits, parse_date, stable_id, term_matches

BASE_URL = "https://mvendor.cgieva.com/"
ALL_OPPORTUNITIES_URL = urllib.parse.urljoin(BASE_URL, "Vendor/public/AllOpportunities.jsp")
SOLR_URL = urllib.parse.urljoin(BASE_URL, "Vendor/public/solrconnect.jsp")
USER_AGENT = "Mozilla/5.0 soe-group3-va-eva-opportunities/0.1"
SOURCE_NAME = "eVA Virginia Business Opportunities"
SOURCE_NOTE = (
    "Official eVA Virginia Business Opportunities public Solr JSON endpoint behind "
    "mvendor.cgieva.com/Vendor/public/AllOpportunities.jsp; queried for status:Open rows."
)
PAGE_SIZE = 500
MAX_SCAN_ROWS = 5000


def fetch_opportunities(
    *,
    keywords: list[str],
    days_back: int,
    max_records: int,
    progress: Callable[[str], None] | None = None,
) -> list[dict[str, str]]:
    rows = fetch_open_rows(max_records=max_records, progress=progress)
    emit(progress, f"VA eVA open opportunities: {len(rows)} public rows")

    records: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in rows:
        record = normalize_opportunity(row, keywords=keywords)
        if not record.get("source_record_id") or record["id"] in seen:
            continue
        if record["status"].lower() in {"canceled", "cancelled"}:
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


def fetch_open_rows(*, max_records: int, progress: Callable[[str], None] | None = None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    start = 0
    total: int | None = None
    max_to_scan = min(MAX_SCAN_ROWS, max(PAGE_SIZE * 2, max(1, max_records) * 50))

    while len(rows) < max_to_scan:
        page_size = min(PAGE_SIZE, max_to_scan - len(rows))
        payload = solr_query({"q": "status:Open", "rows": str(page_size), "start": str(start), "wt": "json"})
        response = payload.get("response") if isinstance(payload, dict) else {}
        if not isinstance(response, dict):
            raise RuntimeError("VA eVA Solr response missing response object")
        if total is None:
            total = int_or_zero(response.get("numFound"))
        batch = valid_rows(response.get("docs"))
        emit(progress, f"VA eVA Solr page start={start}: {len(batch)} rows")
        if not batch:
            break
        rows.extend(batch)
        start += len(batch)
        if total is not None and start >= total:
            break
        time.sleep(0.15)

    return rows[:max_to_scan]


def solr_query(params: dict[str, str]) -> dict[str, Any]:
    url = SOLR_URL + "?" + urllib.parse.urlencode(params)
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Referer": ALL_OPPORTUNITIES_URL,
    }
    last_error: Exception | None = None
    for attempt in range(3):
        result = fetch_url(url, headers=headers, timeout=60, user_agent=USER_AGENT)
        if result.ok and result.body:
            text = result.body_text().strip()
            try:
                data = json.loads(text)
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"VA eVA Solr returned non-JSON response: {text[:120]!r}") from exc
            if not isinstance(data, dict):
                raise RuntimeError("VA eVA Solr response root was not an object")
            return data
        last_error = RuntimeError(f"status={result.status_code} error={result.error or 'empty response'}")
        time.sleep(1 + attempt)
    raise RuntimeError(f"VA eVA Solr request failed: {last_error}")


def normalize_opportunity(row: dict[str, Any], *, keywords: list[str]) -> dict[str, str]:
    source_record_id = clean_text(row.get("id") or row.get("externalid") or row.get("internalid"), 180)
    title = clean_text(row.get("shortdesc") or source_record_id, 500)
    agency = clean_text(row.get("agencyname") or row.get("buyerdeptname") or row.get("agency"), 180)
    status = clean_text(row.get("status") or "Open", 80)
    posted_date = iso_date(row.get("pubdate") or row.get("lastupdatedate"))
    due_date = iso_date(row.get("closedate"))
    detail_url = detail_page_url(row)
    search_text = row_search_text(row)
    matched = keyword_hits(search_text, keywords)
    raw = {
        "source_key": "va_eva",
        "source_note": SOURCE_NOTE,
        "detail_url": detail_url,
        "source_payload": row,
    }

    return {
        "id": stable_id("VA", source_record_id, prefix="va-eva-opportunity"),
        "state": "VA",
        "source": SOURCE_NAME,
        "source_record_id": source_record_id,
        "title": title,
        "agency": agency,
        "document_type": document_type(row),
        "posted_date": posted_date,
        "due_date": due_date,
        "status": status,
        "amount": "",
        "document_url": detail_url,
        "source_url": ALL_OPPORTUNITIES_URL,
        "matched_keywords": ";".join(matched),
        "relevance_score": str(relevance_score(matched, status, search_text, due_date)),
        "raw_json": compact_raw_json(raw, limit=10000),
        "last_checked_at": now_iso(),
    }


def detail_page_url(row: dict[str, Any]) -> str:
    app = clean_text(row.get("app"), 20).upper()
    doc_cd = clean_text(row.get("doccd"), 80)
    dept_cd = clean_text(row.get("docdeptcd") or row.get("agency"), 120)
    internal_id = clean_text(row.get("internalid") or row.get("externalid"), 120)
    external_id = clean_text(row.get("externalid") or row.get("internalid"), 120)
    version = clean_text(row.get("version") or "0", 40)

    if app == "QQ":
        return urljoin_public("QQDetails.jsp", {"PageTitle": "QQ Details", "REQUEST_ID": external_id or internal_id})
    if app == "ADV":
        return urljoin_public(
            "ADVSODetails.jsp",
            {
                "PageTitle": "SO Details",
                "DOC_CD": doc_cd,
                "Details_Page": "ADVSODetails.jsp",
                "DEPT_CD": dept_cd,
                "BID_INTRNL_NO": internal_id,
                "BID_NO": external_id,
                "BID_VERS_NO": version,
            },
        )
    if app == "VBO":
        return urljoin_public(
            "VBODetails.jsp",
            {
                "PageTitle": "SO Details",
                "DOC_CD": doc_cd,
                "Details_Page": "VBOSODetails.jsp",
                "DEPT_CD": dept_cd,
                "BID_INTRNL_NO": internal_id,
                "BID_NO": external_id,
                "BID_VERS_NO": version,
            },
        )
    if app == "IV":
        return urljoin_public("IVDetails.jsp", {"PageTitle": "SO Details", "rfp_id_lot": internal_id, "rfp_id_round": version})
    return ALL_OPPORTUNITIES_URL


def urljoin_public(path: str, params: dict[str, str]) -> str:
    query = urllib.parse.urlencode({key: value for key, value in params.items() if value})
    return urllib.parse.urljoin(BASE_URL, f"Vendor/public/{path}") + (f"?{query}" if query else "")


def document_type(row: dict[str, Any]) -> str:
    doc_desc = clean_text(row.get("doccddesc"), 160)
    doc_cd = clean_text(row.get("doccd"), 40).upper()
    text = " ".join([doc_cd, doc_desc, clean_text(row.get("shortdesc"), 300)]).upper()
    if "RFI" in text or "REQUEST FOR INFORMATION" in text:
        return "eVA Request for Information"
    if "RFP" in text or "REQUEST FOR PROPOS" in text:
        return "eVA Request for Proposals"
    if "RFQ" in text or "REQUEST FOR QUAL" in text or "REQUEST FOR QUOTE" in text:
        return "eVA Request for Quotes"
    if "IFB" in text or "INVITATION FOR BID" in text:
        return "eVA Invitation for Bids"
    if "RFA" in text or "REQUEST FOR APPLICATION" in text:
        return "eVA Request for Applications"
    if "SOLE SOURCE" in text or doc_cd == "SS":
        return "eVA Sole Source Notice"
    return f"eVA {doc_desc}" if doc_desc else "eVA Opportunity"


def row_search_text(row: dict[str, Any]) -> str:
    parts = [
        row.get("id"),
        row.get("externalid"),
        row.get("internalid"),
        row.get("shortdesc"),
        row.get("longdesc"),
        row.get("agencyname"),
        row.get("buyerdeptname"),
        row.get("buyername"),
        row.get("doccd"),
        row.get("doccddesc"),
        row.get("category"),
        row.get("categoryshortdesc"),
        row.get("setasideshortdesc"),
        row.get("status"),
        row.get("workloc"),
    ]
    for field in ("commcode", "commdesc", "commlinedesc"):
        value = row.get(field)
        if isinstance(value, list):
            parts.extend(value)
        else:
            parts.append(value)
    return expand_related_terms(" ".join(clean_text(part, 2000) for part in parts if part))


def expand_related_terms(text: str) -> str:
    expanded = text
    if any(term_matches(text, term) for term in ["Department of Medical Assistance Services", "DMAS"]):
        expanded += " Medicaid managed care MMIS eligibility claims"
    if any(term_matches(text, term) for term in ["Department of Behavioral Health", "Behavioral Health Authority", "DBHDS"]):
        expanded += " behavioral health"
    if term_matches(text, "RHTP"):
        expanded += " rural health rural health transformation"
    return expanded


def useful_keyword_match(matches: list[str], text: str) -> bool:
    generic_terms = {"claims", "eligibility", "enrollment", "workforce"}
    matched_terms = {match.lower() for match in matches if match}
    if not matched_terms or not matched_terms <= generic_terms:
        return True
    context_terms = [
        "department of medical assistance services",
        "department of behavioral health",
        "health and human resources",
        "behavioral health authority",
        "medical assistance",
        "healthcare",
        "health care",
        "medicaid",
        "medicare",
        "hospital",
        "behavioral",
        "managed care",
        "provider",
        "chip",
        "mmis",
        "dmas",
        "dbhds",
    ]
    return any(term_matches(text, term) for term in context_terms)


def false_keyword_hit(record: dict[str, str]) -> bool:
    text = " ".join([record.get("title", ""), record.get("agency", ""), record.get("raw_json", "")])
    return term_matches(text, "commissary") and not term_matches(text, "MMIS")


def relevance_score(matches: list[str], status: str, text: str, due_date: str) -> int:
    score = min(50, len([match for match in matches if match]) * 10)
    if any(term_matches(text, term) for term in ["Medicaid", "MMIS", "Department of Medical Assistance Services", "DMAS"]):
        score += 30
    if any(term_matches(text, term) for term in ["behavioral health", "managed care", "eligibility", "claims", "provider data"]):
        score += 18
    if any(term_matches(text, term) for term in ["interoperability", "FHIR", "prior authorization", "telehealth", "quality measures"]):
        score += 15
    if any(term_matches(text, term) for term in ["rural health", "rural health transformation", "critical access hospital"]):
        score += 25
    if any(term_matches(text, term) for term in ["RFP", "RFI", "RFQ", "software", "data", "analytics", "contact center"]):
        score += 10
    if status.lower() == "open":
        score += 10
    due = parse_date(due_date)
    if due and due >= dt.date.today():
        score += 5
    return min(score, 100)


def is_open_or_recent(posted_date: str, due_date: str, days_back: int) -> bool:
    due = parse_date(due_date)
    if due and due >= dt.date.today():
        return True
    return within_days(posted_date, days_back)


def within_days(value: str, days_back: int) -> bool:
    if days_back <= 0:
        return True
    parsed = parse_date(value)
    if not parsed:
        return True
    return (dt.date.today() - parsed).days <= days_back


def valid_rows(value: Any) -> list[dict[str, Any]]:
    return [row for row in value or [] if isinstance(row, dict)]


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
