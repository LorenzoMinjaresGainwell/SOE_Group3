from __future__ import annotations

from typing import Callable

from services.state_opportunities.vss import VssOpportunityConfig, fetch_vss_published_solicitations

WV_OASIS_VSS_CONFIG = VssOpportunityConfig(
    state="WV",
    source_name="West Virginia wvOASIS VSS Published Solicitations",
    source_key="wv_oasis_vss",
    base_url="https://prd311.wvoasis.gov/PRDVSS1X1ERP/Advantage4",
)


def fetch_opportunities(
    *,
    keywords: list[str],
    days_back: int,
    max_records: int,
    progress: Callable[[str], None] | None = None,
) -> list[dict[str, str]]:
    return fetch_vss_published_solicitations(
        config=WV_OASIS_VSS_CONFIG,
        keywords=keywords,
        days_back=days_back,
        max_records=max_records,
        progress=progress,
    )
