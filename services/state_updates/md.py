from __future__ import annotations

import urllib.parse
from typing import Any, Callable

from services.state_updates import emit, sort_key, state_update_record
from services.state_updates.common import clean_text, fetch_json_data, is_procurement_update, source_id_from_url, unique_records

AGENCY = "Maryland Department of Health"
SOURCE_URL = "https://health.maryland.gov/mmcp/provider/Pages/transmittals.aspx"
API_URL = "https://health.maryland.gov/mmcp/provider/_api/web/lists/GetByTitle('Provider-Transmittals')/items"
MAX_SOURCE_ROWS = 100


def api_url(max_records: int) -> str:
    top = min(MAX_SOURCE_ROWS, max(1, max_records * 3))
    query = urllib.parse.urlencode({
        "$select": "Id,Title,Date,Topic,DetailLink,ProviderTypes",
        "$orderby": "Date desc",
        "$top": str(top),
    })
    return f"{API_URL}?{query}"


def parse_transmittals(payload: Any) -> list[dict[str, str]]:
    source_rows = payload.get("value", []) if isinstance(payload, dict) else []
    rows: list[dict[str, str]] = []
    for item in source_rows:
        if not isinstance(item, dict):
            continue
        detail = item.get("DetailLink")
        detail = detail if isinstance(detail, dict) else {}
        url = clean_text(detail.get("Url"))
        number = clean_text(detail.get("Description"))
        provider = clean_text(item.get("Title"))
        topic = clean_text(item.get("Topic"))
        title = " — ".join(part for part in (number, topic) if part)
        if not title or not url or is_procurement_update(title, provider, url):
            continue
        rows.append({
            "id": clean_text(item.get("Id")) or source_id_from_url(url),
            "title": title,
            "posted_date": clean_text(item.get("Date")),
            "url": url,
            "provider": provider,
        })
    return rows


def fetch_updates(*, keywords: list[str], max_records: int, progress: Callable[[str], None] | None = None) -> list[dict[str, str]]:
    if max_records <= 0:
        return []
    payload = fetch_json_data(api_url(max_records), timeout=20, byte_limit=750_000)
    rows = parse_transmittals(payload)
    records = [state_update_record(
        state="MD", source="md_medicaid_provider_transmittals", source_record_id=row["id"],
        record_type="provider_bulletin", title=row["title"], agency=AGENCY,
        summary=f"Official Maryland Medicaid provider transmittal for {row['provider'] or 'Medicaid providers'}.",
        posted_date=row["posted_date"], document_url=row["url"], source_url=SOURCE_URL, keywords=keywords,
        raw={"provider_type": row["provider"], "source_page": SOURCE_URL, "procurement_excluded": True},
    ) for row in rows]
    output = sorted(unique_records(records), key=sort_key, reverse=True)
    emit(progress, f"MD Medicaid transmittals: normalized {len(output)} non-procurement provider updates")
    return output[:max_records]
