from __future__ import annotations
from typing import Callable
from services.state_contracts.classified import blocked_fetcher

SOURCE_URL = "https://www.dfa.ms.gov/"
BLOCKED_REASON = "the former official contract/bid search path is no longer a contract result feed, and no replacement stable public post-award API, CSV, or simple HTML source with vendor and term was verified; bids are excluded"

def fetch_contracts(*, vendor_terms: list[str], keywords: list[str], max_per_vendor: int, progress: Callable[[str], None] | None = None) -> list[dict[str, str]]:
    return blocked_fetcher("MS", "DFA contract search", SOURCE_URL, BLOCKED_REASON, progress=progress)
