from __future__ import annotations

from typing import Callable

from services.state_opportunities.peoplesoft import (
    InFlightPeopleSoftOpportunityConfig,
    fetch_inflight_peoplesoft_event_opportunities,
)

CONFIG = InFlightPeopleSoftOpportunityConfig(
    state="CA",
    source_name="Cal eProcure Public Events",
    source_key="ca_caleprocure",
    event_search_url="https://caleprocure.ca.gov/pages/Events-BS3/event-search.aspx",
    target_url="https://caleprocure.ca.gov/nlx3/psc/psfpd1/SUPPLIER/ERP/c/AUC_MANAGE_BIDS.AUC_RESP_INQ_AUC.GBL",
    source_note="Cal eProcure public InFlight NLX facade over PeopleSoft AUC_RESP_INQ_AUC event search.",
)


def fetch_opportunities(
    *,
    keywords: list[str],
    days_back: int,
    max_records: int,
    progress: Callable[[str], None] | None = None,
) -> list[dict[str, str]]:
    return fetch_inflight_peoplesoft_event_opportunities(
        config=CONFIG,
        keywords=keywords,
        days_back=days_back,
        max_records=max_records,
        progress=progress,
    )
