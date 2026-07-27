from __future__ import annotations

from typing import Callable

from services.state_contracts.bso import BsoContractConfig, fetch_bso_active_contracts

IL_BIDBUY_CONTRACT_CONFIG = BsoContractConfig(
    state="IL",
    source_name="Illinois BidBuy Active Contracts",
    source_key="il_bidbuy",
    base_url="https://www.bidbuy.illinois.gov/bso/",
)


def fetch_contracts(
    *,
    vendor_terms: list[str],
    keywords: list[str],
    max_per_vendor: int,
    progress: Callable[[str], None] | None = None,
) -> list[dict[str, str]]:
    return fetch_bso_active_contracts(
        config=IL_BIDBUY_CONTRACT_CONFIG,
        vendor_terms=vendor_terms,
        keywords=keywords,
        max_per_vendor=max_per_vendor,
        progress=progress,
    )
