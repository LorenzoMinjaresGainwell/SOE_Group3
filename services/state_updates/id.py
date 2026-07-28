from __future__ import annotations

from typing import Callable
from services.state_updates.official_feed import collect_updates

AGENCY = "Idaho Department of Health and Welfare"
SOURCES = [
    {"key": "id_dhw_medicaid_news", "url": "https://healthandwelfare.idaho.gov/news", "record_type": "medicaid_notice", "terms": ["medicaid", "state plan", "waiver", "rural health"]},
    {"key": "id_dhw_rht", "url": "https://healthandwelfare.idaho.gov/providers/rural-health-transformation-program-grant/about-rural-health-transformation-program-grant", "record_type": "rht_notice", "terms": ["rural health transformation", "funding", "application"]},
]

def fetch_updates(*, keywords: list[str], max_records: int, progress: Callable[[str], None] | None = None) -> list[dict[str, str]]:
    return collect_updates(state="ID", agency=AGENCY, sources=SOURCES, keywords=keywords, max_records=max_records, progress=progress)
