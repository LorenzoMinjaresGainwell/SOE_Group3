from __future__ import annotations

from typing import Callable

from services.state_opportunities.bso import BsoOpportunityConfig, fetch_bso_open_bid_opportunities

NJ_NJSTART_CONFIG = BsoOpportunityConfig(
    state="NJ",
    source_name="NJSTART Open Bids",
    source_key="nj_njstart",
    base_url="https://www.njstart.gov/bso/",
)


def fetch_opportunities(
    *,
    keywords: list[str],
    days_back: int,
    max_records: int,
    progress: Callable[[str], None] | None = None,
) -> list[dict[str, str]]:
    return fetch_bso_open_bid_opportunities(
        config=NJ_NJSTART_CONFIG,
        keywords=keywords,
        days_back=days_back,
        max_records=max_records,
        progress=progress,
    )
