from __future__ import annotations

import datetime as dt
import re
import urllib.parse
from typing import Callable

from services.state_updates import sort_key, state_update_record
from services.state_updates.common import (
    clean_text,
    fetch_text,
    is_procurement_update,
    iso_date_text,
    parse_links,
    record_type_for,
    source_id_from_url,
    unique_records,
)

# Shared implementation for conservative official listing pages.  It accepts only
# item/document links that contain a source-specific term and an explicit date.
# Listing/navigation links and procurement notices are deliberately ignored.

def fetch_official_html_updates(
    *,
    state: str,
    agency: str,
    sources: list[tuple[str, str, str, list[str]]],
    keywords: list[str],
    max_records: int,
    progress: Callable[[str], None] | None = None,
    fetcher: Callable[..., str] = fetch_text,
) -> list[dict[str, str]]:
    if max_records <= 0:
        return []
    records: list[dict[str, str]] = []
    for source, source_url, default_type, terms in sources:
        try:
            markup = fetcher(source_url, timeout=20, byte_limit=1_500_000)
            if is_challenge_page(markup):
                raise RuntimeError("official source returned a bot challenge; skipped without bypass")
            rows = source_rows(markup, source_url, terms)
        except Exception as exc:
            emit(progress, f"{state} {source} unavailable: {exc}")
            continue
        for row in rows:
            text = f"{row['title']} {row['url']}"
            # A waiver application is a waiver notice, not a grant application.
            # Other listing types may still be refined (for example a provider
            # bulletin page carrying a public-comment notice).
            record_type = default_type if default_type == "waiver_notice" else record_type_for(text, default_type)
            records.append(
                state_update_record(
                    state=state,
                    source=source,
                    source_record_id=source_id_from_url(row["url"]) or row["title"],
                    record_type=record_type,
                    title=row["title"],
                    agency=agency,
                    summary=f"Official {agency} Medicaid or health-policy update.",
                    posted_date=row["date"],
                    comment_required=(record_type == "public_comment_notice" or "public comment" in text.lower()),
                    document_url=row["url"],
                    source_url=source_url,
                    keywords=keywords,
                    raw={"source_page": source_url, "procurement_excluded": True},
                )
            )
        emit(progress, f"{state} {source}: normalized {len(rows)} dated non-procurement links")
    output = sorted(unique_records(records), key=sort_key, reverse=True)
    return output[:max_records]


def source_rows(markup: str, source_url: str, terms: list[str]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    for link in parse_links(markup, source_url):
        title = clean_text(link.text)
        url = clean_text(link.href)
        decoded = urllib.parse.unquote(url)
        candidate = f"{title} {decoded}".lower()
        if not title or not is_public_http_url(url) or url in seen:
            continue
        if canonical_page(url) == canonical_page(source_url) or is_procurement_update(title, decoded):
            continue
        if not any(term.lower() in candidate for term in terms):
            continue
        date = date_from_text(candidate)
        if not date:
            continue
        seen.add(url)
        rows.append({"title": title, "url": url, "date": date})
    return rows


def date_from_text(value: str) -> str:
    parsed = iso_date_text(value)
    if parsed:
        return parsed
    patterns = (
        (r"(?<!\d)(20\d{2})[._/-](\d{1,2})[._/-](\d{1,2})(?!\d)", "ymd"),
        (r"(?<!\d)(\d{1,2})[._/-](\d{1,2})[._/-](20\d{2}|\d{2})(?!\d)", "mdy"),
        (r"(?<!\d)(20\d{2})(\d{2})(\d{2})(?!\d)", "ymd"),
        (r"(?<!\d)(\d{1,2})(\d{2})(20\d{2}|\d{2})(?!\d)", "mdy"),
    )
    for pattern, order in patterns:
        match = re.search(pattern, value)
        if not match:
            continue
        a, b, c = (int(part) for part in match.groups())
        year, month, day = (a, b, c) if order == "ymd" else (c, a, b)
        if year < 100:
            year += 2000
        try:
            return dt.date(year, month, day).isoformat()
        except ValueError:
            continue
    # Official upload paths often supply an exact year/month but not a day.
    # Do not invent a day: records without an exact date remain unresolved.
    return ""


def is_challenge_page(markup: str) -> bool:
    lower = markup[:100_000].lower()
    return any(marker in lower for marker in ("validate.perfdrive.com", "radware bot manager captcha", "cf-chl-", "access denied"))


def is_public_http_url(url: str) -> bool:
    parts = urllib.parse.urlsplit(url)
    return parts.scheme in {"http", "https"} and bool(parts.netloc)


def canonical_page(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/").lower(), "", ""))


def emit(progress: Callable[[str], None] | None, message: str) -> None:
    if progress:
        progress(message)
