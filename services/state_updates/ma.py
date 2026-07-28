from __future__ import annotations

from typing import Callable

from services.state_updates import emit, sort_key, state_update_record
from services.state_updates.common import clean_text, fetch_text, first_date_text, is_procurement_update, iso_date_text, parse_links, source_id_from_url, unique_records

AGENCY = "Massachusetts Executive Office of Health and Human Services / MassHealth"
SOURCE_URL = "https://www.mass.gov/lists/masshealth-provider-bulletins"


def parse_bulletins(markup: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for link in parse_links(markup, SOURCE_URL):
        title = clean_text(link.text)
        lower = title.lower()
        if "masshealth" not in lower or "bulletin" not in lower or is_procurement_update(title, link.href):
            continue
        rows.append({"title": title, "posted_date": iso_date_text(first_date_text(title)), "url": link.href})
    return rows


def fetch_updates(*, keywords: list[str], max_records: int, progress: Callable[[str], None] | None = None) -> list[dict[str, str]]:
    if max_records <= 0:
        return []
    try:
        markup = fetch_text(SOURCE_URL, timeout=15, byte_limit=500_000)
    except Exception as exc:
        emit(progress, f"MA MassHealth bulletins blocked or unavailable: {exc}; no bypass attempted")
        return []
    if "not allowed | mass gov" in markup[:20_000].lower():
        emit(progress, "MA MassHealth bulletins blocked by the official site's access policy; no bypass attempted")
        return []
    records = [state_update_record(
        state="MA", source="ma_masshealth_provider_bulletins", source_record_id=source_id_from_url(row["url"]),
        record_type="provider_bulletin", title=row["title"], agency=AGENCY,
        summary="Official MassHealth provider bulletin.", posted_date=row["posted_date"], document_url=row["url"],
        source_url=SOURCE_URL, keywords=keywords, raw={"source_page": SOURCE_URL, "procurement_excluded": True},
    ) for row in parse_bulletins(markup)]
    output = sorted(unique_records(records), key=sort_key, reverse=True)
    emit(progress, f"MA MassHealth bulletins: normalized {len(output)} official provider updates")
    return output[:max_records]
