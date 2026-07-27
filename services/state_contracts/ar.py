from __future__ import annotations

from typing import Callable

from services.state_contracts.bso import BsoContractConfig, fetch_bso_active_contracts

AR_ARBUY_CONTRACT_CONFIG = BsoContractConfig(
    state="AR",
    source_name="Arkansas ARBuy Active Contracts",
    source_key="ar_vendor_services",
    base_url="https://arbuy.arkansas.gov/bso/",
    source_note=(
        "Official ARBuy BSO active-contracts route; searches current contract/blanket records, "
        "not open bids. Arkansas Vendor Services catalog host was DNS-dead during source discovery."
    ),
)


def fetch_contracts(
    *,
    vendor_terms: list[str],
    keywords: list[str],
    max_per_vendor: int,
    progress: Callable[[str], None] | None = None,
) -> list[dict[str, str]]:
    return fetch_bso_active_contracts(
        config=AR_ARBUY_CONTRACT_CONFIG,
        vendor_terms=vendor_terms,
        keywords=keywords,
        max_per_vendor=max_per_vendor,
        progress=progress,
    )
