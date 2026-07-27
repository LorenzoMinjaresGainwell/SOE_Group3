from __future__ import annotations

from typing import Callable

from services.state_opportunities.vss import VssOpportunityConfig, fetch_vss_published_solicitations

CO_VSS_CONFIG = VssOpportunityConfig(
    state="CO",
    source_name="ColoradoVSS Published Solicitations",
    source_key="co_vss",
    base_url="https://prd.co.cgiadvantage.com/PRDVSS1X1/Advantage4",
    source_note=(
        "Official Colorado OSC/SPCO Solicitations page links this ColoradoVSS public route; "
        "prior codpa-vss.cloud.cgifederal.com host is DNS-dead."
    ),
)


def fetch_opportunities(
    *,
    keywords: list[str],
    days_back: int,
    max_records: int,
    progress: Callable[[str], None] | None = None,
) -> list[dict[str, str]]:
    return fetch_vss_published_solicitations(
        config=CO_VSS_CONFIG,
        keywords=keywords,
        days_back=days_back,
        max_records=max_records,
        progress=progress,
    )
