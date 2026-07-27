from __future__ import annotations

import datetime as dt
import re
import urllib.parse
from typing import Callable

from services.state_http import fetch_url
from services.state_updates import sort_key, state_update_record
from services.state_updates.common import clean_text, iso_date_text, parse_links, record_type_for, source_id_from_url, unique_records

BULLETINS_URL = "https://hcpf.colorado.gov/bulletins"
PROVIDER_NEWS_URL = "https://hcpf.colorado.gov/provider-news"
NEWSROOM_URL = "https://hcpf.colorado.gov/newsroom"
RHTP_URL = "https://hcpf.colorado.gov/rural-health-transformation-program"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0 Safari/537.36"

HCPF_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Upgrade-Insecure-Requests": "1",
}

CO_CONTEXT_TERMS = [
    "health first colorado",
    "colorado medicaid",
    "medicaid",
    "chp+",
    "provider",
    "claims",
    "eligibility",
    "enrollment",
    "prior authorization",
    "managed care",
    "waiver",
    "state plan",
    "rural health",
    "rhtp",
    "hospital",
    "behavioral health",
]

PROVIDER_NEWS_SKIP = ["monthly provider bulletins", "provider bulletin", "provider bulletin index"]


def fetch_updates(
    *,
    keywords: list[str],
    max_records: int,
    progress: Callable[[str], None] | None = None,
) -> list[dict[str, str]]:
    limit = max(1, max_records)
    records: list[dict[str, str]] = []
    for fetcher in (fetch_bulletin_records, fetch_provider_news_records, fetch_newsroom_notice_records, fetch_rhtp_records):
        try:
            batch, scanned = fetcher(keywords=keywords)
        except Exception as exc:
            emit(progress, f"CO: {fetcher.__name__} failed: {exc}")
            continue
        records.extend(batch)
        emit(progress, f"CO {fetcher.__name__}: scanned {scanned} rows, normalized {len(batch)} records")

    output = unique_records(records)
    emit(progress, f"CO: normalized {len(output)} records")
    return sorted(output, key=sort_key, reverse=True)[:limit]


def fetch_bulletin_records(*, keywords: list[str]) -> tuple[list[dict[str, str]], int]:
    rows = dated_hcpf_links(
        BULLETINS_URL,
        url_terms=["/sites/hcpf/files/"],
        title_terms=["bulletin", "health first colorado", "medicaid program"],
        skip_terms=["index"],
    )
    records = []
    for row in rows:
        text = " ".join([row["title"], row["url"], "Health First Colorado Medicaid provider bulletin"])
        if not has_keyword_or_context(text, keywords, CO_CONTEXT_TERMS):
            continue
        records.append(
            state_update_record(
                state="CO",
                source="co_hcpf_provider_bulletins",
                source_record_id=source_id_from_url(row["url"]) or row["title"],
                record_type="provider_bulletin",
                title=row["title"],
                agency="Colorado Department of Health Care Policy and Financing",
                summary="Official Health First Colorado provider bulletin from HCPF.",
                posted_date=row["date"],
                document_url=row["url"],
                source_url=BULLETINS_URL,
                keywords=keywords,
                raw={"source_page": BULLETINS_URL},
            )
        )
    return records, len(rows)


def fetch_provider_news_records(*, keywords: list[str]) -> tuple[list[dict[str, str]], int]:
    rows = dated_hcpf_links(
        PROVIDER_NEWS_URL,
        url_terms=["/sites/hcpf/files/"],
        title_terms=["provider news", "action required", "known issue", "billing", "claim", "waiver", "pharmacy", "prior authorization", "rural", "policy"],
        skip_terms=PROVIDER_NEWS_SKIP,
    )
    records = []
    for row in rows:
        text = " ".join([row["title"], row["url"], "Health First Colorado Medicaid provider news"])
        if not has_keyword_or_context(text, keywords, CO_CONTEXT_TERMS):
            continue
        records.append(
            state_update_record(
                state="CO",
                source="co_hcpf_provider_news",
                source_record_id=source_id_from_url(row["url"]) or row["title"],
                record_type=record_type_for(text, "provider_bulletin"),
                title=row["title"],
                agency="Colorado Department of Health Care Policy and Financing",
                summary="Official HCPF Provider News and Resources document.",
                posted_date=row["date"],
                document_url=row["url"],
                source_url=PROVIDER_NEWS_URL,
                keywords=keywords,
                raw={"source_page": PROVIDER_NEWS_URL},
            )
        )
    return records, len(rows)


def fetch_newsroom_notice_records(*, keywords: list[str]) -> tuple[list[dict[str, str]], int]:
    markup = fetch_hcpf_text(NEWSROOM_URL)
    rows = []
    for link in parse_links(markup, NEWSROOM_URL):
        title = clean_text(link.text)
        url = clean_text(link.href)
        if not title or "coloradosos.gov/ccr/registercontents.do" not in url.lower():
            continue
        date = date_from_text(url)
        if not date:
            continue
        rows.append({"title": title, "url": url, "date": date})

    records = []
    for row in rows:
        text = " ".join([row["title"], row["url"], "HCPF Medicaid public notice state plan rulemaking"])
        if not has_keyword_or_context(text, keywords, CO_CONTEXT_TERMS):
            continue
        records.append(
            state_update_record(
                state="CO",
                source="co_hcpf_public_notices",
                source_record_id=source_id_from_url(row["url"]) or row["title"],
                record_type="spa_notice" if "state plan" in text.lower() else "public_comment_notice",
                title=row["title"],
                agency="Colorado Department of Health Care Policy and Financing",
                summary="Official HCPF newsroom public notice linked to Colorado Secretary of State rulemaking register.",
                posted_date=row["date"],
                comment_required=True,
                document_url=row["url"],
                source_url=NEWSROOM_URL,
                keywords=keywords,
                raw={"source_page": NEWSROOM_URL},
            )
        )
    return records, len(rows)


