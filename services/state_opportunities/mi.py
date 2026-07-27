from __future__ import annotations

from typing import Callable

from services.state_opportunities.vss import VssOpportunityConfig, fetch_vss_published_solicitations

MI_SIGMA_VSS_CONFIG = VssOpportunityConfig(
    state="MI",
    source_name="Michigan SIGMA VSS Published Solicitations",
    source_key="mi_sigma_vss",
    base_url="https://sigma.michigan.gov/PRDVSS1X1/Advantage4",
)


def fetch_opportunities(
    *,
    keywords: list[str],
    days_back: int,
    max_records: int,
    progress: Callable[[str], None] | None = None,
) -> list[dict[str, str]]:
    return fetch_vss_published_solicitations(
        config=MI_SIGMA_VSS_CONFIG,
        keywords=keywords,
        days_back=days_back,
        max_records=max_records,
        progress=progress,
    )
