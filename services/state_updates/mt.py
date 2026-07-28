from __future__ import annotations

from typing import Callable

from services.state_updates._official_html import fetch_official_html_updates, source_rows
from services.state_updates.common import fetch_text

AGENCY = "Montana Department of Public Health and Human Services"
SOURCES = [
    ("mt_medicaid_spa_public_notices", "https://dphhs.mt.gov/MontanaHealthcarePrograms/MedicaidStatePlanAmendmentPublicNotices", "spa_notice", ["state plan", "state-plan", "medicaid", "waiver", "publicnotice"]),
]


def fetch_updates(*, keywords: list[str], max_records: int, progress: Callable[[str], None] | None = None) -> list[dict[str, str]]:
    return fetch_official_html_updates(state="MT", agency=AGENCY, sources=SOURCES, keywords=keywords,
                                       max_records=max_records, progress=progress, fetcher=fetch_text)
