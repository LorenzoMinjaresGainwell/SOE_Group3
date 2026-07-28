from __future__ import annotations

from typing import Callable
from services.state_updates.official_feed import collect_updates

AGENCY = "South Carolina Department of Health and Human Services"
SOURCES = [
    {"key": "sc_scdhhs_public_notices", "url": "https://www.scdhhs.gov/communications/public-notices", "record_type": "public_comment_notice", "terms": ["public notice", "medicaid", "state plan", "waiver", "rural health", "medicare"]},
    {"key": "sc_scdhhs_state_plan_notices", "url": "https://www.scdhhs.gov/communications/state-plan-notices", "record_type": "spa_notice", "terms": ["state plan", "amendment", "public notice"]},
]

def fetch_updates(*, keywords: list[str], max_records: int, progress: Callable[[str], None] | None = None) -> list[dict[str, str]]:
    return collect_updates(state="SC", agency=AGENCY, sources=SOURCES, keywords=keywords, max_records=max_records, progress=progress)
