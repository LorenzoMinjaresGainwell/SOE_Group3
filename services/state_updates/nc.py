from __future__ import annotations

import re
from typing import Callable

from services.state_updates import emit, state_update_record
from services.state_updates.common import absolute_url, clean_text, fetch_text, is_procurement_update, source_id_from_url, unique_records

BULLETIN_URL = "https://medicaid.ncdhhs.gov/providers/medicaid-bulletin"
AGENCY = "North Carolina Department of Health and Human Services, Division of Health Benefits"
ROW_RE = re.compile(
    r'(?is)<div class="views-row">.*?<a href="(?P<href>[^"]+)"[^>]*>(?P<title>.*?)</a>.*?'
    r'<time datetime="(?P<date>\d{4}-\d{2}-\d{2})[^\"]*"[^>]*>.*?</time>.*?'
    r'<div class="field-content">(?P<summary>.*?)</div>\s*</div>\s*</div>'
)


def parse_bulletins(markup: str) -> list[dict[str, str]]:
    rows = []
    for match in ROW_RE.finditer(markup):
        title = clean_text(match.group("title"))
        summary = clean_text(match.group("summary"))
        url = absolute_url(BULLETIN_URL, match.group("href"))
        if is_procurement_update(title, summary, url):
            continue
        rows.append({"title": title, "summary": summary, "posted_date": match.group("date"), "url": url})
    return rows


def fetch_updates(*, keywords: list[str], max_records: int, progress: Callable[[str], None] | None = None) -> list[dict[str, str]]:
    if max_records <= 0:
        return []
    rows = parse_bulletins(fetch_text(BULLETIN_URL, timeout=30, byte_limit=1_500_000))
    records = [state_update_record(
        state="NC", source="nc_medicaid_bulletins", source_record_id=source_id_from_url(row["url"]),
        record_type="provider_bulletin", title=row["title"], agency=AGENCY, summary=row["summary"],
        posted_date=row["posted_date"], document_url=row["url"], source_url=BULLETIN_URL, keywords=keywords,
        raw={"source_page": BULLETIN_URL},
    ) for row in rows]
    output = unique_records(records)
    emit(progress, f"NC Medicaid bulletins: scanned {len(rows)}, normalized {len(output)}")
    return output[:max_records]
