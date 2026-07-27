from __future__ import annotations

import datetime as dt
import re
import ssl
import time
import urllib.error
import urllib.request
from typing import Callable

from services.state_updates import emit, state_update_record
from services.state_updates.common import absolute_url, clean_text, matches_keywords_or_context, parse_tables, record_type_for, source_id_from_url, unique_records

AGENCY = "Alabama Medicaid Agency"
ALERTS_URL = "https://medicaid.alabama.gov/alerts.aspx"
PROPOSED_SPAS_URL = "https://medicaid.alabama.gov/content/9.0_Resources/9.8_State_Plan/9.8.1_Proposed_SPAs.aspx"
USER_AGENT = "Mozilla/5.0 (compatible; soe-group3-al-state-updates/0.1)"
CONTEXT_TERMS = [
    "alabama medicaid",
    "provider alert",
    "alert",
    "state plan amendment",
    "proposed spa",
    "public notice",
    "waiver",
    "rural emergency hospital",
    "rural health clinic",
    "hospital reimbursement",
    "telehealth",
    "eligibility",
    "managed care",
    "provider portal",
    "claims",
]


def fetch_updates(
    *,
    keywords: list[str],
    max_records: int,
    progress: Callable[[str], None] | None = None,
) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    for fetcher in (fetch_alerts, fetch_proposed_spas):
        try:
            records.extend(fetcher(keywords=keywords, progress=progress))
        except Exception as exc:  # Keep one official AL source failure from suppressing the other.
            emit(progress, f"AL: {fetcher.__name__} failed: {exc}")
    output = unique_records(records)
    emit(progress, f"AL: normalized {len(output)} records from official Alabama Medicaid update sources")
    return output[:max_records]


def fetch_alerts(*, keywords: list[str], progress: Callable[[str], None] | None) -> list[dict[str, str]]:
    markup = fetch_alabama_text(ALERTS_URL)
    records: list[dict[str, str]] = []
    scanned = 0
    for table in parse_tables(markup):
        if not table or not is_alert_table(table[0]):
            continue
        for row in table[1:]:
            if len(row) < 2:
                continue
            posted_date = al_date_to_iso(row[0].text)
            title_cell = row[1]
            link = title_cell.links[0] if title_cell.links else None
            title = clean_text(link.text if link else title_cell.text)
            if not posted_date or not title:
                continue
            scanned += 1
            search_text = " ".join([title, "Alabama Medicaid provider alert policy update"])
            if not matches_keywords_or_context(search_text, keywords, CONTEXT_TERMS):
                continue
            document_url = absolute_url(ALERTS_URL, link.href) if link else ALERTS_URL
            records.append(
                state_update_record(
                    state="AL",
                    source="al_medicaid_alerts",
                    source_record_id=source_id_from_url(document_url) or f"alert:{posted_date}:{title[:80]}",
                    record_type=alert_record_type(title),
                    title=title,
                    agency=AGENCY,
                    summary="Official Alabama Medicaid ALERT/provider update.",
                    posted_date=posted_date,
                    document_url=document_url,
                    source_url=ALERTS_URL,
                    keywords=keywords,
                    raw={"source_page": ALERTS_URL, "row_date": row[0].text},
                )
            )
    emit(progress, f"AL Medicaid ALERTs: scanned {scanned}, kept {len(records)}")
    return records


def fetch_proposed_spas(*, keywords: list[str], progress: Callable[[str], None] | None) -> list[dict[str, str]]:
    markup = fetch_alabama_text(PROPOSED_SPAS_URL)
    records: list[dict[str, str]] = []
    scanned = 0
    for table in parse_tables(markup):
        if not table or not is_spa_table(table[0]):
            continue
        for row in table[1:]:
            if len(row) < 2:
                continue
            posted_date = al_date_to_iso(row[0].text)
            title = clean_text(row[1].text)
            link = first_link(row)
            if not posted_date or not title or not link:
                continue
            scanned += 1
            search_text = " ".join([title, "Alabama Medicaid proposed state plan amendment public notice SPA"])
            if not matches_keywords_or_context(search_text, keywords, CONTEXT_TERMS):
                continue
            document_url = absolute_url(PROPOSED_SPAS_URL, link.href)
            records.append(
                state_update_record(
                    state="AL",
                    source="al_medicaid_proposed_spas",
                    source_record_id=source_id_from_url(document_url) or f"spa:{posted_date}:{title[:80]}",
                    record_type="spa_notice",
                    title=f"Proposed SPA: {title}",
                    agency=AGENCY,
                    summary="Official Alabama Medicaid proposed State Plan Amendment public notice.",
                    posted_date=posted_date,
                    comment_required=True,
                    document_url=document_url,
                    source_url=PROPOSED_SPAS_URL,
                    keywords=keywords,
                    raw={"source_page": PROPOSED_SPAS_URL, "date_filed": row[0].text, "proposed_spa": title},
                )
            )
    emit(progress, f"AL proposed SPAs: scanned {scanned}, kept {len(records)}")
    return records


def is_alert_table(header: list[object]) -> bool:
    text = " | ".join(clean_text(getattr(cell, "text", "")).lower() for cell in header)
    return "date" in text and "title" in text


def is_spa_table(header: list[object]) -> bool:
    text = " | ".join(clean_text(getattr(cell, "text", "")).lower() for cell in header)
    return "date filed" in text and "proposed spa" in text


def first_link(row: list[object]) -> object | None:
    for cell in row:
        links = getattr(cell, "links", [])
        if links:
            return links[0]
    return None


def alert_record_type(title: str) -> str:
    value = record_type_for(title, "provider_bulletin")
    return "provider_bulletin" if value == "policy_update" else value


def al_date_to_iso(value: str) -> str:
    text = clean_text(value)
    text = re.sub(r"\s*/\s*", "/", text)
    text = text.replace("Sept", "Sep")
    for fmt in ("%b %d, %Y", "%B %d, %Y", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return dt.datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            pass
    return ""


def fetch_alabama_text(url: str, timeout: int = 45) -> str:
    # medicaid.alabama.gov currently serves an incomplete cert chain to Python; keep bypass scoped to this official host.
    context = ssl._create_unverified_context()
    headers = {"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml,*/*"}
    request = urllib.request.Request(url, headers=headers)
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, context=context, timeout=timeout) as response:
                return response.read().decode("utf-8", "replace")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = exc
            time.sleep(1 + attempt)
    raise RuntimeError(f"Alabama Medicaid request failed for {url}: {last_error}")
