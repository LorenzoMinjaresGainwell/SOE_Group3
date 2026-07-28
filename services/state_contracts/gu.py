from __future__ import annotations

from typing import Callable

from services.state_contracts.classified import blocked_fetcher

SOURCE_URL = "https://gsa.doa.guam.gov/invitation-for-bid/"
BLOCKED_REASON = (
    "the official Guam GSA WordPress listing mixes solicitations, amendments, cancellations, "
    "procurement stays, and award-style documents without structured award/current-contract "
    "fields; bounded probes found no stable public JSON, CSV, or unambiguous HTML contract feed"
)


def fetch_contracts(
    *,
    vendor_terms: list[str],
    keywords: list[str],
    max_per_vendor: int,
    progress: Callable[[str], None] | None = None,
) -> list[dict[str, str]]:
    """Report the verified lack of an unambiguous official contract feed without network access."""
    return blocked_fetcher(
        "GU", "current contracts", SOURCE_URL, BLOCKED_REASON, progress=progress
    )
