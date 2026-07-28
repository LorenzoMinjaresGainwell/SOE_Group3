from __future__ import annotations

from typing import Callable
from services.state_updates.official_feed import collect_updates

AGENCY = "Rhode Island Executive Office of Health and Human Services"
SOURCES = [
    {"key": "ri_eohhs_provider_updates", "url": "https://eohhs.ri.gov/providers-partners/provider-updates", "record_type": "provider_bulletin", "terms": ["medicaid", "provider update", "provider", "medicare"]},
    {"key": "ri_eohhs_spa_waiver_changes", "url": "https://eohhs.ri.gov/reference-center/medicaid-state-plan-and-1115-waiver/spa-and-1115-waiver-changes", "record_type": "spa_notice", "terms": ["state plan", "spa", "1115", "waiver", "public notice"]},
    {"key": "ri_eohhs_rht", "url": "https://eohhs.ri.gov/initiatives/rural-health-transformation-program", "record_type": "rht_notice", "terms": ["rural health transformation", "rht", "funding", "application"]},
]

def fetch_updates(*, keywords: list[str], max_records: int, progress: Callable[[str], None] | None = None) -> list[dict[str, str]]:
    return collect_updates(state="RI", agency=AGENCY, sources=SOURCES, keywords=keywords, max_records=max_records, progress=progress)
