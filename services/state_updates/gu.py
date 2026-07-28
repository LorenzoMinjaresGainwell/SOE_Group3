from __future__ import annotations

from typing import Callable

SOURCE_URL = "https://medicaid.guam.gov/"
BLOCKED_REASON = (
    "the bounded official Guam Medicaid and public-health hosts were unavailable, the legacy "
    "DPHSS route did not provide a Medicaid source, and the official Guam content API returned "
    "no Medicaid records; no stable public JSON, CSV, or simple HTML update feed was identified"
)


def fetch_updates(
    *,
    keywords: list[str],
    max_records: int,
    progress: Callable[[str], None] | None = None,
) -> list[dict[str, str]]:
    """Report verified official-source unavailability without network access."""
    if progress:
        progress(f"GU Medicaid updates: skipped: {BLOCKED_REASON} ({SOURCE_URL})")
    return []
