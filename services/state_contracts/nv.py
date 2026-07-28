from __future__ import annotations
from typing import Callable

SOURCE_URL = "https://www.purchasing.nv.gov/statewide-contracts/"
BLOCKED_REASON = "official Nevada statewide-contract index is reachable, but vendor and contract-period fields exist only on many separate detail pages; no stable public JSON, CSV, or bounded vendor/end-date index was verified"


def fetch_contracts(*, vendor_terms: list[str], keywords: list[str], max_per_vendor: int, progress: Callable[[str], None] | None = None) -> list[dict[str, str]]:
    if progress:
        progress(f"NV contracts: skipped: {BLOCKED_REASON} ({SOURCE_URL})")
    return []
