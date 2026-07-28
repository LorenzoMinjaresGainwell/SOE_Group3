from __future__ import annotations

import re
from typing import Callable

from services.state_updates import sort_key, state_update_record
from services.state_updates._official_html import date_from_text
from services.state_updates.common import absolute_url, clean_text, fetch_text, is_procurement_update, record_type_for, source_id_from_url, unique_records

NEWS_URL = "https://dhs.vi.gov/news/"
AGENCY = "U.S. Virgin Islands Department of Human Services, Office of Medicaid"
ITEM_RE = re.compile(
    r"(?is)(?P<date>\d{1,2}/\d{1,2}/(?:20)?\d{2})(?P<body>.{0,2500}?)"
    r"<a\b[^>]*href=[\"'](?P<href>[^\"']+)[\"'][^>]*>(?P<link>.*?)</a>"
)
CONTEXT = ("medicaid", "provider enrollment", "provider policy", "state plan", "spa", "waiver", "public comment")


def source_rows(markup: str, source_url: str = NEWS_URL, terms: list[str] | None = None) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    for match in ITEM_RE.finditer(markup):
        body = clean_text(match.group("body"))
        link_text = clean_text(match.group("link"))
        url = absolute_url(source_url, match.group("href"))
        text = f"{body} {link_text} {url}"
        if not any(term in text.lower() for term in (terms or CONTEXT)) or is_procurement_update(text):
            continue
        if url in seen or not url.lower().startswith(("http://", "https://")):
            continue
        title = body or link_text
        if not title or title.lower() in {"read more", ">>read more>>"}:
            continue
        seen.add(url)
        rows.append({"title": title[:500], "url": url, "date": date_from_text(match.group("date"))})
    return rows


def fetch_updates(*, keywords: list[str], max_records: int, progress: Callable[[str], None] | None = None) -> list[dict[str, str]]:
    if max_records <= 0:
        return []
    try:
        rows = source_rows(fetch_text(NEWS_URL, timeout=20, byte_limit=1_500_000))
    except Exception as exc:
        emit(progress, f"VI Medicaid news unavailable: {exc}")
        return []
    records = []
    for row in rows:
        text = f"{row['title']} {row['url']}"
        record_type = record_type_for(text, "medicaid_notice")
        records.append(state_update_record(
            state="VI", source="vi_dhs_medicaid_news", source_record_id=source_id_from_url(row["url"]) or row["title"],
            record_type=record_type, title=row["title"], agency=AGENCY,
            summary="Official USVI DHS Medicaid policy or provider notice.", posted_date=row["date"],
            comment_required=("public comment" in text.lower()), document_url=row["url"], source_url=NEWS_URL,
            keywords=keywords, raw={"source_page": NEWS_URL, "procurement_excluded": True},
        ))
    output = sorted(unique_records(records), key=sort_key, reverse=True)
    emit(progress, f"VI: normalized {len(output)} dated Medicaid news records")
    return output[:max_records]


def emit(progress: Callable[[str], None] | None, message: str) -> None:
    if progress:
        progress(message)
