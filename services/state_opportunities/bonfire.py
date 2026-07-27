from __future__ import annotations

import datetime as dt
import json
import re
import urllib.parse
from dataclasses import dataclass
from typing import Any, Callable

from services.state_http import fetch_url
from services.state_normalization import clean_id, clean_text, compact_raw_json, iso_date, keyword_hits, parse_date, stable_id, term_matches

USER_AGENT = "soe-group3-bonfire-opportunities/0.1"
SOURCE_NOTE = "Official Bonfire public portal JSON endpoint /PublicPortal/getOpenPublicOpportunitiesSectionData; no login or browser automation used."
MAX_SCAN_ROWS = 1000


@dataclass(frozen=True)
class BonfireConfig:
    state: str
    source_name: str
    portal_base_url: str
    source_key: str
    official_source_url: str = ""
    agency_fallback: str = ""

    @property
    def portal_url(self) -> str:
        return urllib.parse.urljoin(self.portal_base_url.rstrip("/") + "/", "portal/?tab=openOpportunities")

    @property
    def open_opportunities_url(self) -> str:
        return urllib.parse.urljoin(self.portal_base_url.rstrip("/") + "/", "PublicPortal/getOpenPublicOpportunitiesSectionData")


def fetch_bonfire_opportunities(
    *,
    config: BonfireConfig,
    keywords: list[str],
    days_back: int,
    max_records: int,
    progress: Callable[[str], None] | None = None,
) -> list[dict[str, str]]:
    payload = fetch_open_payload(config)
    projects = valid_project_rows(payload.get("projects"))[:MAX_SCAN_ROWS]
    departments = valid_departments(payload.get("departments"))
    emit(progress, f"{config.state} Bonfire open opportunities: {len(projects)} public rows")

    records: list[dict[str, str]] = []
    seen: set[str] = set()
    for project in projects:
        record = normalize_project(project, departments=departments, config=config, keywords=keywords)
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


def fetch_open_payload(config: BonfireConfig) -> dict[str, Any]:
    result = fetch_url(
        config.open_opportunities_url,
        headers={
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Referer": config.portal_url,
            "X-Requested-With": "XMLHttpRequest",
        },
        timeout=60,
        byte_limit=3_000_000,
        user_agent=USER_AGENT,
    )
    result.raise_for_status()
    data = json.loads(result.body_text())
    if not isinstance(data, dict) or not data.get("success"):
        raise RuntimeError(f"Bonfire open opportunities failed for {config.portal_base_url}: {clean_text(data.get('message') if isinstance(data, dict) else data)}")
    payload = data.get("payload")
    if not isinstance(payload, dict):
        raise RuntimeError("Bonfire response missing payload object")
    return payload


