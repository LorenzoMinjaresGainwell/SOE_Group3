from __future__ import annotations

from typing import Callable

from services.state_opportunities.bso import BsoOpportunityConfig, fetch_bso_open_bid_opportunities

CONFIG = BsoOpportunityConfig(
    state="VI",
    source_name="GVIBUY Open Bids",
    source_key="vi_dpp_procurement",
    base_url="https://gvibuy.buyspeed.com/bso/",
)


def fetch_opportunities(
    *,
    keywords: list[str],
    days_back: int,
    max_records: int,
    progress: Callable[[str], None] | None = None,
) -> list[dict[str, str]]:
    return fetch_bso_open_bid_opportunities(
        config=CONFIG,
        keywords=keywords,
        days_back=days_back,
        max_records=max_records,
        progress=progress,
    )
