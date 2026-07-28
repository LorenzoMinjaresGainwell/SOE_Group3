from __future__ import annotations

from typing import Callable
from services.state_updates.official_feed import collect_updates

AGENCY = "New Hampshire Department of Health and Human Services"
SOURCES = [
    {"key": "nh_dhhs_medicaid_provider_notices", "url": "https://www.dhhs.nh.gov/programs-services/medicaid/medicaid-provider-relations", "record_type": "provider_bulletin", "terms": ["medicaid", "provider", "bulletin", "state plan", "waiver", "rural health", "medicare"]},
]

def fetch_updates(*, keywords: list[str], max_records: int, progress: Callable[[str], None] | None = None) -> list[dict[str, str]]:
    return collect_updates(state="NH", agency=AGENCY, sources=SOURCES, keywords=keywords, max_records=max_records, progress=progress)
