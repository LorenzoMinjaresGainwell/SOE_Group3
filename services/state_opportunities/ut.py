from __future__ import annotations

from typing import Callable

from services.state_opportunities.bonfire import BonfireConfig, fetch_bonfire_opportunities

CONFIG = BonfireConfig(
    state="UT",
    source_name="Utah Public Procurement Place Bonfire Open Opportunities",
    source_key="ut_procurement_place",
    portal_base_url="https://utah.bonfirehub.com/",
    official_source_url="https://purchasing.utah.gov/for-vendors/",
    agency_fallback="Utah Public Procurement Place",
)


def fetch_opportunities(
    *,
    keywords: list[str],
    days_back: int,
    max_records: int,
    progress: Callable[[str], None] | None = None,
) -> list[dict[str, str]]:
    return fetch_bonfire_opportunities(
        config=CONFIG,
        keywords=keywords,
        days_back=days_back,
        max_records=max_records,
        progress=progress,
    )
