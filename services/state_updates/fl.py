from __future__ import annotations

import re
from typing import Callable

from services.state_updates import emit, state_update_record
from services.state_updates.common import absolute_url, clean_text, fetch_text, first_date_text, iso_date_text, matches_keywords_or_context, parse_tables, record_type_for, source_id_from_url, unique_records

AGENCY = "Florida Agency for Health Care Administration"
ALERTS_LAST_90_URL = "https://ahca.myflorida.com/medicaid/florida-medicaid-health-care-alerts/florida-medicaid-health-care-alerts-last-90-days.html"
ALERTS_ARCHIVE_URL = "https://ahca.myflorida.com/medicaid/florida-medicaid-health-care-alerts/florida-medicaid-health-care-alerts-archive.html"
CONTEXT_TERMS = [
    "florida medicaid",
    "provider alert",
    "health care alert",
    "statewide medicaid managed care",
    "managed care",
    "waiver",
    "provider enrollment",
    "provider payment",
    "fee schedule",
    "coverage policy",
    "public workshop",
    "public hearing",
    "rule 59g",
    "state plan amendment",
    "developmental disabilities",
    "behavior analysis",
]


def fetch_updates(
    *,
    keywords: list[str],
    max_records: int,
    progress: Callable[[str], None] | None = None,
) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    for page_url in (ALERTS_LAST_90_URL, ALERTS_ARCHIVE_URL):
        try:
            records.extend(fetch_alert_page(page_url, keywords=keywords, progress=progress))
        except Exception as exc:  # Keep one AHCA listing failure from hiding the other.
            emit(progress, f"FL: alert page failed {page_url}: {exc}")
    output = unique_records(records)
    emit(progress, f"FL: normalized {len(output)} records from official AHCA Medicaid alert sources")
    return output[:max_records]


def fetch_alert_page(page_url: str, *, keywords: list[str], progress: Callable[[str], None] | None) -> list[dict[str, str]]:
    markup = fetch_text(page_url, timeout=45, byte_limit=1_500_000)
    records: list[dict[str, str]] = []
    scanned = 0
    for table in parse_tables(markup):
        for row in table:
            if len(row) < 2:
                continue
            posted_date = iso_date_text(first_date_text(row[0].text) or row[0].text)
            title_cell = row[1]
            link = title_cell.links[0] if title_cell.links else None
            title = clean_text(link.text if link else title_cell.text)
            if not posted_date or not title or title.lower() == "subject of the alert":
                continue
            scanned += 1
            document_url = absolute_url(page_url, link.href) if link else page_url
            search_text = " ".join([title, "Florida Medicaid AHCA provider health care alert"])
            if not matches_keywords_or_context(search_text, keywords, CONTEXT_TERMS):
                continue
            records.append(
                state_update_record(
                    state="FL",
                    source="fl_ahca_medicaid_alerts",
                    source_record_id=source_id_from_url(document_url) or f"alert:{posted_date}:{title[:80]}",
                    record_type=fl_record_type(title),
                    title=title,
                    agency=AGENCY,
                    summary="Official AHCA Florida Medicaid Health Care Alert.",
                    posted_date=posted_date,
                    document_url=document_url,
                    source_url=page_url,
                    keywords=keywords,
                    raw={"source_page": page_url, "row_date": row[0].text, "link_href": link.href if link else ""},
                )
            )
    emit(progress, f"FL AHCA alerts {page_label(page_url)}: scanned {scanned}, kept {len(records)}")
    return records


def fl_record_type(title: str) -> str:
    lower = title.lower()
    if "public workshop" in lower or "public hearing" in lower or "provider comments" in lower or "rule 59g" in lower:
        return "public_comment_notice"
    if "state plan amendment" in lower or re.search(r"\bspa\b", lower):
        return "spa_notice"
    if "waiver" in lower or "1115" in lower or "1915" in lower:
        return "waiver_notice"
    if any(term in lower for term in ("provider", "fee schedule", "billing", "coverage", "managed care", "payment", "enrollment", "reimbursement")):
        return "provider_bulletin"
    value = record_type_for(title, "provider_bulletin")
    return "provider_bulletin" if value == "policy_update" else value


def page_label(url: str) -> str:
    return "archive" if "archive" in url else "last-90-days"
