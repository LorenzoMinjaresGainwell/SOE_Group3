from __future__ import annotations
from typing import Callable

SOURCE_URL = "https://www.sunshineportalnm.com/"
BLOCKED_REASON = "official New Mexico Sunshine Portal rejected conservative public probes (TLS handshake failure on HTTPS and HTTP 403), so no contract feed can be collected without bypass behavior"


def fetch_contracts(*, vendor_terms: list[str], keywords: list[str], max_per_vendor: int, progress: Callable[[str], None] | None = None) -> list[dict[str, str]]:
    if progress:
        progress(f"NM contracts: skipped: {BLOCKED_REASON} ({SOURCE_URL})")
    return []
