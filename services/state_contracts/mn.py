from __future__ import annotations
from typing import Callable

SOURCE_URL = "https://mn.gov/admin/osp/government/contracts/"
BLOCKED_REASON = "official current-contract page returned a Radware Bot Manager CAPTCHA to the low-rate probe; no bypass attempted"

def fetch_contracts(*, vendor_terms: list[str], keywords: list[str], max_per_vendor: int, progress: Callable[[str], None] | None = None) -> list[dict[str, str]]:
    if progress: progress(f"MN current contracts: skipped: {BLOCKED_REASON} ({SOURCE_URL})")
    return []
