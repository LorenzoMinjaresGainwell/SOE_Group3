from __future__ import annotations

from typing import Callable

SOURCE_URL = "https://www.americansamoa.gov/notices"
BLOCKED_REASON = (
    "the bounded official ASG notices and sitemap probes returned public content but no Medicaid, "
    "state-plan, waiver, or provider-update source, and the medicaid.as.gov host was unavailable; "
    "no stable public JSON, CSV, or simple HTML Medicaid update feed was identified"
)


def fetch_updates(
    *,
    keywords: list[str],
    max_records: int,
    progress: Callable[[str], None] | None = None,
) -> list[dict[str, str]]:
    """Report verified official-source unavailability without network access."""
    if progress:
        progress(f"AS Medicaid updates: skipped: {BLOCKED_REASON} ({SOURCE_URL})")
    return []
