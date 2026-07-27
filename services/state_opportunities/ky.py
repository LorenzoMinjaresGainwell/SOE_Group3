from __future__ import annotations

from typing import Callable

from services.state_opportunities.vss import VssOpportunityConfig, fetch_vss_published_solicitations

KY_VSS_CONFIG = VssOpportunityConfig(
    state="KY",
    source_name="Kentucky VSS Published Solicitations",
    source_key="ky_vss",
    base_url="https://vss.ky.gov/vssprod-ext/Advantage4",
    source_note=(
        "Official Kentucky VSS replacement host vss.ky.gov; prior emars.ky.gov/online/vss/ "
        "resets CLI HTTP/TLS connections."
    ),
)


def fetch_opportunities(
    *,
    keywords: list[str],
    days_back: int,
    max_records: int,
    progress: Callable[[str], None] | None = None,
) -> list[dict[str, str]]:
    records = fetch_vss_published_solicitations(
        config=KY_VSS_CONFIG,
        keywords=keywords,
        days_back=days_back,
        max_records=max_records,
        progress=progress,
    )
    return [record for record in records if not category_code_hms_only(record)]


def category_code_hms_only(record: dict[str, str]) -> bool:
    if record.get("matched_keywords") != "HMS":
        return False
    visible_text = " ".join([record.get("title", ""), record.get("agency", ""), record.get("document_type", "")]).lower()
    return '"SO_CAT_CD":"HMS"' in record.get("raw_json", "") and "hms" not in visible_text
