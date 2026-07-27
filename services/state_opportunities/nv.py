from __future__ import annotations

from typing import Callable

from services.state_opportunities.bso import BsoOpportunityConfig, fetch_bso_open_bid_opportunities

NV_NEVADAEPRO_CONFIG = BsoOpportunityConfig(
    state="NV",
    source_name="NevadaEPro Open Bids",
    source_key="nv_nevadaepro",
    base_url="https://nevadaepro.com/bso/",
)


def fetch_opportunities(
    *,
    keywords: list[str],
    days_back: int,
    max_records: int,
    progress: Callable[[str], None] | None = None,
) -> list[dict[str, str]]:
    return fetch_bso_open_bid_opportunities(
        config=NV_NEVADAEPRO_CONFIG,
        keywords=keywords,
        days_back=days_back,
        max_records=max_records,
        progress=progress,
    )
