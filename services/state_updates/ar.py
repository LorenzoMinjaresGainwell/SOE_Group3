from __future__ import annotations

import re
from typing import Any, Callable

from services.state_updates import emit, state_update_record
from services.state_updates.common import clean_text, fetch_json_data, first_date_text, iso_date_text, matches_keywords_or_context, record_type_for, source_id_from_url, unique_records

AGENCY = "Arkansas Department of Human Services / Division of Medical Services"
DMS_EVENTS_API = "https://humanservices.arkansas.gov/wp-json/wp/v2/events?categories=22&per_page=100"
DMS_CATEGORY_PAGE = "https://humanservices.arkansas.gov/category/dms/"
CONTEXT_TERMS = [
    "arkansas medicaid",
    "division of medical services",
    "dhs public hearing",
    "public hearing",
    "proposed rule",
    "arhome",
    "hcbs",
    "waiver",
    "provider manual",
    "provider meeting",
    "payment rates",
    "dur board",
    "medicaid utilization management",
    "personal care",
    "independent assessment",
    "behavioral health",
    "substance use disorder",
    "primary care provider",
]


def fetch_updates(
    *,
    keywords: list[str],
    max_records: int,
    progress: Callable[[str], None] | None = None,
) -> list[dict[str, str]]:
    records = fetch_dms_events(keywords=keywords, progress=progress)
    output = unique_records(records)
    emit(progress, f"AR: normalized {len(output)} records from official DHS/DMS update sources")
    return output[:max_records]


def fetch_dms_events(*, keywords: list[str], progress: Callable[[str], None] | None) -> list[dict[str, str]]:
    data = fetch_json_data(DMS_EVENTS_API, byte_limit=2_000_000)
    rows = data if isinstance(data, list) else []
    records: list[dict[str, str]] = []
    scanned = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        scanned += 1
        title = rendered_text(row.get("title"))
        excerpt = rendered_text(row.get("excerpt"))
        content = rendered_text(row.get("content"))
        url = clean_text(row.get("link"))
        posted_date = iso_date_text(str(row.get("date", ""))[:10])
        text = " ".join([title, excerpt, content])
        if not title or not posted_date:
            continue
        if not matches_keywords_or_context(text, keywords, CONTEXT_TERMS):
            continue
        event_date = iso_date_text(first_date_text(" ".join([title, excerpt, content])))
        record_type = ar_record_type(text)
        records.append(
            state_update_record(
                state="AR",
                source="ar_dhs_dms_events",
                source_record_id=str(row.get("id") or source_id_from_url(url) or title[:120]),
                record_type=record_type,
                title=title,
                agency=AGENCY,
                summary=excerpt or "Arkansas DHS/DMS Medicaid policy, provider, or public-hearing event notice.",
                posted_date=posted_date,
                due_date=event_date if record_type == "public_comment_notice" else "",
                action_required_by=event_date if record_type == "public_comment_notice" else "",
                comment_required=record_type == "public_comment_notice",
                document_url=url,
                source_url=DMS_CATEGORY_PAGE,
                keywords=keywords,
                raw={
                    "source_api": DMS_EVENTS_API,
                    "event_id": row.get("id"),
                    "modified": row.get("modified"),
                    "categories": row.get("categories"),
                    "event_date_detected": event_date,
                },
            )
        )
    emit(progress, f"AR DHS/DMS events: scanned {scanned}, kept {len(records)}")
    return records


def rendered_text(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("rendered", "")
    return clean_text(re.sub(r"<[^>]+>", " ", str(value or "")))


def ar_record_type(text: str) -> str:
    lower = text.lower()
    if "public hearing" in lower or "public comment" in lower or "proposed rule" in lower:
        return "public_comment_notice"
    if "state plan amendment" in lower or re.search(r"\bspa\b", lower):
        return "spa_notice"
    if "waiver" in lower or "hcbs" in lower or "1915" in lower:
        return "waiver_notice"
    if "provider" in lower or "manual" in lower or "dur board" in lower or "payment rate" in lower:
        return "provider_bulletin"
    return record_type_for(text, "medicaid_notice")
