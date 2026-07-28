from __future__ import annotations
from typing import Callable
from services.state_contracts.classified import blocked_fetcher

SOURCE_URL = "https://purchasing.ri.gov/"
BLOCKED_REASON = "official purchasing pages did not expose a verified stable public post-award API, CSV, or simple HTML contract feed containing vendor and term; RIVIP bid/event listings are excluded"

def fetch_contracts(*, vendor_terms: list[str], keywords: list[str], max_per_vendor: int, progress: Callable[[str], None] | None = None) -> list[dict[str, str]]:
    return blocked_fetcher("RI", "purchasing contracts", SOURCE_URL, BLOCKED_REASON, progress=progress)
