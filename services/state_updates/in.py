from __future__ import annotations

import datetime as dt
import re
import urllib.parse
from typing import Callable

from services.state_updates import sort_key, state_update_record
from services.state_updates.common import clean_text, fetch_text, is_procurement_update, iso_date_text, parse_links, record_type_for, source_id_from_url, unique_records

AGENCY = 'Indiana Family and Social Services Administration / Indiana Medicaid'
# Discovery is intentionally limited to these official, public, simple HTML pages.
# A source failure is isolated and reported; no anti-bot/login challenge is bypassed.
SOURCES = [('in_ihcp_bulletins', 'https://www.in.gov/medicaid/providers/provider-references/bulletins-banner-pages-and-reference-modules/ihcp-bulletins/', 'provider_bulletin', ['bulletin', 'bt20', 'br20']), ('in_medicaid_state_plan', 'https://www.in.gov/medicaid/providers/provider-references/other-provider-resources/indiana-state-plan/', 'spa_notice', ['state plan amendment', 'spa ', 'public notice', 'waiver amendment'])]
CONTEXT_TERMS = ['indiana medicaid', 'ihcp', 'provider', 'state plan', 'waiver']


def fetch_updates(*, keywords: list[str], max_records: int, progress: Callable[[str], None] | None = None) -> list[dict[str, str]]:
    if max_records <= 0:
        return []
    records: list[dict[str, str]] = []
    for source, url, default_type, terms in SOURCES:
        try:
            markup = fetch_text(url, timeout=20, byte_limit=2_000_000)
            if is_block_page(markup):
                raise RuntimeError("official page returned an access-validation/challenge page; skipped")
            rows = source_rows(markup, url, terms)
        except Exception as exc:
            emit(progress, f"IN {source} unavailable: {exc}")
            continue
        accepted = [to_record(row, source, url, default_type, keywords) for row in rows if keep_row(row, keywords)]
        records.extend(accepted)
        emit(progress, f"IN {source}: scanned {len(rows)} candidate links, normalized {len(accepted)}")
    output = sorted(unique_records(records), key=sort_key, reverse=True)
    emit(progress, f"IN: normalized {len(output)} official policy updates")
    return output[:max_records]


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
        state='IN', source=source, source_record_id=source_id_from_url(row["url"]) or row["title"],
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
