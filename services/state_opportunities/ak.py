from __future__ import annotations

from typing import Callable

from services.state_opportunities.vss import VssOpportunityConfig, fetch_vss_published_solicitations

AK_IRIS_VSS_CONFIG = VssOpportunityConfig(
    state="AK",
    source_name="Alaska IRIS VSS Published Solicitations",
    source_key="ak_iris_vss",
    base_url="https://iris-vss.alaska.gov/PRDVSS1X1/Advantage4",
)


def fetch_opportunities(
    *,
    keywords: list[str],
    days_back: int,
    max_records: int,
    progress: Callable[[str], None] | None = None,
) -> list[dict[str, str]]:
    return fetch_vss_published_solicitations(
        config=AK_IRIS_VSS_CONFIG,
        keywords=keywords,
        days_back=days_back,
        max_records=max_records,
        progress=progress,
    )
