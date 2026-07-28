from __future__ import annotations

from typing import Callable

from services.state_contracts.classified import blocked_fetcher

SOURCE_URL = "https://ohiobuys.ohio.gov/"
BLOCKED_REASON = (
    "the official OhioBuys public contract routes redirect to an Ivalua browser-check "
    "with reCAPTCHA Enterprise, and bounded official alternate-route checks found no "
    "public JSON, CSV, or server-rendered current-contract feed; no browser-check bypass attempted"
)


def fetch_contracts(
    *,
    vendor_terms: list[str],
    keywords: list[str],
    max_per_vendor: int,
    progress: Callable[[str], None] | None = None,
) -> list[dict[str, str]]:
    """Report the verified official-source block without network access."""
    return blocked_fetcher(
        "OH", "current contracts", SOURCE_URL, BLOCKED_REASON, progress=progress
    )
