from __future__ import annotations

import datetime as dt
import re
from typing import Callable

from services.state_updates import sort_key, state_update_record
from services.state_updates.common import clean_text, fetch_text, iso_date_text, parse_links, record_type_for, source_id_from_url, unique_records

HOME_URL = "https://www.wymedicaid.org/content/wymedicaid/en"
PDL_URL = "https://www.wymedicaid.org/content/wymedicaid/en/pa-pdl/preferred-drug-lists"
PROVIDER_MANUAL_URL = "https://www.wymedicaid.org/content/wymedicaid/en/provider/provider-manual"

WY_CONTEXT_TERMS = [
    "wyoming medicaid",
    "medicaid",
    "provider",
    "pharmacy",
    "prior authorization",
    "claims",
    "enrollment",
    "pdl",
    "preferred drug list",
    "portal",
]

NEWSLETTER_TERMS = ["medicaid-newsletter", "newsletter", "pharmacy portal", "cutover information", "revalidation"]
PDL_TERMS = ["pdl", "preferred drug", "drug list"]
MANUAL_TERMS = ["provider manual", "pharmacy services manual"]


def fetch_updates(
    *,
    keywords: list[str],
    max_records: int,
    progress: Callable[[str], None] | None = None,
) -> list[dict[str, str]]:
    limit = max(1, max_records)
    records: list[dict[str, str]] = []
    for fetcher in (fetch_newsletter_records, fetch_pdl_records, fetch_provider_manual_records):
        try:
            batch, scanned = fetcher(keywords=keywords)
        except Exception as exc:
            emit(progress, f"WY: {fetcher.__name__} failed: {exc}")
            continue
        records.extend(batch)
        emit(progress, f"WY {fetcher.__name__}: scanned {scanned} links, normalized {len(batch)} records")

    output = unique_records(records)
    emit(progress, f"WY: normalized {len(output)} records")
    return sorted(output, key=sort_key, reverse=True)[:limit]


def fetch_newsletter_records(*, keywords: list[str]) -> tuple[list[dict[str, str]], int]:
    rows = dated_portal_links(HOME_URL, title_terms=NEWSLETTER_TERMS)
    records = []
    for row in rows:
        text = " ".join([row["title"], row["url"], "Wyoming Medicaid provider newsletter"])
        if not has_keyword_or_context(text, keywords, WY_CONTEXT_TERMS):
            continue
        records.append(
            state_update_record(
                state="WY",
                source="wy_medicaid_provider_newsletters",
                source_record_id=source_id_from_url(row["url"]) or row["title"],
                record_type=record_type_for(text, "provider_bulletin"),
                title=row["title"],
                agency="Wyoming Medicaid",
                summary="Official Wyoming Medicaid provider newsletter or portal update document.",
                posted_date=row["date"],
                document_url=row["url"],
                source_url=HOME_URL,
                keywords=keywords,
                raw={"source_page": HOME_URL, "source_note": "Official Wyoming Medicaid public provider portal; WDH health.wyo.gov pages were Cloudflare-blocked from CLI probes."},
            )
        )
    return records, len(rows)


def fetch_pdl_records(*, keywords: list[str]) -> tuple[list[dict[str, str]], int]:
    rows = dated_portal_links(PDL_URL, title_terms=PDL_TERMS)
    records = []
    for row in rows:
        text = " ".join([row["title"], row["url"], "Wyoming Medicaid pharmacy preferred drug list prior authorization"])
        if not has_keyword_or_context(text, keywords, WY_CONTEXT_TERMS):
            continue
        records.append(
            state_update_record(
                state="WY",
                source="wy_medicaid_pdl_updates",
                source_record_id=source_id_from_url(row["url"]) or row["title"],
                record_type="guidance",
                title=row["title"],
                agency="Wyoming Medicaid",
                summary="Official Wyoming Medicaid preferred drug list update.",
                effective_date=row["date"] if "effective" in text.lower() else "",
                posted_date="" if "effective" in text.lower() else row["date"],
                document_url=row["url"],
                source_url=PDL_URL,
                keywords=keywords,
                raw={"source_page": PDL_URL},
            )
        )
    return records, len(rows)


