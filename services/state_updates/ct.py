from __future__ import annotations

from typing import Callable

from services.state_updates._official_html import fetch_official_html_updates, source_rows
from services.state_updates.common import fetch_text

AGENCY = "Connecticut Department of Social Services"
SOURCES = [
    ("ct_dss_spa", "https://portal.ct.gov/dss/health-and-home-care/medicaid-state-plan-amendments", "spa_notice", ["spa", "state-plan", "state plan"]),
    ("ct_dss_waivers", "https://portal.ct.gov/dss/health-and-home-care/medicaid-waiver-applications/medicaid-waiver-applications", "waiver_notice", ["waiver", "notice-of-intent", "notice of intent"]),
]


def fetch_updates(*, keywords: list[str], max_records: int, progress: Callable[[str], None] | None = None) -> list[dict[str, str]]:
    return fetch_official_html_updates(state="CT", agency=AGENCY, sources=SOURCES, keywords=keywords,
                                       max_records=max_records, progress=progress, fetcher=fetch_text)
