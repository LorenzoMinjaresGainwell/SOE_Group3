from __future__ import annotations

from typing import Callable
from services.state_updates.official_feed import collect_updates

AGENCY = "Oklahoma Health Care Authority"
SOURCES = [
    {"key": "ok_ohca_policy", "url": "https://oklahoma.gov/ohca/about/policy.html", "record_type": "public_comment_notice", "terms": ["medicaid", "soonercare", "state plan", "waiver", "public comment", "provider", "medicare"]},
    {"key": "ok_ohca_newsroom", "url": "https://oklahoma.gov/ohca/about/newsroom.html", "record_type": "medicaid_notice", "terms": ["medicaid", "soonercare", "waiver", "provider", "rural health"]},
]

def fetch_updates(*, keywords: list[str], max_records: int, progress: Callable[[str], None] | None = None) -> list[dict[str, str]]:
    return collect_updates(state="OK", agency=AGENCY, sources=SOURCES, keywords=keywords, max_records=max_records, progress=progress)
