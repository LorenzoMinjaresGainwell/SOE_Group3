from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from services.state_contracts import pa, tx

STATE_CLIENTS = {
    "PA": pa.fetch_contracts,
    "TX": tx.fetch_contracts,
}


def fetch_state_contracts(
    *,
    states: list[str],
    vendor_terms: list[str],
    keywords: list[str],
    max_per_vendor: int,
    progress: Callable[[str], None] | None = None,
) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    for state in states:
        state_code = state.strip().upper()
        fetcher = STATE_CLIENTS.get(state_code)
        if not fetcher:
            emit(progress, f"{state_code}: no state adapter yet")
            continue
        emit(progress, f"{state_code}: searching state contracts")
        records.extend(
            fetcher(
                vendor_terms=vendor_terms,
                keywords=keywords,
                max_per_vendor=max_per_vendor,
                progress=progress,
            )
        )
    return records


def emit(progress: Callable[[str], None] | None, message: str) -> None:
    if progress:
        progress(message)
