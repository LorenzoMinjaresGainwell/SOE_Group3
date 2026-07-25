from __future__ import annotations

from typing import Callable

from services.state_opportunities.bso import BsoOpportunityConfig, fetch_bso_open_bid_opportunities

IL_BIDBUY_CONFIG = BsoOpportunityConfig(
    state="IL",
    source_name="Illinois BidBuy Open Bids",
    source_key="il_bidbuy",
    base_url="https://www.bidbuy.illinois.gov/bso/",
)


def fetch_opportunities(
    *,
    keywords: list[str],
    days_back: int,
    max_records: int,
    progress: Callable[[str], None] | None = None,
) -> list[dict[str, str]]:
    return fetch_bso_open_bid_opportunities(
        config=IL_BIDBUY_CONFIG,
        keywords=keywords,
        days_back=days_back,
        max_records=max_records,
        progress=progress,
    )
