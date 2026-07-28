from __future__ import annotations
from typing import Callable
from services.state_contracts.classified import blocked_fetcher

SOURCE_URL = "https://www.maine.gov/dafs/bbm/procurementservices"
BLOCKED_REASON = "official procurement pages did not expose a stable public awarded/current-contract API, CSV, or simple HTML feed with vendor and contract term; bid listings are excluded"

def fetch_contracts(*, vendor_terms: list[str], keywords: list[str], max_per_vendor: int, progress: Callable[[str], None] | None = None) -> list[dict[str, str]]:
    return blocked_fetcher("ME", "procurement contracts", SOURCE_URL, BLOCKED_REASON, progress=progress)
