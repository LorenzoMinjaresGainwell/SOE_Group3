from __future__ import annotations

from typing import Callable

SOURCE_URLS = (
    "https://www.forwardhealth.wi.gov/WIPortal/content/Provider/forwardhealth_update/2026.htm.spage",
    "https://www.forwardhealth.wi.gov/WIPortal/content/Provider/forwardhealth_update/2025.htm.spage",
)
BLOCKED_REASON = (
    "the bounded official ForwardHealth year-page probes return public shells that explicitly "
    "say the requested provider-update resource is not currently available and expose no update "
    "rows; no stable public JSON, CSV, or simple HTML update feed was identified"
)


def fetch_updates(
    *,
    keywords: list[str],
    max_records: int,
    progress: Callable[[str], None] | None = None,
) -> list[dict[str, str]]:
    """Report verified official-route unavailability without network access."""
    if progress:
        progress(f"WI ForwardHealth updates: skipped: {BLOCKED_REASON} ({', '.join(SOURCE_URLS)})")
    return []
