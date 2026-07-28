from __future__ import annotations
from typing import Callable
from services.state_contracts.classified import blocked_fetcher

SOURCE_URL = "https://dpp.vi.gov/"
BLOCKED_REASON = "official Department of Property and Procurement pages did not expose a verified stable public awarded/current-contract API, CSV, or simple HTML feed containing vendor and contract term; bid notices are excluded"

def fetch_contracts(*, vendor_terms: list[str], keywords: list[str], max_per_vendor: int, progress: Callable[[str], None] | None = None) -> list[dict[str, str]]:
    return blocked_fetcher("VI", "DPP contracts", SOURCE_URL, BLOCKED_REASON, progress=progress)
