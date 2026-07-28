from __future__ import annotations

from typing import Callable
from services.state_updates.official_feed import collect_updates

AGENCY = "Kansas Department of Health and Environment, Division of Health Care Finance"
SOURCES = [
    {"key": "ks_kancare_publications", "url": "https://www.kancare.ks.gov/about-kancare/publications", "record_type": "medicaid_notice", "terms": ["medicaid", "kancare", "provider", "state plan", "waiver", "rural health", "medicare"]},
]

def fetch_updates(*, keywords: list[str], max_records: int, progress: Callable[[str], None] | None = None) -> list[dict[str, str]]:
    return collect_updates(state="KS", agency=AGENCY, sources=SOURCES, keywords=keywords, max_records=max_records, progress=progress)
