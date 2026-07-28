from __future__ import annotations

from typing import Callable
from services.state_updates.official_feed import collect_updates

AGENCY = "Nebraska Department of Health and Human Services, Division of Medicaid and Long-Term Care"
SOURCES = [
    {"key": "ne_mltc_provider_bulletins", "url": "https://dhhs.ne.gov/Pages/Medicaid-Provider-Bulletins.aspx", "record_type": "provider_bulletin", "terms": ["provider bulletin", "medicaid", "heritage health", "state plan", "waiver", "rural health", "medicare"]},
]

def fetch_updates(*, keywords: list[str], max_records: int, progress: Callable[[str], None] | None = None) -> list[dict[str, str]]:
    return collect_updates(state="NE", agency=AGENCY, sources=SOURCES, keywords=keywords, max_records=max_records, progress=progress)
