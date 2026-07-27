from __future__ import annotations

from typing import Callable

from services.state_updates import emit, state_update_record
from services.state_updates.common import (
    absolute_url,
    clean_text,
    data_rows,
    due_date_from_text,
    fetch_text,
    find_table,
    first_date_text,
    head_last_modified,
    iso_date_text,
    matches_keywords_or_context,
    meta_modified,
    parse_links,
    parse_tables,
    record_type_for,
    source_id_from_url,
    unique_records,
)

AGENCY = "New Jersey Division of Medical Assistance and Health Services"
PUBLIC_NOTICES_PAGE = "https://nj.gov/humanservices/notices/grants/public-notices/"
MEDICAID_COMMUNICATIONS_PAGE = (
    "https://nj.gov/humanservices/dmahs/providers-stakeholders/provider-resources/"
    "medicaid-communications/"
)
CONTEXT_TERMS = [
    "dmahs",
    "medicaid",
    "nj familycare",
    "medical assistance",
    "provider resources",
    "medicaid communications",
    "state plan amendment",
    "spa",
    "waiver",
    "managed care",
    "behavioral health",
    "hospital",
    "nursing facility",
    "pace",
]


def fetch_updates(
    *,
    keywords: list[str],
    max_records: int,
    progress: Callable[[str], None] | None = None,
) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    records.extend(fetch_public_notices(keywords=keywords, progress=progress))
    remaining = max(0, max_records - len(records)) or max_records
    records.extend(fetch_medicaid_communications(keywords=keywords, limit=min(remaining, 80), progress=progress))
    emit(progress, f"NJ: normalized {len(records)} records from official DHS/DMAHS update sources")
    return unique_records(records)[:max_records]


def fetch_public_notices(*, keywords: list[str], progress: Callable[[str], None] | None) -> list[dict[str, str]]:
    markup = fetch_text(PUBLIC_NOTICES_PAGE)
    table = find_table(parse_tables(markup), ["division/office", "notice"])
    records: list[dict[str, str]] = []
    scanned = 0
    for row in data_rows(table):
        if len(row) < 4:
            continue
        scanned += 1
        division, notice, posted, action = row[:4]
        row_context = " ".join(cell.text for cell in row)
        if "dmahs" not in division.text.lower() and not matches_keywords_or_context(row_context, keywords, CONTEXT_TERMS):
            continue
        posted_date = iso_date_text(first_date_text(posted.text))
        due_date = due_date_from_text(action.text, posted_date)
        effective_date = iso_date_text(action.text) if "effective" in action.text.lower() else ""
        if not (posted_date or due_date or effective_date):
            continue
        for link in notice.links:
            title = clean_text(link.text)
            if not title:
                continue
            search_text = " ".join([division.text, title, row_context, "New Jersey DMAHS Medicaid public notice"])
            if not matches_keywords_or_context(search_text, keywords, CONTEXT_TERMS):
                continue
            document_url = absolute_url(PUBLIC_NOTICES_PAGE, link.href)
            record_type = record_type_for(title, "public_comment_notice")
            records.append(
                state_update_record(
                    state="NJ",
                    source="nj_dhs_public_notices",
                    source_record_id=source_id_from_url(document_url),
                    record_type=record_type,
                    title=title,
                    agency=AGENCY if "dmahs" in division.text.lower() else "New Jersey Department of Human Services",
                    summary=f"Division/office: {clean_text(division.text)}. {clean_text(action.text)}",
                    posted_date=posted_date,
                    due_date=due_date,
                    effective_date=effective_date,
                    comment_required="comment" in action.text.lower(),
                    document_url=document_url,
                    source_url=PUBLIC_NOTICES_PAGE,
                    keywords=keywords,
                    raw={"division": division.text, "posted": posted.text, "action": action.text, "href": link.href},
                )
            )
    emit(progress, f"NJ DHS public notices: scanned {scanned}, kept {len(records)}")
    return records


def fetch_medicaid_communications(
    *,
    keywords: list[str],
    limit: int,
    progress: Callable[[str], None] | None,
) -> list[dict[str, str]]:
    markup = fetch_text(MEDICAID_COMMUNICATIONS_PAGE)
    page_modified = meta_modified(markup)
    candidates = []
    for link in parse_links(markup, MEDICAID_COMMUNICATIONS_PAGE):
        if "/humanservices/dmahs/documents/providers-stakeholders/resources/medicaid/" not in link.href:
            continue
        if not link.href.lower().endswith(".pdf"):
            continue
        title = clean_text(link.text)
        if not title:
            continue
        candidates.append((title, link.href))
    records: list[dict[str, str]] = []
    scanned = 0
    for title, document_url in candidates[:limit]:
        scanned += 1
        search_text = " ".join([title, document_url, "NJ DMAHS Medicaid Communications provider resources"])
        if not matches_keywords_or_context(search_text, keywords, CONTEXT_TERMS):
            continue
        modified = head_last_modified(document_url) or page_modified
        effective_date = iso_date_text(title) if "effective" in title.lower() else ""
        records.append(
            state_update_record(
                state="NJ",
                source="nj_dmahs_medicaid_communications",
                source_record_id=source_id_from_url(document_url),
                record_type=record_type_for(title, "guidance"),
                title=title,
                agency=AGENCY,
                summary="DMAHS Medicaid Communications provider-resource PDF.",
                posted_date=modified,
                updated_date=modified,
                effective_date=effective_date,
                document_url=document_url,
                source_url=MEDICAID_COMMUNICATIONS_PAGE,
                keywords=keywords,
                raw={"page_modified": page_modified, "href": document_url},
            )
        )
    emit(progress, f"NJ DMAHS Medicaid communications: scanned {scanned}, kept {len(records)}")
    return records
