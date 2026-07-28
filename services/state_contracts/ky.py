from __future__ import annotations
from typing import Callable

SOURCE_URL = "https://transparency.ky.gov/search/Pages/contractsearch.aspx"
BLOCKED_REASON = "official Kentucky contract search is reachable, but it is a client-rendered SharePoint application and no stable public JSON, CSV, or simple vendor/end-date HTML feed was verified"


def fetch_contracts(*, vendor_terms: list[str], keywords: list[str], max_per_vendor: int, progress: Callable[[str], None] | None = None) -> list[dict[str, str]]:
    if progress:
        progress(f"KY contracts: skipped: {BLOCKED_REASON} ({SOURCE_URL})")
    return []
