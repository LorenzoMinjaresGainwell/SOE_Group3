from __future__ import annotations
from typing import Callable
from services.state_contracts.classified import blocked_fetcher

SOURCE_URL = "https://procurement.sc.gov/contracts"
BLOCKED_REASON = "official statewide-term-contract HTML exposes descriptions and expiration dates but not awarded vendors; its linked legacy contract search is not a verified stable API, CSV, or simple HTML vendor-and-term feed"

def fetch_contracts(*, vendor_terms: list[str], keywords: list[str], max_per_vendor: int, progress: Callable[[str], None] | None = None) -> list[dict[str, str]]:
    return blocked_fetcher("SC", "statewide term contracts", SOURCE_URL, BLOCKED_REASON, progress=progress)
