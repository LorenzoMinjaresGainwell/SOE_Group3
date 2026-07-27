from __future__ import annotations

from typing import Callable

from services.state_contracts.vss import VssContractConfig, fetch_vss_awarded_contracts

MI_SIGMA_VSS_CONTRACT_CONFIG = VssContractConfig(
    state="MI",
    source_name="Michigan SIGMA VSS Awarded Solicitations",
    source_key="mi_sigma_vss",
    base_url="https://sigma.michigan.gov/PRDVSS1X1/Advantage4",
)


def fetch_contracts(
    *,
    vendor_terms: list[str],
    keywords: list[str],
    max_per_vendor: int,
    progress: Callable[[str], None] | None = None,
) -> list[dict[str, str]]:
    return fetch_vss_awarded_contracts(
        config=MI_SIGMA_VSS_CONTRACT_CONFIG,
        vendor_terms=vendor_terms,
        keywords=keywords,
        max_per_vendor=max_per_vendor,
        progress=progress,
    )
