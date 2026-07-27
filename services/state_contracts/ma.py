from __future__ import annotations

from typing import Callable

from services.state_contracts.bso import BsoContractConfig, fetch_bso_active_contracts

MA_COMMBUYS_CONTRACT_CONFIG = BsoContractConfig(
    state="MA",
    source_name="COMMBUYS Active Contracts",
    source_key="ma_commbuys",
    base_url="https://www.commbuys.com/bso/",
)


def fetch_contracts(
    *,
    vendor_terms: list[str],
    keywords: list[str],
    max_per_vendor: int,
    progress: Callable[[str], None] | None = None,
) -> list[dict[str, str]]:
    return fetch_bso_active_contracts(
        config=MA_COMMBUYS_CONTRACT_CONFIG,
        vendor_terms=vendor_terms,
        keywords=keywords,
        max_per_vendor=max_per_vendor,
        progress=progress,
    )
