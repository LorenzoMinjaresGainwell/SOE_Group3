from __future__ import annotations

from typing import Callable

SOURCE_URL = "https://biznet.ct.gov/SCP_Search/ContractSearch.aspx"
BLOCKED_REASON = (
    "the official Connecticut State Contracting Portal contract search returned its protected "
    "ASP.NET error/access-control route to the low-rate probe, and the official CT Open Data "
    "catalog exposes no awarded/current contract dataset; no protection bypass attempted"
)


def fetch_contracts(
    *,
    vendor_terms: list[str],
    keywords: list[str],
    max_per_vendor: int,
    progress: Callable[[str], None] | None = None,
) -> list[dict[str, str]]:
    """Document the conclusive official-source block without issuing network requests."""
    if progress:
        progress(f"CT current contracts: skipped: {BLOCKED_REASON} ({SOURCE_URL})")
    return []
