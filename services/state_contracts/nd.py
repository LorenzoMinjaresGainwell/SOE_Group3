from __future__ import annotations

from typing import Callable

SOURCE_URL = "https://public.ndbuys.nd.gov/page.aspx/en/ctr/contract_browse_public"
BLOCKED_REASON = (
    "the official NDBuys public contract-browse route redirected the low-rate probe to an "
    "enterprise reCAPTCHA browser-check page; no CAPTCHA or browser-check bypass attempted"
)


def fetch_contracts(
    *,
    vendor_terms: list[str],
    keywords: list[str],
    max_per_vendor: int,
    progress: Callable[[str], None] | None = None,
) -> list[dict[str, str]]:
    """Document the conclusive official-source block without issuing network requests."""
    if progress:
        progress(f"ND current contracts: skipped: {BLOCKED_REASON} ({SOURCE_URL})")
    return []