def fetch_provider_manual_records(*, keywords: list[str]) -> tuple[list[dict[str, str]], int]:
    rows = dated_portal_links(PROVIDER_MANUAL_URL, title_terms=MANUAL_TERMS)
    records = []
    for row in rows:
        text = " ".join([row["title"], row["url"], "Wyoming Medicaid pharmacy services provider manual"])
        if not has_keyword_or_context(text, keywords, WY_CONTEXT_TERMS):
            continue
        records.append(
            state_update_record(
                state="WY",
                source="wy_medicaid_provider_manual",
                source_record_id=source_id_from_url(row["url"]) or row["title"],
                record_type="guidance",
                title=row["title"],
                agency="Wyoming Medicaid",
                summary="Official Wyoming Medicaid provider/pharmacy services manual update.",
                effective_date=row["date"] if "effective" in text.lower() else "",
                posted_date="" if "effective" in text.lower() else row["date"],
                document_url=row["url"],
                source_url=PROVIDER_MANUAL_URL,
                keywords=keywords,
                raw={"source_page": PROVIDER_MANUAL_URL},
            )
        )
    return records, len(rows)


def dated_portal_links(base_url: str, *, title_terms: list[str]) -> list[dict[str, str]]:
    markup = fetch_text(base_url, timeout=30, byte_limit=1_000_000)
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    for link in parse_links(markup, base_url):
        title = clean_text(link.text)
        url = clean_text(link.href)
        if not title or not url or url in seen:
            continue
        seen.add(url)
        lower = " ".join([title, url]).lower()
        if "www.wymedicaid.org/content/dam/" not in lower or not any(term in lower for term in title_terms):
            continue
        date = date_from_text(" ".join([title, url]))
        if not date or not is_current_period(date):
            continue
        rows.append({"title": title, "url": url, "date": date})
    return rows


def has_keyword_or_context(text: str, keywords: list[str], context_terms: list[str]) -> bool:
    lower = clean_text(text).lower()
    if any(str(keyword).strip().lower() in lower for keyword in keywords if str(keyword).strip()):
        return True
    return any(term in lower for term in context_terms)


def is_current_period(date_text: str) -> bool:
    return date_text.startswith("2026") or date_text.startswith("2025")


def date_from_text(value: str) -> str:
    text = clean_text(value)
    parsed = iso_date_text(text)
    if parsed:
        return parsed
    match = re.search(r"\b(\d{1,2})[-._/](\d{1,2})[-._/](20\d{2}|\d{2})\b", text)
    if match:
        year = int(match.group(3))
        if year < 100:
            year += 2000
        return safe_date(year, int(match.group(1)), int(match.group(2)))
    match = re.search(r"\b(20\d{2})[-_/]?(\d{2})[-_/]?(\d{2})\b", text)
    if match:
        return safe_date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    match = re.search(r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+(20\d{2})\b", text, re.I)
    if match:
        return safe_date(int(match.group(2)), month_number(match.group(1)), 1)
    match = re.search(r"(?:^|[-_/])(\d{2})(\d{2})(\d{2})(?:[-_.]|$)", text)
    if match:
        return safe_date(2000 + int(match.group(3)), int(match.group(1)), int(match.group(2)))
    return ""


def safe_date(year: int, month: int, day: int) -> str:
    try:
        return dt.date(year, month, day).isoformat()
    except ValueError:
        return ""


def month_number(value: str) -> int:
    key = value.lower()[:3]
    return {
        "jan": 1,
        "feb": 2,
        "mar": 3,
        "apr": 4,
        "may": 5,
        "jun": 6,
        "jul": 7,
        "aug": 8,
        "sep": 9,
        "oct": 10,
        "nov": 11,
        "dec": 12,
    }.get(key, 1)


def emit(progress: Callable[[str], None] | None, message: str) -> None:
    if progress:
        progress(message)
