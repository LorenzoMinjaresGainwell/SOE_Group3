from __future__ import annotations

from typing import Callable

from services.state_opportunities.bso import BsoOpportunityConfig, fetch_bso_open_bid_opportunities

AR_ARBUY_CONFIG = BsoOpportunityConfig(
    state="AR",
    source_name="Arkansas ARBuy Open Bids",
    source_key="ar_vendor_services",
    base_url="https://arbuy.arkansas.gov/bso/",
    source_note=(
        "Official Arkansas SAS current solicitations page links ARBuy BSO open-bids route; "
        "adapter parses the public BSO openBids=true results page."
    ),
)


def fetch_opportunities(
    *,
    keywords: list[str],
    days_back: int,
    max_records: int,
    progress: Callable[[str], None] | None = None,
) -> list[dict[str, str]]:
    return fetch_bso_open_bid_opportunities(
        config=AR_ARBUY_CONFIG,
        keywords=keywords,
        days_back=days_back,
        max_records=max_records,
        progress=progress,
    )
