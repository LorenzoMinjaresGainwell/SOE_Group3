from __future__ import annotations

from typing import Callable

SOURCE_URLS = (
    "https://medicaid.ohio.gov/resources-for-providers/billing/provider-bulletins",
    "https://medicaid.ohio.gov/about-us/medicaid-state-plan",
)
BLOCKED_REASON = (
    "both bounded official ODM provider-bulletin and Medicaid-state-plan probes returned "
    "the same 404 page with no update/document links, and no stable public JSON, CSV, or "
    "simple HTML update feed was identified"
)


def fetch_updates(
    *,
    keywords: list[str],
    max_records: int,
    progress: Callable[[str], None] | None = None,
) -> list[dict[str, str]]:
    """Report verified official-route unavailability without network access."""
    if progress:
        progress(f"OH Medicaid updates: skipped: {BLOCKED_REASON} ({', '.join(SOURCE_URLS)})")
    return []
