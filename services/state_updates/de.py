from __future__ import annotations

from typing import Callable

from services.state_updates import emit, sort_key, state_update_record
from services.state_updates.common import clean_text, fetch_text, first_date_text, is_procurement_update, iso_date_text, parse_links, record_type_for, source_id_from_url, unique_records

AGENCY = "Delaware Division of Medicaid and Medical Assistance"
SOURCE_URL = "https://medicaid.dhss.delaware.gov/provider/Home/ProviderBulletins"
CONTEXT = ("medicaid", "provider bulletin", "state plan", "waiver", "public notice", "rural health")


def parse_updates(markup: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for link in parse_links(markup, SOURCE_URL):
        title = clean_text(link.text)
        text = f"{title} {link.href}".lower()
        if not title or not any(term in text for term in CONTEXT) or is_procurement_update(title, link.href):
            continue
        rows.append({"title": title, "posted_date": iso_date_text(first_date_text(title)), "url": link.href})
    return rows


def fetch_updates(*, keywords: list[str], max_records: int, progress: Callable[[str], None] | None = None) -> list[dict[str, str]]:
    if max_records <= 0:
        return []
    try:
        markup = fetch_text(SOURCE_URL, timeout=15, byte_limit=500_000)
    except Exception as exc:
        emit(progress, f"DE Medicaid provider bulletins unavailable: {exc}")
        return []
    records = [state_update_record(
        state="DE", source="de_dmma_policy_updates", source_record_id=source_id_from_url(row["url"]),
        record_type=record_type_for(row["title"], "provider_bulletin"), title=row["title"], agency=AGENCY,
        summary="Official Delaware Medicaid policy or provider update.", posted_date=row["posted_date"],
        comment_required="public notice" in row["title"].lower(), document_url=row["url"], source_url=SOURCE_URL,
        keywords=keywords, raw={"source_page": SOURCE_URL, "procurement_excluded": True},
    ) for row in parse_updates(markup)]
    output = sorted(unique_records(records), key=sort_key, reverse=True)
    emit(progress, f"DE Medicaid updates: normalized {len(output)} official non-procurement updates")
    return output[:max_records]
