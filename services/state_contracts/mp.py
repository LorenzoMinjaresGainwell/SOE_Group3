from __future__ import annotations

from typing import Callable

from services.state_contracts.classified import blocked_fetcher

SOURCE_URL = "https://www.finance.gov.mp/procurement-services.php"
BLOCKED_REASON = (
    "the official CNMI Finance page links contract/procurement systems whose public routes "
    "redirect to an upgrade page or authenticated Tyler Portico sign-in, while the separate "
    "RFP host was unavailable; no stable public JSON, CSV, or current-contract HTML feed was found"
)


def fetch_contracts(
    *,
    vendor_terms: list[str],
    keywords: list[str],
    max_per_vendor: int,
    progress: Callable[[str], None] | None = None,
) -> list[dict[str, str]]:
    """Report the verified official-source unavailability without network access."""
    return blocked_fetcher(
        "MP", "current contracts", SOURCE_URL, BLOCKED_REASON, progress=progress
    )
