from __future__ import annotations

from typing import Callable

from services.state_updates._official_html import fetch_official_html_updates, source_rows
from services.state_updates.common import fetch_text

AGENCY = "Maine Department of Health and Human Services, Office of MaineCare Services"
SOURCES = [
    ("me_mainecare_bulletins", "https://www.maine.gov/dhhs/oms/about-us/mainecare-bulletins", "provider_bulletin", ["provider-bulletins", "mainecare bulletin"]),
]


def fetch_updates(*, keywords: list[str], max_records: int, progress: Callable[[str], None] | None = None) -> list[dict[str, str]]:
    return fetch_official_html_updates(state="ME", agency=AGENCY, sources=SOURCES, keywords=keywords,
                                       max_records=max_records, progress=progress, fetcher=fetch_text)
