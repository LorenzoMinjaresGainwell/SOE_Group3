from __future__ import annotations

import re
from typing import Callable

from services.state_updates import emit, state_update_record
from services.state_updates.common import absolute_url, clean_text, fetch_text, is_procurement_update, iso_date_text, record_type_for, source_id_from_url, unique_records

AGENCY = "District of Columbia Department of Health Care Finance"
NEWSROOM_URL = "https://dhcf.dc.gov/newsroom"
ROW_RE = re.compile(r'(?is)<div class="views-row[^>]*>(?P<body>.*?)(?=<div class="views-row|</div>\s*</div>\s*<div class="item-list")')
DATE_RE = re.compile(r'class="date-display-single"[^>]*content="(?P<date>20\d{2}-\d{2}-\d{2})T[^\"]*"')
TITLE_RE = re.compile(r'(?is)<div class="views-field views-field-title".*?<a href="(?P<href>[^"]+)"[^>]*>(?P<title>.*?)</a>')
CONTEXT = ("medicaid", "medicare", "waiver", "public notice", "provider", "reimbursement", "rural health", "health transformation", "regulation")


def parse_newsroom(markup: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for match in ROW_RE.finditer(markup):
        body = match.group("body")
        date_match = DATE_RE.search(body)
        title_match = TITLE_RE.search(body)
        if not date_match or not title_match:
            continue
        title = clean_text(title_match.group("title"))
        url = absolute_url(NEWSROOM_URL, title_match.group("href"))
        if not any(term in title.lower() for term in CONTEXT) or is_procurement_update(title, url):
            continue
        rows.append({"title": title, "posted_date": date_match.group("date"), "url": url})
    return rows


def fetch_updates(*, keywords: list[str], max_records: int, progress: Callable[[str], None] | None = None) -> list[dict[str, str]]:
    if max_records <= 0:
        return []
    rows = parse_newsroom(fetch_text(NEWSROOM_URL, timeout=20, byte_limit=500_000))
    records = [state_update_record(
        state="DC", source="dc_dhcf_policy_news", source_record_id=source_id_from_url(row["url"]),
        record_type=record_type_for(row["title"], "policy_update"), title=row["title"], agency=AGENCY,
        summary="Official DC DHCF Medicaid policy or program news.", posted_date=iso_date_text(row["posted_date"]),
        comment_required="public notice" in row["title"].lower(), document_url=row["url"],
        source_url=NEWSROOM_URL, keywords=keywords,
        raw={"source_page": NEWSROOM_URL, "procurement_excluded": True},
    ) for row in rows]
    output = unique_records(records)
    emit(progress, f"DC DHCF newsroom: normalized {len(output)} non-procurement policy updates")
    return output[:max_records]
