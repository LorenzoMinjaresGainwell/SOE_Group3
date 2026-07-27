from __future__ import annotations

from typing import Callable

from services.state_opportunities.jaggaer import JaggaerPublicEventConfig, fetch_jaggaer_public_event_opportunities

CONFIG = JaggaerPublicEventConfig(
    state="MT",
    source_name="Montana eMACS JAGGAER PublicEvent",
    source_key="mt_emacs",
    public_event_url="https://bids.sciquest.com/apps/Router/PublicEvent?CustomerOrg=StateOfMontana",
)


def fetch_opportunities(
    *,
    keywords: list[str],
    days_back: int,
    max_records: int,
    progress: Callable[[str], None] | None = None,
) -> list[dict[str, str]]:
    return fetch_jaggaer_public_event_opportunities(
        config=CONFIG,
        keywords=keywords,
        days_back=days_back,
        max_records=max_records,
        progress=progress,
    )
