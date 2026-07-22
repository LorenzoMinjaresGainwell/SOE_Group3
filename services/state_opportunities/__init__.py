from __future__ import annotations

from typing import Callable

from services.state_opportunities import pa, tx

STATE_CLIENTS = {
    "PA": pa.fetch_opportunities,
    "TX": tx.fetch_opportunities,
}


def fetch_state_opportunities(
    *,
    states: list[str],
    keywords: list[str],
    days_back: int,
    max_records: int,
    progress: Callable[[str], None] | None = None,
) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    for state in states:
        state_code = state.strip().upper()
        fetcher = STATE_CLIENTS.get(state_code)
        if not fetcher:
            emit(progress, f"{state_code}: no state opportunity adapter yet")
            continue
        emit(progress, f"{state_code}: searching state opportunities")
        records.extend(
            fetcher(
                keywords=keywords,
                days_back=days_back,
                max_records=max_records,
                progress=progress,
            )
        )
    return sorted(records, key=sort_key, reverse=True)[:max_records]


def sort_key(row: dict[str, str]) -> tuple[int, str, str]:
    return (int_or_zero(row.get("relevance_score")), row.get("due_date", ""), row.get("posted_date", ""))


def int_or_zero(value: str | None) -> int:
    try:
        return int(float(value or 0))
    except ValueError:
        return 0


def emit(progress: Callable[[str], None] | None, message: str) -> None:
    if progress:
        progress(message)
