from __future__ import annotations

from typing import Callable

from services.state_contracts.al_static_pdf import fetch_contracts as fetch_static_pdf_contracts


def fetch_contracts(
    *,
    vendor_terms: list[str],
    keywords: list[str],
    max_per_vendor: int,
    progress: Callable[[str], None] | None = None,
) -> list[dict[str, str]]:
    return fetch_static_pdf_contracts(
        vendor_terms=vendor_terms,
        keywords=keywords,
        max_per_vendor=max_per_vendor,
        progress=progress,
    )
