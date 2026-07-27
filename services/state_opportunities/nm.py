from __future__ import annotations

from typing import Callable

from services.state_opportunities.bonfire import BonfireConfig, fetch_bonfire_opportunities

CONFIG = BonfireConfig(
    state="NM",
    source_name="New Mexico GSD Bonfire Open Opportunities",
    source_key="nm_state_purchasing",
    portal_base_url="https://nmgeneralservices.bonfirehub.com/",
    official_source_url="https://www.generalservices.state.nm.us/state-purchasing/",
    agency_fallback="New Mexico General Services Department",
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
