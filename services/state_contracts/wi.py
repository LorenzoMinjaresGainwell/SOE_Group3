from __future__ import annotations

from typing import Callable

from services.state_contracts.classified import blocked_fetcher

SOURCE_URL = "https://vendornet.wi.gov/Contracts.aspx"
BLOCKED_REASON = (
    "the official VendorNet Contracts page returns only a Telerik/ASP.NET AJAX shell: "
    "bounded probes found no server-rendered contract rows or stable public JSON, CSV, "
    "or export feed; interactive browser automation is outside this collector's scope"
)


def fetch_contracts(
    *,
    vendor_terms: list[str],
    keywords: list[str],
    max_per_vendor: int,
    progress: Callable[[str], None] | None = None,
) -> list[dict[str, str]]:
    """Report the verified lack of a lightweight official feed without network access."""
    return blocked_fetcher(
        "WI", "current contracts", SOURCE_URL, BLOCKED_REASON, progress=progress
    )
