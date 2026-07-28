from __future__ import annotations

import datetime as dt
import re
from typing import Callable

from services.state_updates import emit, state_update_record
from services.state_updates.common import absolute_url, clean_text, fetch_text, is_procurement_update, record_type_for, source_id_from_url, unique_records

PUBLIC_NOTICES_URL = "https://dch.georgia.gov/meetings-notices/public-notices"
AGENCY = "Georgia Department of Community Health"
LI_RE = re.compile(r"(?is)<li[^>]*>(?P<body>.*?)</li>")
ANCHOR_RE = re.compile(r'(?is)<a\s+[^>]*href="(?P<href>[^"]+)"[^>]*data-text="(?P<title>[^"]+)"[^>]*>')
DATE_RE = re.compile(r"(?i)(?:posted\s+)?(\d{1,2}/\d{1,2}/(?:20)?\d{2})")
CONTEXT = ("medicaid", "cms", "rural", "hospital", "nursing", "waiver", "state plan", "payment", "reimbursement", "health benefit")


def parse_public_notices(markup: str) -> list[dict[str, str]]:
    rows = []
    for item in LI_RE.finditer(markup):
        body = item.group("body")
        anchor = ANCHOR_RE.search(body)
        date = DATE_RE.search(clean_text(body))
        if not anchor or not date:
            continue
        title = clean_text(anchor.group("title"))
        lower = title.lower()
        url = absolute_url(PUBLIC_NOTICES_URL, anchor.group("href"))
        if not any(term in lower for term in CONTEXT) or is_procurement_update(title, clean_text(body), url):
            continue
        rows.append({"title": title, "posted_date": notice_date(date.group(1)), "url": url})
    return rows


def notice_date(value: str) -> str:
    for fmt in ("%m/%d/%Y", "%m/%d/%y"):
        try:
            return dt.datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            pass
    return ""


def fetch_updates(*, keywords: list[str], max_records: int, progress: Callable[[str], None] | None = None) -> list[dict[str, str]]:
    if max_records <= 0:
        return []
    rows = parse_public_notices(fetch_text(PUBLIC_NOTICES_URL, timeout=35, byte_limit=1_500_000))
    records = [state_update_record(
        state="GA", source="ga_dch_public_notices", source_record_id=source_id_from_url(row["url"]),
        record_type=record_type_for(row["title"], "public_comment_notice"), title=row["title"], agency=AGENCY,
        summary="Official DCH health policy public notice.", posted_date=row["posted_date"], comment_required=True,
        document_url=row["url"], source_url=PUBLIC_NOTICES_URL, keywords=keywords,
        raw={"source_page": PUBLIC_NOTICES_URL, "procurement_excluded": True},
    ) for row in rows]
    output = unique_records(records)
    emit(progress, f"GA DCH public notices: scanned and kept {len(output)} non-procurement health policy notices")
    return output[:max_records]
