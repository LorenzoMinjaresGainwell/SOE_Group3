from __future__ import annotations

import datetime as dt
from typing import Any

from services.state_contracts.keyword_context import useful_keyword_match
from services.state_normalization import (
    amount_string,
    clean_text,
    compact_raw_json,
    iso_date,
    keyword_hits,
    months_until,
    parse_date,
    stable_id,
    term_matches,
)


def normalize_awarded_record(
    row: dict[str, Any],
    *,
    state: str,
    source: str,
    source_key: str,
    source_note: str,
    source_url: str,
    vendor_terms: list[str],
    keywords: list[str],
    contract_number: Any,
    title: Any,
    vendor_name: Any = "",
    agency: Any = "",
    amount: Any = "",
    execution_date: Any = "",
    start_date: Any = "",
    end_date: Any = "",
    document_url: Any = "",
    document_type: str = "Contract Award",
    contract_record_type: str = "award",
    source_record_id: Any = "",
) -> dict[str, str]:
    number = clean_text(contract_number, 180)
    vendor = clean_text(vendor_name, 180)
    record_title = clean_text(title or number, 500)
    record_id = clean_text(source_record_id or number, 180)
    normalized_end, months, end_signal, expired = normalized_end_date_fields(end_date)
    if not record_id or not record_title or expired:
        return {}

    search_text = " ".join(clean_text(value, 2000) for value in [number, record_title, vendor, agency, document_type] if value)
    vendor_hits = keyword_hits(vendor, unique_terms(vendor_terms))
    matched = keyword_hits(search_text, keywords)
    if not vendor_hits and (not matched or not useful_keyword_match(matched, search_text)):
        return {}

    # A keyword-only hit is useful discovery, but a subject keyword is not a
    # vendor search term and must not be represented as one.
    query = vendor_hits[0] if vendor_hits else ""
    raw = {"source_key": source_key, "source_note": source_note, "row": row}
    return {
        "id": stable_id(state, record_id, vendor, prefix=f"{state.lower()}-contract"),
        "state": state,
        "source": source,
        "source_record_id": record_id,
        "parent_id": number or record_id,
        "contract_record_type": contract_record_type,
        "vendor_name": vendor,
        "vendor_query": query,
        "agency": clean_text(agency, 180),
        "contract_number": number,
        "title": record_title,
        "amount": amount_string(amount) or "0",
        "execution_date": iso_date(execution_date),
        "start_date": iso_date(start_date),
        "end_date": normalized_end,
        "months_to_end": "" if months is None else str(months),
        "recompete_signal": end_signal,
        "document_type": clean_text(document_type, 160),
        "document_url": clean_text(document_url or source_url, 500),
        "source_url": source_url,
        "matched_keywords": ";".join(matched),
        "relevance_score": str(relevance_score(vendor_hits, matched, months, search_text)),
        "raw_json": compact_raw_json(raw, limit=10000),
        "last_checked_at": now_iso(),
    }


def recompete_signal(months: int | None) -> str:
    if months is None:
        return "Unknown end date"
    if months < 0:
        return "Expired/past award"
    if months > 600:
        return "Open-ended/placeholder end date"
    if months <= 18:
        return "Expiring soon"
    if months <= 36:
        return "Recompete watch"
    return "Longer-term contract"


def relevance_score(vendor_hits: list[str], matches: list[str], months: int | None, text: str) -> int:
    score = min(45, len(matches) * 8) + (35 if vendor_hits else 0)
    if any(term_matches(text, term) for term in ["Medicaid", "MMIS", "Medicare", "managed care", "provider data"]):
        score += 25
    if months is not None and 0 <= months <= 18:
        score += 25
    elif months is not None and 0 <= months <= 36:
        score += 18
    return min(100, score)


def unique_terms(terms: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for term in terms:
        value = clean_text(term, 100)
        if value and value.lower() not in seen:
            seen.add(value.lower())
            result.append(value)
    return result


def normalized_end_date_fields(
    value: Any, *, today: dt.date | None = None
) -> tuple[str, int | None, str, bool]:
    parsed = parse_date(value)
    if not parsed:
        return "", None, "Unknown end date", False
    base = today or dt.date.today()
    months = months_until(parsed, today=base)
    if parsed < base:
        return parsed.isoformat(), months, "Expired/past award", True
    if parsed.year >= 2099 or (months is not None and months > 600):
        return "", None, "Open-ended/placeholder end date", False
    return parsed.isoformat(), months, recompete_signal(months), False


def record_sort_key(row: dict[str, str]) -> tuple[int, int, str]:
    # Explicitly rank real dates above blanks so placeholders cannot outrank a
    # real contract merely because their raw date was far in the future.
    end_date = row.get("end_date", "")
    return (int(row.get("relevance_score") or 0), 1 if end_date else 0, end_date)


def limit_records(records: list[dict[str, str]], max_per_vendor: int, vendor_terms: list[str]) -> list[dict[str, str]]:
    per_term_limit = max(1, max_per_vendor)
    known_terms = {term.lower() for term in unique_terms(vendor_terms)}
    unique = {record["id"]: record for record in records if record.get("id")}
    counts: dict[str, int] = {}
    selected: list[dict[str, str]] = []
    for record in sorted(unique.values(), key=record_sort_key, reverse=True):
        query = clean_text(record.get("vendor_query"), 100).lower()
        bucket = query if query in known_terms else "__keyword_only__"
        if counts.get(bucket, 0) >= per_term_limit:
            continue
        counts[bucket] = counts.get(bucket, 0) + 1
        selected.append(record)
    return selected


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
