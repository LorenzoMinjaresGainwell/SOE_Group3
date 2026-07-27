from __future__ import annotations

from typing import Callable

from services.state_opportunities.public_purchase import PublicPurchaseConfig, fetch_public_purchase_opportunities

CONFIG = PublicPurchaseConfig(
    state="WY",
    source_name="Wyoming Public Purchase Bid Opportunities",
    public_info_url="https://www.publicpurchase.com/gems/wyominggsd,wy/buyer/public/publicInfo",
    source_key="wy_procurement",
    agency="Wyoming Administration & Information - General Services Division",
    official_source_url="https://ai.wyo.gov/divisions/general-services/purchasing/bid-opportunities",
)


def fetch_opportunities(
    *,
    keywords: list[str],
    days_back: int,
    max_records: int,
    progress: Callable[[str], None] | None = None,
) -> list[dict[str, str]]:
    return fetch_public_purchase_opportunities(
        config=CONFIG,
        keywords=keywords,
        days_back=days_back,
        max_records=max_records,
        progress=progress,
    )
