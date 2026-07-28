from __future__ import annotations

import re
from typing import Callable

from services.state_updates import sort_key, state_update_record
from services.state_updates._official_html import date_from_text, is_challenge_page, source_rows as link_source_rows
from services.state_updates.common import clean_text, fetch_text, is_procurement_update, parse_links, record_type_for, source_id_from_url, unique_records

AGENCY = "North Dakota Health and Human Services"
UPDATES_URL = "https://www.hhs.nd.gov/healthcare/medicaid/provider/communications/updates"
PUBLIC_COMMENT_URL = "https://www.hhs.nd.gov/healthcare/medicaid/provider/manuals-and-guidelines/public-comment"
SOURCES = [
    ("nd_medicaid_provider_updates", UPDATES_URL, "provider_bulletin", ["medicaid", "provider", "fee-schedule", "policy"]),
    ("nd_medicaid_public_comment", PUBLIC_COMMENT_URL, "public_comment_notice", ["medicaid", "public-comment", "public comment", "policy"]),
]
SECTION_RE = re.compile(
    r"(?is)<h[1-6][^>]*>\s*(?:<[^>]+>\s*)*Posted\s+(?P<date>\d{1,2}/\d{1,2}/\d{4}).*?</h[1-6]>"
    r"(?P<body>.*?)(?=<h[1-6][^>]*>\s*(?:<[^>]+>\s*)*Posted\s+\d|$)"
)


def source_rows(markup: str, source_url: str, terms: list[str]) -> list[dict[str, str]]:
    """Parse ND's dated WYSIWYG sections, with dated links as a fallback."""
    rows: list[dict[str, str]] = []
    for match in SECTION_RE.finditer(markup):
        body_markup = match.group("body")
        summary = clean_text(body_markup)
        emphasized = re.search(r"(?is)<(?:strong|b|em)\b[^>]*>(.*?)</(?:strong|b|em)>", body_markup)
        title = clean_text(emphasized.group(1)) if emphasized else re.split(r"\s+[–—-]\s+", summary, maxsplit=1)[0]
        title = title.rstrip(".:")
        if not title or is_procurement_update(title, summary):
            continue
        links = [link.href for link in parse_links(match.group("body"), source_url) if not is_procurement_update(link.text, link.href)]
        rows.append({"title": title[:500], "url": links[0] if links else source_url,
                     "date": date_from_text(match.group("date")), "summary": summary[:1200]})
    return rows or link_source_rows(markup, source_url, terms)


def fetch_updates(*, keywords: list[str], max_records: int, progress: Callable[[str], None] | None = None) -> list[dict[str, str]]:
    if max_records <= 0:
        return []
    records: list[dict[str, str]] = []
    for source, source_url, default_type, terms in SOURCES:
        try:
            markup = fetch_text(source_url, timeout=20, byte_limit=1_500_000)
            if is_challenge_page(markup):
                raise RuntimeError("official source returned a bot challenge; skipped without bypass")
            rows = source_rows(markup, source_url, terms)
        except Exception as exc:
            emit(progress, f"ND {source} unavailable: {exc}")
            continue
        for row in rows:
            text = f"{row['title']} {row.get('summary', '')} {row['url']}"
            record_type = record_type_for(text, default_type)
            records.append(state_update_record(
                state="ND", source=source,
                source_record_id=f"{row['date']}:{row['title'][:180]}" if row["url"] == source_url else source_id_from_url(row["url"]),
                record_type=record_type, title=row["title"], agency=AGENCY,
                summary=row.get("summary", "") or f"Official {AGENCY} Medicaid update.", posted_date=row["date"],
                comment_required=(record_type == "public_comment_notice" or "public comment" in text.lower()),
                document_url=row["url"], source_url=source_url, keywords=keywords,
                raw={"source_page": source_url, "procurement_excluded": True},
            ))
        emit(progress, f"ND {source}: normalized {len(rows)} dated non-procurement notices")
    return sorted(unique_records(records), key=sort_key, reverse=True)[:max_records]


def emit(progress: Callable[[str], None] | None, message: str) -> None:
    if progress:
        progress(message)
