from __future__ import annotations

from typing import Callable

from services.state_contracts.classified import blocked_fetcher

SOURCE_URL = "https://www.americansamoa.gov/"
BLOCKED_REASON = (
    "the official ASG homepage, notices page, robots file, and Wix sitemaps are public, "
    "but bounded same-domain discovery found no contract/award listing or stable public "
    "JSON, CSV, or server-rendered current-contract feed"
)


def fetch_contracts(
    *,
    vendor_terms: list[str],
    keywords: list[str],
    max_per_vendor: int,
    progress: Callable[[str], None] | None = None,
) -> list[dict[str, str]]:
    """Report the verified absence of a discoverable official feed without network access."""
    return blocked_fetcher(
        "AS", "current contracts", SOURCE_URL, BLOCKED_REASON, progress=progress
    )
