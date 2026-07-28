from __future__ import annotations
from typing import Callable
from services.state_contracts.classified import blocked_fetcher

SOURCE_URL = "https://hands.ehawaii.gov/hands/awards"
BLOCKED_REASON = "official HANDS awards are an Angular application; no stable public award API, CSV, or simple HTML result containing awardee and contract term was verified"

def fetch_contracts(*, vendor_terms: list[str], keywords: list[str], max_per_vendor: int, progress: Callable[[str], None] | None = None) -> list[dict[str, str]]:
    return blocked_fetcher("HI", "HANDS awards", SOURCE_URL, BLOCKED_REASON, progress=progress)