def valid_project_rows(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        return [row for row in value.values() if isinstance(row, dict)]
    if isinstance(value, list):
        return [row for row in value if isinstance(row, dict)]
    return []


def valid_departments(value: Any) -> dict[str, dict[str, Any]]:
    if isinstance(value, dict):
        return {clean_id(key): row for key, row in value.items() if isinstance(row, dict)}
    return {}


def normalize_project(
    project: dict[str, Any],
    *,
    departments: dict[str, dict[str, Any]],
    config: BonfireConfig,
    keywords: list[str],
) -> dict[str, str]:
    project_id = clean_id(project.get("ProjectID"))
    private_id = clean_id(project.get("PrivateProjectID"))
    reference_id = clean_text(project.get("ReferenceID") or project_id, 180)
    title = clean_text(project.get("ProjectName") or reference_id, 500)
    department_id = clean_id(project.get("DepartmentID"))
    agency = department_name(departments.get(department_id)) or config.agency_fallback
    due_date = iso_date(project.get("DateClose"))
    status = status_from_project(project, due_date)
    document_url = project_url(project, config)
    search_text = expand_related_terms(" ".join([reference_id, title, agency, status]))
    matched = keyword_hits(search_text, keywords)
    raw = {
        "source_key": config.source_key,
        "source_note": SOURCE_NOTE,
        "official_source_url": config.official_source_url,
        "portal_url": config.portal_url,
        "project": project,
        "department": departments.get(department_id) or {},
    }

    return {
        "id": stable_id(config.state, project_id or reference_id, prefix=f"{config.state.lower()}-bonfire-opportunity"),
        "state": config.state,
        "source": config.source_name,
        "source_record_id": reference_id or project_id,
        "title": title,
        "agency": agency,
        "document_type": document_type(reference_id, title),
        "posted_date": "",
        "due_date": due_date,
        "status": status,
        "amount": "",
        "document_url": document_url,
        "source_url": config.portal_url,
        "matched_keywords": ";".join(matched),
        "relevance_score": str(relevance_score(matched, status, search_text, due_date)),
        "raw_json": compact_raw_json(raw, limit=10000),
        "last_checked_at": now_iso(),
    }


def department_name(row: dict[str, Any] | None) -> str:
    if not row:
        return ""
    return clean_text(row.get("DepartmentName"), 180)


def project_url(project: dict[str, Any], config: BonfireConfig) -> str:
    visibility_id = clean_id(project.get("ProjectVisibilityID"))
    if visibility_id == "2" and clean_id(project.get("PrivateProjectID")):
        path = "opportunities/private/" + urllib.parse.quote(clean_id(project.get("PrivateProjectID")))
    else:
        path = "opportunities/" + urllib.parse.quote(clean_id(project.get("ProjectID")))
    return urllib.parse.urljoin(config.portal_base_url.rstrip("/") + "/", path)


def status_from_project(project: dict[str, Any], due_date: str) -> str:
    status_id = clean_id(project.get("ProjectStatusID"))
    substatus_id = clean_id(project.get("ProjectSubStatusID"))
    due = parse_date(due_date)
    if due and due < dt.date.today():
        return "Closed"
    if status_id in {"2", "3"} or substatus_id in {"1", "2"}:
        return "Open"
    return "Open"


def document_type(reference_id: str, title: str) -> str:
    text = " ".join([reference_id, title]).upper()
    if code_matches(text, "RFI") or "REQUEST FOR INFORMATION" in text:
        return "Bonfire Request for Information"
    if code_matches(text, "RFP") or "REQUEST FOR PROPOS" in text:
        return "Bonfire Request for Proposal"
    if code_matches(text, "RFQ") or "REQUEST FOR QUOTE" in text:
        return "Bonfire Request for Quote"
    if code_matches(text, "IFB") or "INVITATION FOR BID" in text:
        return "Bonfire Invitation for Bid"
    if "SOLE SOURCE" in text:
        return "Bonfire Sole Source Notice"
    return "Bonfire Sourcing Event"


def expand_related_terms(text: str) -> str:
    expanded = text
    if any(term_matches(text, term) for term in ["Department of Health", "Health", "Healthcare", "Medical", "Hospital"]):
        expanded += " Medicaid Medicare managed care eligibility claims provider health care"
    if any(term_matches(text, term) for term in ["Human Services", "Workforce Services", "Children and Families"]):
        expanded += " Medicaid eligibility enrollment human services"
    if any(term_matches(text, term) for term in ["Behavioral Health", "Mental Health"]):
        expanded += " behavioral health managed care"
    if term_matches(text, "RHTP"):
        expanded += " rural health rural health transformation"
    return expanded


def useful_keyword_match(matches: list[str], text: str) -> bool:
    ambiguous_terms = {"claims", "eligibility", "enrollment", "cms", "workforce"}
    matched_terms = {match.lower() for match in matches if match}
    if not matched_terms or not matched_terms <= ambiguous_terms:
        return True
    context_terms = [
        "department of health",
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
    if any(term_matches(text, term) for term in ["Medicaid", "MMIS", "Department of Health", "Human Services"]):
        score += 30
    if any(term_matches(text, term) for term in ["eligibility", "claims", "enrollment", "managed care", "provider data"]):
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
    if days_back <= 0:
        return True
    posted = parse_date(posted_date)
    return not posted or (dt.date.today() - posted).days <= days_back


def code_matches(text: str, code: str) -> bool:
    return re.search(rf"(?<![A-Z0-9]){re.escape(code)}(?![A-Z0-9])", text, re.IGNORECASE) is not None


def record_sort_key(row: dict[str, str]) -> tuple[int, str, str]:
    return (int_or_zero(row.get("relevance_score")), row.get("due_date", ""), row.get("title", ""))


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
