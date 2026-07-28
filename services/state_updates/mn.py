from __future__ import annotations

import datetime as dt
import re
import urllib.parse
from typing import Callable

from services.state_updates import sort_key, state_update_record
from services.state_updates.common import clean_text, fetch_text, is_procurement_update, iso_date_text, parse_links, record_type_for, source_id_from_url, unique_records

AGENCY = 'Minnesota Department of Human Services'
# Discovery is intentionally limited to these official, public, simple HTML pages.
# A source failure is isolated and reported; no anti-bot/login challenge is bypassed.
# Live discovery (time-boxed): both DHS routes currently redirect this client to
# validate.perfdrive.com; the collector detects and reports that challenge, then stops.
BLOCKED_SOURCES = [('mn_mhcp_provider_news', 'https://mn.gov/dhs/partners-and-providers/news-initiatives-reports-workgroups/minnesota-health-care-programs/provider-news/', 'provider_bulletin', ['provider news', 'mhcp', 'bulletin']), ('mn_dhs_rht', 'https://mn.gov/dhs/rural-health-transformation/', 'rht_notice', ['rural health transformation', 'rht', 'funding'])]
SOURCES = BLOCKED_SOURCES
CONTEXT_TERMS = ['minnesota health care programs', 'mhcp', 'medicaid', 'provider', 'waiver', 'rural health']


def fetch_updates(*, keywords: list[str], max_records: int, progress: Callable[[str], None] | None = None) -> list[dict[str, str]]:
    """Documented network-free no-op for conclusive Radware-blocked sources.

    Repeated public GET probes of both official DHS listings redirect to
    ``validate.perfdrive.com`` and return the Radware Bot Manager CAPTCHA.
    We do not attempt challenge bypasses.  Keep the pure parsing helpers below
    for fixture validation and a future source recovery, but never call the
    network from this adapter while that classification applies.
    """
    if SOURCES is BLOCKED_SOURCES:
        emit(progress, "MN: blocked — official DHS update listings return a Radware Bot Manager CAPTCHA challenge page; no bypass attempted")
        return []

    # Hermetic fixture seam: tests may replace SOURCES with local synthetic
    # listings to retain confidence in parsing before an official source recovers.
    records: list[dict[str, str]] = []
    for source, url, default_type, terms in SOURCES:
        markup = fetch_text(url, timeout=20, byte_limit=2_000_000)
        if is_block_page(markup):
            emit(progress, f"MN {source} unavailable: official challenge page; skipped")
            continue
        rows = source_rows(markup, url, terms)
        records.extend(to_record(row, source, url, default_type, keywords) for row in rows if keep_row(row, keywords))
    return sorted(unique_records(records), key=sort_key, reverse=True)[:max_records]


def source_rows(markup: str, source_url: str, terms: list[str]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    for link in parse_links(markup, source_url):
        title, url = clean_text(link.text), clean_text(link.href)
        decoded_url = urllib.parse.unquote(url)
        candidate_text = f"{title} {decoded_url.rsplit('/', 1)[-1]}".lower()
        text = f"{title} {decoded_url}".lower()
        if not title or not url or url in seen or is_procurement_update(title, decoded_url):
            continue
        if not any(term.lower() in candidate_text for term in terms):
            continue
        if canonical_page(url) == canonical_page(source_url):
            continue
        seen.add(url)
        rows.append({"title": title, "url": url, "date": date_from_text(text)})
    return rows


def keep_row(row: dict[str, str], keywords: list[str]) -> bool:
    text = f"{row['title']} {row['url']}".lower()
    return any(str(k).strip().lower() in text for k in keywords if str(k).strip()) or any(term in text for term in CONTEXT_TERMS)


def to_record(row: dict[str, str], source: str, source_url: str, default_type: str, keywords: list[str]) -> dict[str, str]:
    text = f"{row['title']} {row['url']}"
    rtype = record_type_for(text, default_type)
    comment = rtype == "public_comment_notice" or "public comment" in text.lower()
    return state_update_record(
        state='MN', source=source, source_record_id=source_id_from_url(row["url"]) or row["title"],
        record_type=rtype, title=row["title"], agency=AGENCY,
        summary=f"Official {AGENCY} policy/program update.", posted_date=row["date"],
        comment_required=comment, document_url=row["url"],
        source_url=source_url, keywords=keywords, raw={"href": row["url"], "source_page": source_url},
    )


def date_from_text(value: str) -> str:
    parsed = iso_date_text(clean_text(urllib.parse.unquote(value)))
    if parsed:
        return parsed
    for pattern in (r"/(20\d{2})-(\d{2})-(\d{2})/", r"(?<!\d)(20\d{2})(\d{2})(\d{2})(?!\d)", r"(?<!\d)(\d{1,2})[._-](\d{1,2})[._-](20\d{2}|\d{2})(?!\d)"):
        match = re.search(pattern, value)
        if not match:
            continue
        parts = [int(part) for part in match.groups()]
        if pattern.startswith("/") or len(match.group(1)) == 4:
            year, month, day = parts
        else:
            month, day, year = parts
            year += 2000 if year < 100 else 0
        try:
            return dt.date(year, month, day).isoformat()
        except ValueError:
            pass
    return ""


def canonical_page(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))


def is_block_page(markup: str) -> bool:
    lower = markup[:100_000].lower()
    return "validate.perfdrive.com" in lower or "captcha" in lower or "access denied" in lower


def emit(progress: Callable[[str], None] | None, message: str) -> None:
    if progress:
        progress(message)
