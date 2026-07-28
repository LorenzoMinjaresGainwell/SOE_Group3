from __future__ import annotations
from typing import Callable

SOURCE_URL = "https://admin.ks.gov/offices/procurement-contracts/bidding--contracts/contracts/contract-search"
BLOCKED_REASON = "official eSupplier contract search redirected the low-rate public probe to an Oracle login/cookie-check page; bid solicitations are intentionally excluded"

def fetch_contracts(*, vendor_terms: list[str], keywords: list[str], max_per_vendor: int, progress: Callable[[str], None] | None = None) -> list[dict[str, str]]:
    if progress: progress(f"KS awarded contracts: skipped: {BLOCKED_REASON} ({SOURCE_URL})")
    return []
