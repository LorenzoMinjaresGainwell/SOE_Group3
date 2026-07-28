from __future__ import annotations

import datetime as dt
import re
from typing import Callable

from services.state_updates import emit, state_update_record
from services.state_updates.common import clean_text, fetch_text, parse_links, source_id_from_url, unique_records

PROVIDER_UPDATES_URL = "https://www.lamedicaid.com/providerupdate/providerupdates.htm"
AGENCY = "Louisiana Department of Health, Louisiana Medicaid"
FILE_RE = re.compile(r"provider_update_(?P<month>\d{2})_(?P<year>\d{2})\.pdf$", re.I)


def parse_provider_updates(markup: str) -> list[dict[str, str]]:
    rows = []
    for link in parse_links(markup, PROVIDER_UPDATES_URL):
        match = FILE_RE.search(link.href)
        if not match:
            continue
        try:
            posted_date = dt.datetime.strptime(clean_text(link.text), "%m/%d/%y").date().isoformat()
        except ValueError:
            continue
        rows.append({"title": f"Louisiana Medicaid Provider Update — {posted_date[:7]}", "posted_date": posted_date, "url": link.href})
    return rows


def fetch_updates(*, keywords: list[str], max_records: int, progress: Callable[[str], None] | None = None) -> list[dict[str, str]]:
    if max_records <= 0:
        return []
    rows = parse_provider_updates(fetch_text(PROVIDER_UPDATES_URL, timeout=30, byte_limit=900_000))
    records = [state_update_record(
        state="LA", source="la_medicaid_provider_updates", source_record_id=source_id_from_url(row["url"]),
        record_type="provider_bulletin", title=row["title"], agency=AGENCY,
        summary="Official monthly Louisiana Medicaid provider newsletter.", posted_date=row["posted_date"],
        document_url=row["url"], source_url=PROVIDER_UPDATES_URL, keywords=keywords,
        raw={"source_page": PROVIDER_UPDATES_URL},
    ) for row in rows]
    output = unique_records(records)
    emit(progress, f"LA Medicaid Provider Updates: normalized {len(output)} monthly issues")
    return output[:max_records]
