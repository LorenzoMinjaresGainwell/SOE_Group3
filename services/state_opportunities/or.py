from __future__ import annotations

from typing import Callable

from services.state_opportunities.bso import BsoOpportunityConfig, fetch_bso_open_bid_opportunities

OR_OREGONBUYS_CONFIG = BsoOpportunityConfig(
    state="OR",
    source_name="OregonBuys Open Bids",
    source_key="or_oregonbuys",
    base_url="https://oregonbuys.gov/bso/",
)


def fetch_opportunities(
    *,
    keywords: list[str],
    days_back: int,
    max_records: int,
    progress: Callable[[str], None] | None = None,
) -> list[dict[str, str]]:
    return fetch_bso_open_bid_opportunities(
        config=OR_OREGONBUYS_CONFIG,
        keywords=keywords,
        days_back=days_back,
        max_records=max_records,
        progress=progress,
    )
