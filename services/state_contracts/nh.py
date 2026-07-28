from __future__ import annotations
from typing import Callable
from services.state_contracts.classified import blocked_fetcher

SOURCE_URL = "https://apps.das.nh.gov/bidscontracts/contracts.aspx"
BLOCKED_REASON = "official contracts search denied the conservative public probe and uses an ASP.NET postback UI; no stable public API, CSV, or simple HTML feed with vendor and term was verified, and access controls are not bypassed"

def fetch_contracts(*, vendor_terms: list[str], keywords: list[str], max_per_vendor: int, progress: Callable[[str], None] | None = None) -> list[dict[str, str]]:
    return blocked_fetcher("NH", "DAS contracts", SOURCE_URL, BLOCKED_REASON, progress=progress)
