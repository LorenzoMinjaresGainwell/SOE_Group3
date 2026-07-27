from __future__ import annotations

from typing import Callable

from services.state_contracts.bso import BsoContractConfig, fetch_bso_active_contracts

NJ_NJSTART_CONTRACT_CONFIG = BsoContractConfig(
    state="NJ",
    source_name="NJSTART Active Contracts",
    source_key="nj_njstart",
    base_url="https://www.njstart.gov/bso/",
)


def fetch_contracts(
    *,
    vendor_terms: list[str],
    keywords: list[str],
    max_per_vendor: int,
    progress: Callable[[str], None] | None = None,
) -> list[dict[str, str]]:
    return fetch_bso_active_contracts(
        config=NJ_NJSTART_CONTRACT_CONFIG,
        vendor_terms=vendor_terms,
        keywords=keywords,
        max_per_vendor=max_per_vendor,
        progress=progress,
    )
