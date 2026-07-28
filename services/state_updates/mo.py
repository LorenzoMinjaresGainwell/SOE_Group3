from __future__ import annotations

from typing import Callable
from services.state_updates.official_feed import collect_updates

AGENCY = "Missouri Department of Social Services, MO HealthNet Division"
SOURCES = [
    {"key": "mo_healthnet_news", "url": "https://mydss.mo.gov/mhd/news", "record_type": "medicaid_notice", "terms": ["medicaid", "mo healthnet", "provider", "waiver", "state plan", "medicare"]},
    {"key": "mo_healthnet_alerts", "url": "https://mydss.mo.gov/mhd/alerts", "record_type": "provider_bulletin", "terms": ["medicaid", "mo healthnet", "provider", "waiver", "state plan", "medicare"]},
]

def fetch_updates(*, keywords: list[str], max_records: int, progress: Callable[[str], None] | None = None) -> list[dict[str, str]]:
    return collect_updates(state="MO", agency=AGENCY, sources=SOURCES, keywords=keywords, max_records=max_records, progress=progress)
