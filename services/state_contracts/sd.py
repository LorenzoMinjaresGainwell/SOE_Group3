from __future__ import annotations
from typing import Callable
from services.state_contracts.classified import blocked_fetcher

SOURCE_URL = "https://open.sd.gov/contracts.aspx"
BLOCKED_REASON = "official Open SD contract/grant search is a stateful ASP.NET postback page and no stable public API, CSV, or simple HTML result feed with current/awarded status and contract term was verified"

def fetch_contracts(*, vendor_terms: list[str], keywords: list[str], max_per_vendor: int, progress: Callable[[str], None] | None = None) -> list[dict[str, str]]:
    return blocked_fetcher("SD", "Open SD contracts", SOURCE_URL, BLOCKED_REASON, progress=progress)
