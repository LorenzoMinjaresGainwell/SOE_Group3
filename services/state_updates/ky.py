from __future__ import annotations

import re
from typing import Callable

from services.state_updates import emit, state_update_record
from services.state_updates.common import absolute_url, clean_text, fetch_text, first_date_text, is_procurement_update, iso_date_text, parse_links, record_type_for, source_id_from_url, unique_records

PUBLIC_NOTICES_URL = "https://www.chfs.ky.gov/agencies/dms/Pages/publicnotices.aspx"
AGENCY = "Kentucky Cabinet for Health and Family Services, Department for Medicaid Services"


def parse_public_notices(markup: str) -> list[dict[str, str]]:
    rows = []
    for link in parse_links(markup, PUBLIC_NOTICES_URL):
        title = clean_text(link.text).replace("\u200b", "")
        if "public notice" not in title.lower() or not re.search(r"/agencies/dms/documents/", link.href, re.I):
            continue
        if is_procurement_update(title, link.href):
            continue
        posted_date = iso_date_text(first_date_text(title))
        if posted_date:
            rows.append({"title": re.sub(r"\s*-?\s*PDF\s*$", "", title, flags=re.I), "posted_date": posted_date, "url": link.href})
    return rows


def fetch_updates(*, keywords: list[str], max_records: int, progress: Callable[[str], None] | None = None) -> list[dict[str, str]]:
    if max_records <= 0:
        return []
    rows = parse_public_notices(fetch_text(PUBLIC_NOTICES_URL, timeout=30, byte_limit=900_000))
    records = [state_update_record(
        state="KY", source="ky_dms_public_notices", source_record_id=source_id_from_url(row["url"]),
        record_type=record_type_for(row["title"], "public_comment_notice"), title=row["title"], agency=AGENCY,
        summary="Official Kentucky Medicaid public notice.", posted_date=row["posted_date"], comment_required=True,
        document_url=row["url"], source_url=PUBLIC_NOTICES_URL, keywords=keywords,
        raw={"source_page": PUBLIC_NOTICES_URL},
    ) for row in rows]
    output = unique_records(records)
    emit(progress, f"KY Medicaid public notices: normalized {len(output)} dated notices")
    return output[:max_records]
