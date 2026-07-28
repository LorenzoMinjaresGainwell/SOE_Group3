from __future__ import annotations

import re
from typing import Callable

from services.state_updates import emit, state_update_record
from services.state_updates.common import clean_text, fetch_text, is_procurement_update, iso_date_text, record_type_for, source_id_from_url, unique_records

NEWS_URL = "https://dhhr.wv.gov/News/Pages/default.aspx"
AGENCY = "West Virginia Departments of Health, Human Services, and Health Facilities"
ITEM_RE = re.compile(
    r'(?is)<div style="padding-bottom:15px;"[^>]*>\s*<b><a href="(?P<href>[^"]+)"[^>]*>(?P<title>.*?)</a></b>'
    r'\s*<br\s*/?>\s*<i>(?P<date>[^<]+)</i>\s*<br\s*/?>(?P<summary>.*?)(?:<a [^>]*title="Click here to read the full article"|</div>)'
)
CONTEXT = ("medicaid", "rural health", "rural healthcare", "rhtp", "health transformation", "behavioral health", "health workforce", "hospital")
POLICY_ACTION = ("medicaid", "transformation", "rhtp", "policy", "program", "waiver", "funding", "grant", "payment", "provider", "implementation", "application", "public notice", "comment")


def parse_health_news(markup: str) -> list[dict[str, str]]:
    rows = []
    for match in ITEM_RE.finditer(markup):
        title = clean_text(match.group("title"))
        summary = clean_text(match.group("summary")).removesuffix("...")
        url = match.group("href")
        lower = f"{title} {summary}".lower()
        if (not any(term in lower for term in CONTEXT) or not any(term in lower for term in POLICY_ACTION)
                or is_procurement_update(title, summary, url)):
            continue
        rows.append({"title": title, "summary": summary, "posted_date": iso_date_text(match.group("date")), "url": url})
    return rows


def fetch_updates(*, keywords: list[str], max_records: int, progress: Callable[[str], None] | None = None) -> list[dict[str, str]]:
    if max_records <= 0:
        return []
    rows = parse_health_news(fetch_text(NEWS_URL, timeout=30, byte_limit=1_000_000))
    records = [state_update_record(
        state="WV", source="wv_health_news", source_record_id=source_id_from_url(row["url"]),
        record_type=record_type_for(f"{row['title']} {row['summary']}"), title=row["title"], agency=AGENCY,
        summary=row["summary"], posted_date=row["posted_date"], document_url=row["url"], source_url=NEWS_URL,
        keywords=keywords, raw={"source_page": NEWS_URL, "policy_context_filter": True},
    ) for row in rows]
    output = unique_records(records)
    emit(progress, f"WV health news: kept {len(output)} policy-relevant items from {len(rows)} parsed rows")
    return output[:max_records]
