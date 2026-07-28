from __future__ import annotations

from typing import Callable
from services.state_updates.official_feed import collect_updates

AGENCY = "Utah Department of Health and Human Services, Division of Integrated Healthcare"
SOURCES = [
    {"key": "ut_medicaid_information_bulletins", "url": "https://medicaid.utah.gov/medicaid-information-bulletins/", "record_type": "provider_bulletin", "terms": ["medicaid information bulletin", "mib", "provider", "medicare"]},
    {"key": "ut_medicaid_public_notices", "url": "https://medicaid.utah.gov/full-public-notice/", "record_type": "public_comment_notice", "terms": ["public notice", "state plan", "waiver", "medicaid", "rural health"]},
]

def fetch_updates(*, keywords: list[str], max_records: int, progress: Callable[[str], None] | None = None) -> list[dict[str, str]]:
    return collect_updates(state="UT", agency=AGENCY, sources=SOURCES, keywords=keywords, max_records=max_records, progress=progress)
