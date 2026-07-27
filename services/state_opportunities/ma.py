from __future__ import annotations

from typing import Callable

from services.state_opportunities.bso import BsoOpportunityConfig, fetch_bso_open_bid_opportunities

MA_COMMBUYS_CONFIG = BsoOpportunityConfig(
    state="MA",
    source_name="COMMBUYS Open Bids",
    source_key="ma_commbuys",
    base_url="https://www.commbuys.com/bso/",
)


def fetch_opportunities(
    *,
    keywords: list[str],
    days_back: int,
    max_records: int,
    progress: Callable[[str], None] | None = None,
) -> list[dict[str, str]]:
    return fetch_bso_open_bid_opportunities(
        config=MA_COMMBUYS_CONFIG,
        keywords=keywords,
        days_back=days_back,
        max_records=max_records,
        progress=progress,
    )
