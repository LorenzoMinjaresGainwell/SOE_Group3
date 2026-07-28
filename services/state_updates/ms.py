from __future__ import annotations

from typing import Callable
from services.state_updates.official_feed import collect_updates

AGENCY = "Mississippi Division of Medicaid"
SOURCES = [
    {"key": "ms_dom_provider_bulletins", "url": "https://medicaid.ms.gov/providers/provider-resources/provider-bulletins/", "record_type": "provider_bulletin", "terms": ["provider bulletin", "medicaid", "state plan", "waiver", "rural health", "medicare"]},
]

def fetch_updates(*, keywords: list[str], max_records: int, progress: Callable[[str], None] | None = None) -> list[dict[str, str]]:
    return collect_updates(state="MS", agency=AGENCY, sources=SOURCES, keywords=keywords, max_records=max_records, progress=progress)
