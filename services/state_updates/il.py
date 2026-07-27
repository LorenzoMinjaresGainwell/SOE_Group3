from __future__ import annotations

import datetime as dt
from typing import Any, Callable

from services.state_updates import emit, state_update_record
from services.state_updates.common import (
    absolute_url,
    clean_text,
    data_rows,
    due_date_from_text,
    fetch_json_data,
    fetch_text,
    find_table,
    first_date_text,
    iso_date_text,
    matches_keywords_or_context,
    parse_links,
    parse_tables,
    public_hfs_url,
    record_type_for,
    source_id_from_url,
    unique_records,
)

AGENCY = "Illinois Department of Healthcare and Family Services"
PROVIDER_NOTICES_PAGE = "https://hfs.illinois.gov/medicalproviders/notices.html"
PROVIDER_NOTICES_JSON = (
    "https://hfs.illinois.gov/content/soi/hfs/en/medicalproviders/notices/"
    "jcr:content/responsivegrid/container/container_293684588/"
    "datatablecontentfrag.datatablecontentfragment.json"
)
PUBLIC_NOTICES_PAGE = "https://hfs.illinois.gov/info/legal/publicnotices.html"
RECENT_NOTICE_YEAR_FLOOR = dt.date.today().year - 2
CONTEXT_TERMS = [
    "medicaid",
    "medical assistance",
    "provider notice",
    "provider bulletin",
    "public notice",
    "waiver",
    "state plan amendment",
    "managed care",
    "healthchoice",
    "hospital",
    "behavioral health",
]


def fetch_updates(
    *,
    keywords: list[str],
    max_records: int,
    progress: Callable[[str], None] | None = None,
) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    records.extend(fetch_provider_notices(keywords=keywords, progress=progress))
    records.extend(fetch_public_notices(keywords=keywords, progress=progress))
    emit(progress, f"IL: normalized {len(records)} records from official HFS update sources")
    return unique_records(records)[:max_records]


def fetch_provider_notices(*, keywords: list[str], progress: Callable[[str], None] | None) -> list[dict[str, str]]:
    data = fetch_json_data(PROVIDER_NOTICES_JSON)
    rows = data.get("data", []) if isinstance(data, dict) else []
    records: list[dict[str, str]] = []
    scanned = 0
    for row in rows:
        scanned += 1
        if not isinstance(row, list) or len(row) < 5:
            continue
        year, notice_date, link_info, subject, category = row[:5]
        if int_or_zero(year) < RECENT_NOTICE_YEAR_FLOOR:
            continue
        link_id, href = notice_link(link_info)
        title = clean_text(subject) or clean_text(link_id)
        category_text = clean_text(str(category).replace("+", " "))
        search_text = " ".join([title, category_text, str(year), "provider notice medicaid medical assistance"])
        if not matches_keywords_or_context(search_text, keywords, CONTEXT_TERMS):
            continue
        source_url = public_hfs_url(absolute_url("https://hfs.illinois.gov", href)) if href else PROVIDER_NOTICES_PAGE
        records.append(
            state_update_record(
                state="IL",
                source="il_hfs_provider_notices",
                source_record_id=clean_text(link_id) or source_id_from_url(source_url),
                record_type="provider_bulletin",
                title=title,
                agency=AGENCY,
                summary=f"Provider category: {category_text}" if category_text else "HFS provider notice.",
                posted_date=notice_date,
                document_url=source_url,
                source_url=PROVIDER_NOTICES_PAGE,
                keywords=keywords,
                raw={"row": row, "source_url": PROVIDER_NOTICES_JSON},
            )
        )
    emit(progress, f"IL HFS provider notices: scanned {scanned}, kept {len(records)}")
    return records


def fetch_public_notices(*, keywords: list[str], progress: Callable[[str], None] | None) -> list[dict[str, str]]:
    markup = fetch_text(PUBLIC_NOTICES_PAGE)
    records: list[dict[str, str]] = []
    scanned = 0
    table = find_table(parse_tables(markup), ["notice"])
    for row in data_rows(table):
        if len(row) < 1:
            continue
        scanned += 1
        add_public_notice_links(records, row[0].links, row[0].text, keywords)

    if not records:
        for link in parse_links(markup, PUBLIC_NOTICES_PAGE):
            title = clean_text(link.text)
            if "public notice" not in title.lower() and "/publicnotices/" not in link.href.lower():
                continue
            scanned += 1
            add_public_notice_links(records, [link], title, keywords)

    emit(progress, f"IL HFS public notices: scanned {scanned}, kept {len(records)}")
    return records


def add_public_notice_links(records: list[dict[str, str]], links: list[Any], context: str, keywords: list[str]) -> None:
    for link in links:
        title = clean_text(link.text)
        if not title or title.lower().endswith("public notices"):
            continue
        search_text = " ".join([title, context, "HFS Medicaid public notice state plan waiver"])
        if not matches_keywords_or_context(search_text, keywords, CONTEXT_TERMS):
            continue
        document_url = absolute_url(PUBLIC_NOTICES_PAGE, link.href)
        posted_date = iso_date_text(first_date_text(context) or first_date_text(title))
        if not posted_date:
            continue
        record_type = record_type_for(title, "public_comment_notice")
        records.append(
            state_update_record(
                state="IL",
                source="il_hfs_public_notices",
                source_record_id=source_id_from_url(document_url),
                record_type=record_type,
                title=title,
                agency=AGENCY,
                summary="HFS Medicaid/public notice from the Legal Center public notices page.",
                posted_date=posted_date,
                comment_required="comment" in context.lower() or "public notice" in title.lower(),
                due_date=due_date_from_text(context, posted_date),
                document_url=document_url,
                source_url=PUBLIC_NOTICES_PAGE,
                keywords=keywords,
                raw={"context": context, "href": link.href},
            )
        )


def notice_link(link_info: Any) -> tuple[str, str]:
    if isinstance(link_info, list) and len(link_info) >= 2:
        return clean_text(link_info[0]), str(link_info[1] or "")
    if isinstance(link_info, dict):
        return clean_text(link_info.get("text") or link_info.get("id") or ""), str(link_info.get("href") or link_info.get("url") or "")
    return clean_text(link_info), ""


def int_or_zero(value: Any) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return 0
