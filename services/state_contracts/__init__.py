from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any, Callable

from services.state_contracts import ak, al, ar, az, ca, co, fl, il, ma, mi, nj, pa, pr, tn, tx, vt, wy

or_contracts = importlib.import_module("services.state_contracts.or")
_NEW_STATE_MODULES = {
    state: importlib.import_module(f"services.state_contracts.{module}")
    for state, module in {
        "DC": "dc",
        "DE": "de",
        "GA": "ga",
        "IA": "ia",
        "ID": "id",
        "IN": "in",
        "LA": "la",
        "MD": "md",
        "MO": "mo",
        "NC": "nc",
        "NY": "ny",
        "OK": "ok",
        "UT": "ut",
        "VA": "va",
        "WA": "wa",
        "WV": "wv",
    }.items()
}

STATE_CLIENTS = {
    "AK": ak.fetch_contracts,
    "AL": al.fetch_contracts,
    "AR": ar.fetch_contracts,
    "AZ": az.fetch_contracts,
    "CA": ca.fetch_contracts,
    "CO": co.fetch_contracts,
    "FL": fl.fetch_contracts,
    "IL": il.fetch_contracts,
    "MA": ma.fetch_contracts,
    "MI": mi.fetch_contracts,
    "NJ": nj.fetch_contracts,
    "OR": or_contracts.fetch_contracts,
    "PA": pa.fetch_contracts,
    "PR": pr.fetch_contracts,
    "TN": tn.fetch_contracts,
    "TX": tx.fetch_contracts,
    "VT": vt.fetch_contracts,
    "WY": wy.fetch_contracts,
    **{state: module.fetch_contracts for state, module in _NEW_STATE_MODULES.items()},
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
