from __future__ import annotations

import importlib
from typing import Callable

from services.state_opportunities import (
    ak,
    al,
    ar,
    az,
    ca,
    co,
    dc,
    de,
    fl,
    ga,
    hi,
    id,
    ia,
    il,
    ks,
    ky,
    la,
    ma,
    md,
    me,
    mi,
    mo,
    ms,
    mt,
    nc,
    ne,
    nj,
    nm,
    nv,
    ny,
    ok,
    pa,
    pr,
    ri,
    sc,
    sd,
    tn,
    tx,
    ut,
    va,
    vi,
    vt,
    wa,
    wi,
    wv,
    wy,
)

indiana = importlib.import_module("services.state_opportunities.in")
oregon = importlib.import_module("services.state_opportunities.or")

STATE_CLIENTS = {
    "AK": ak.fetch_opportunities,
    "AL": al.fetch_opportunities,
    "AR": ar.fetch_opportunities,
    "AZ": az.fetch_opportunities,
    "CA": ca.fetch_opportunities,
    "CO": co.fetch_opportunities,
    "DC": dc.fetch_opportunities,
    "DE": de.fetch_opportunities,
    "FL": fl.fetch_opportunities,
    "GA": ga.fetch_opportunities,
    "HI": hi.fetch_opportunities,
    "ID": id.fetch_opportunities,
    "IA": ia.fetch_opportunities,
    "IL": il.fetch_opportunities,
    "IN": indiana.fetch_opportunities,
    "KS": ks.fetch_opportunities,
    "KY": ky.fetch_opportunities,
    "LA": la.fetch_opportunities,
    "MA": ma.fetch_opportunities,
    "MD": md.fetch_opportunities,
    "ME": me.fetch_opportunities,
    "MI": mi.fetch_opportunities,
    "MO": mo.fetch_opportunities,
    "MS": ms.fetch_opportunities,
    "MT": mt.fetch_opportunities,
    "NC": nc.fetch_opportunities,
    "NE": ne.fetch_opportunities,
    "NJ": nj.fetch_opportunities,
    "NM": nm.fetch_opportunities,
    "NV": nv.fetch_opportunities,
    "NY": ny.fetch_opportunities,
    "OK": ok.fetch_opportunities,
    "OR": oregon.fetch_opportunities,
    "PA": pa.fetch_opportunities,
    "PR": pr.fetch_opportunities,
    "RI": ri.fetch_opportunities,
    "SC": sc.fetch_opportunities,
    "SD": sd.fetch_opportunities,
    "TN": tn.fetch_opportunities,
    "TX": tx.fetch_opportunities,
    "UT": ut.fetch_opportunities,
    "VA": va.fetch_opportunities,
    "VI": vi.fetch_opportunities,
    "VT": vt.fetch_opportunities,
    "WA": wa.fetch_opportunities,
    "WI": wi.fetch_opportunities,
    "WV": wv.fetch_opportunities,
    "WY": wy.fetch_opportunities,
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
