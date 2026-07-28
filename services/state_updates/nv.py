from __future__ import annotations

from typing import Callable
from services.state_updates.official_feed import collect_updates

AGENCY = "Nevada Department of Health and Human Services, Division of Health Care Financing and Policy"
SOURCES = [
    {"key": "nv_dhcfp_public_notices", "url": "https://www.nevadamedicaid.nv.gov/public-notices/", "record_type": "public_comment_notice", "terms": ["medicaid", "public notice", "state plan", "waiver", "rate", "rural health", "medicare"]},
]

def fetch_updates(*, keywords: list[str], max_records: int, progress: Callable[[str], None] | None = None) -> list[dict[str, str]]:
    return collect_updates(state="NV", agency=AGENCY, sources=SOURCES, keywords=keywords, max_records=max_records, progress=progress)
