from __future__ import annotations

from typing import Callable

from services.state_contracts.vss import VssContractConfig, fetch_vss_awarded_contracts

AK_IRIS_VSS_CONTRACT_CONFIG = VssContractConfig(
    state="AK",
    source_name="Alaska IRIS VSS Awarded Solicitations",
    source_key="ak_iris_vss",
    base_url="https://iris-vss.alaska.gov/PRDVSS1X1/Advantage4",
    source_note=(
        "Public Alaska IRIS/VSS guest flow through Published Solicitations. The public carousel "
        "does not expose Award History, so records come from awarded solicitation detail JSON."
    ),
)


def fetch_contracts(
    *,
    vendor_terms: list[str],
    keywords: list[str],
    max_per_vendor: int,
    progress: Callable[[str], None] | None = None,
) -> list[dict[str, str]]:
    try:
        return fetch_vss_awarded_contracts(
            config=AK_IRIS_VSS_CONTRACT_CONFIG,
            vendor_terms=vendor_terms,
            keywords=keywords,
            max_per_vendor=max_per_vendor,
            progress=progress,
        )
    except RuntimeError as exc:
        if progress:
            progress(f"AK VSS awarded solicitations: skipped after {exc}")
        return []
