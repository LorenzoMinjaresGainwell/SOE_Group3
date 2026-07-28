from __future__ import annotations

import csv
import io
from typing import Callable

from services.state_updates import sort_key, state_update_record
from services.state_updates.common import clean_text, fetch_text, is_procurement_update, iso_date_text, unique_records

AGENCY = "Commonwealth of the Northern Mariana Islands Medicaid Agency"
LISTING_URL = "https://www.cnmimedicaid.org/medicaid-information-center/state-plan-amendments-spa"
CSV_URL = "https://docs.google.com/spreadsheets/d/11FTqwNpRK3syfuMkBTKbeQF2ZZlHqAzzvXCx6XnYse0/export?format=csv"


def source_rows(content: str) -> list[dict[str, str]]:
    rows = list(csv.reader(io.StringIO(content)))
    header_index = next((i for i, row in enumerate(rows) if row and clean_text(row[0]).lower() == "transmittal number"), -1)
    if header_index < 0:
        return []
    output = []
    for values in rows[header_index + 1:]:
        padded = values + [""] * (6 - len(values))
        transmittal, approved, effective, topics, summary, _links = (clean_text(value) for value in padded[:6])
        if not transmittal or not iso_date_text(approved) or is_procurement_update(topics, summary):
            continue
        output.append({"transmittal": transmittal, "approved": iso_date_text(approved),
                       "effective": iso_date_text(effective), "topics": topics, "summary": summary})
    return output


def fetch_updates(*, keywords: list[str], max_records: int, progress: Callable[[str], None] | None = None) -> list[dict[str, str]]:
    if max_records <= 0:
        return []
    try:
        rows = source_rows(fetch_text(CSV_URL, timeout=20, byte_limit=500_000))
    except Exception as exc:
        emit(progress, f"MP CNMI SPA listing unavailable: {exc}")
        return []
    records = [state_update_record(
        state="MP", source="mp_cnmi_spa_listing", source_record_id=row["transmittal"], record_type="spa_notice",
        title=f"CNMI Medicaid State Plan Amendment {row['transmittal']}", agency=AGENCY,
        summary=" — ".join(value for value in (row["topics"], row["summary"]) if value),
        posted_date=row["approved"], effective_date=row["effective"], document_url=LISTING_URL,
        source_url=LISTING_URL, keywords=keywords,
        raw={"official_csv": CSV_URL, "transmittal_number": row["transmittal"], "procurement_excluded": True},
    ) for row in rows]
    output = sorted(unique_records(records), key=sort_key, reverse=True)
    emit(progress, f"MP: normalized {len(output)} CNMI SPA rows")
    return output[:max_records]


def emit(progress: Callable[[str], None] | None, message: str) -> None:
    if progress:
        progress(message)