def fetch_rhtp_records(*, keywords: list[str]) -> tuple[list[dict[str, str]], int]:
    markup = fetch_hcpf_text(RHTP_URL)
    page_text = strip_tags(markup)
    rows: list[dict[str, str]] = []
    due_date = date_from_text(segment_after(page_text, "Applications must be submitted", 240))
    if due_date:
        rows.append(
            {
                "title": "Colorado Rural Health Transformation Program RFA is open",
                "url": RHTP_URL,
                "date": due_date,
                "due_date": due_date,
                "summary": "Official HCPF RHTP page states RFA applications must be submitted by the listed deadline.",
            }
        )

    rows.extend(
        dated_hcpf_links(
            RHTP_URL,
            url_terms=["/sites/hcpf/files/"],
            title_terms=["rhtp", "rural", "grant", "rfa", "eligibility", "budget", "fact sheet", "template", "cms"],
            skip_terms=[],
        )
    )

    records = []
    for row in rows:
        text = " ".join([row["title"], row["url"], row.get("summary", ""), "rural health transformation rhtp medicaid cms"])
        if not has_keyword_or_context(text, keywords, CO_CONTEXT_TERMS):
            continue
        records.append(
            state_update_record(
                state="CO",
                source="co_hcpf_rhtp",
                source_record_id=source_id_from_url(row["url"]) or row["title"],
                record_type="grant_notice" if row.get("due_date") or "grant" in text.lower() or "rfa" in text.lower() else "rht_notice",
                title=row["title"],
                agency="Colorado Department of Health Care Policy and Financing",
                summary=row.get("summary", "Official HCPF Rural Health Transformation Program document."),
                posted_date="" if row.get("due_date") else row.get("date", ""),
                due_date=row.get("due_date", ""),
                action_required_by=row.get("due_date", ""),
                document_url=row["url"],
                source_url=RHTP_URL,
                keywords=keywords,
                raw={"source_page": RHTP_URL},
            )
        )
    return records, len(rows)


def dated_hcpf_links(base_url: str, *, url_terms: list[str], title_terms: list[str], skip_terms: list[str]) -> list[dict[str, str]]:
    markup = fetch_hcpf_text(base_url)
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    for link in parse_links(markup, base_url):
        title = clean_text(link.text)
        url = clean_text(link.href)
        if not title or not url or url in seen:
            continue
        seen.add(url)
        lower = " ".join([title, url]).lower()
        if not any(term in lower for term in url_terms) or not any(term in lower for term in title_terms):
            continue
        if any(term in lower for term in skip_terms):
            continue
        date = date_from_text(" ".join([title, url]))
        if not date or not is_current_period(date):
            continue
        rows.append({"title": title, "url": url, "date": date})
    return rows


def fetch_hcpf_text(url: str) -> str:
    result = fetch_url(url, headers=HCPF_HEADERS, timeout=30, byte_limit=2_000_000, user_agent=USER_AGENT)
    result.raise_for_status()
    return result.body_text()


def has_keyword_or_context(text: str, keywords: list[str], context_terms: list[str]) -> bool:
    lower = clean_text(text).lower()
    if any(str(keyword).strip().lower() in lower for keyword in keywords if str(keyword).strip()):
        return True
    return any(term in lower for term in context_terms)


def is_current_period(date_text: str) -> bool:
    return date_text.startswith("2026") or date_text.startswith("2025")


def date_from_text(value: str) -> str:
    text = clean_text(urllib.parse.unquote(value))
    parsed = iso_date_text(text)
    if parsed:
        return parsed
    match = re.search(r"publicationDay=(\d{2})/(\d{2})/(20\d{2})", text, re.I)
    if match:
        return safe_date(int(match.group(3)), int(match.group(1)), int(match.group(2)))
    match = re.search(r"\b(\d{1,2})/(20\d{2})\b", text)
    if match:
        return safe_date(int(match.group(2)), int(match.group(1)), 1)
    match = re.search(r"\b(\d{1,2})[-._/](\d{1,2})[-._/](20\d{2}|\d{2})\b", text)
    if match:
        year = int(match.group(3))
        if year < 100:
            year += 2000
        return safe_date(year, int(match.group(1)), int(match.group(2)))
    match = re.search(r"\b(20\d{2})(\d{2})(\d{2})\b", text)
    if match:
        return safe_date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    match = re.search(r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+(20\d{2})\b", text, re.I)
    if match:
        return safe_date(int(match.group(2)), month_number(match.group(1)), 1)
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


def strip_tags(value: str) -> str:
    return re.sub(r"<[^>]+>", " ", value)


def segment_after(text: str, marker: str, limit: int) -> str:
    index = text.find(marker)
    return "" if index < 0 else text[index : index + limit]


def emit(progress: Callable[[str], None] | None, message: str) -> None:
    if progress:
        progress(message)
