from __future__ import annotations

import csv
import datetime as dt
import io
import json
import re
import urllib.parse
from typing import Any, Callable

from services.state_updates import sort_key, state_update_record
from services.state_updates.common import (
    absolute_url,
    clean_text,
    fetch_text,
    is_procurement_update,
    iso_date_text,
    parse_links,
    record_type_for,
    source_id_from_url,
    unique_records,
)

# Collector contract for small official HTML/JSON/CSV listings. Discovery is
# deliberately single-request, bounded, and does not follow item links.
MAX_BYTES = 1_000_000
TIMEOUT_SECONDS = 15
BLOCK_MARKERS = ("validate.perfdrive.com", "captcha", "access denied", "cf-chl-")
CONTAINER_RE = re.compile(r"(?is)<(?:tr|li|article)\b[^>]*>(.*?)</(?:tr|li|article)>")
YEAR_DATE_RE = re.compile(r"(?<!\d)(20\d{2})[._/-](\d{1,2})[._/-](\d{1,2})(?!\d)")
SHORT_DATE_RE = re.compile(r"(?<!\d)(\d{1,2})[._/-](\d{1,2})[._/-](\d{2}|20\d{2})(?!\d)")
MONTH_NAMES = "January|February|March|April|May|June|July|August|September|October|November|December"
MONTH_YEAR_RE = re.compile(rf"\b({MONTH_NAMES})\s+(20\d{{2}})\b", re.I)
MONTH_RANGE_RE = re.compile(rf"\b(?:{MONTH_NAMES})\s*[-–—]\s*(?:{MONTH_NAMES})\s+20\d{{2}}\b", re.I)


def collect_updates(
    *,
    state: str,
    agency: str,
    sources: list[dict[str, Any]],
    keywords: list[str],
    max_records: int,
    progress: Callable[[str], None] | None = None,
) -> list[dict[str, str]]:
    if max_records <= 0:
        return []
    records: list[dict[str, str]] = []
    for source in sources:
        key, url = str(source["key"]), str(source["url"])
        try:
            payload = fetch_text(url, timeout=TIMEOUT_SECONDS, byte_limit=MAX_BYTES)
            if is_block_page(payload):
                raise RuntimeError("official source returned a challenge page; skipped")
            rows = source_rows(payload, url, list(source.get("terms", [])))
        except Exception as exc:
            emit(progress, f"{state} {key} unavailable: {exc}")
            continue
        accepted = [
            to_record(state, agency, source, row, keywords)
            for row in rows
            if row.get("date") and not is_procurement_update(row.get("title", ""), row.get("url", ""), row.get("context", ""))
        ]
        records.extend(accepted)
        emit(progress, f"{state} {key}: scanned {len(rows)} dated candidates, normalized {len(accepted)}")
    output = sorted(unique_records(records), key=sort_key, reverse=True)
    emit(progress, f"{state}: normalized {len(output)} official dated policy updates")
    return output[:max_records]


def source_rows(payload: str, source_url: str, terms: list[str]) -> list[dict[str, str]]:
    stripped = payload.lstrip("\ufeff \t\r\n")
    if stripped.startswith(("{", "[")):
        return json_rows(stripped, source_url, terms)
    if looks_like_csv(stripped):
        return csv_rows(stripped, source_url, terms)
    return html_rows(payload, source_url, terms)


def html_rows(markup: str, source_url: str, terms: list[str]) -> list[dict[str, str]]:
    contexts: dict[str, str] = {}
    for fragment in CONTAINER_RE.findall(markup):
        context = clean_text(fragment)
        for link in parse_links(fragment, source_url):
            contexts.setdefault(link.href, context)
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    for link in parse_links(markup, source_url):
        title, url = clean_text(link.text), clean_text(link.href)
        context = contexts.get(url, title)
        candidate = clean_text(f"{title} {urllib.parse.unquote(url)} {context}")
        if not title or not url or url in seen or canonical_url(url) == canonical_url(source_url):
            continue
        if not term_match(candidate, terms) or is_procurement_update(candidate):
            continue
        date = date_from_text(candidate)
        if not date:
            continue
        seen.add(url)
        rows.append({"title": title, "url": url, "date": date, "context": context})
    return rows


def json_rows(payload: str, source_url: str, terms: list[str]) -> list[dict[str, str]]:
    data = json.loads(payload)
    if isinstance(data, dict):
        for key in ("items", "results", "value", "data"):
            if isinstance(data.get(key), list):
                data = data[key]
                break
    if not isinstance(data, list):
        return []
    return mapping_rows(data, source_url, terms)


def csv_rows(payload: str, source_url: str, terms: list[str]) -> list[dict[str, str]]:
    return mapping_rows(list(csv.DictReader(io.StringIO(payload))), source_url, terms)


def mapping_rows(items: list[Any], source_url: str, terms: list[str]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        normalized = {str(key).strip().lower(): value for key, value in item.items()}
        title = first_value(normalized, "title", "name", "subject", "headline")
        href = first_value(normalized, "document_url", "url", "link", "href", "permalink")
        date_text = first_value(normalized, "posted_date", "published_date", "published", "date", "updated_date", "updated")
        url = absolute_url(source_url, href)
        context = clean_text(" ".join(str(value or "") for value in normalized.values()))
        if not title or not url or not term_match(f"{title} {url} {context}", terms):
            continue
        if is_procurement_update(title, url, context):
            continue
        date = date_from_text(date_text)
        if date:
            rows.append({"title": title, "url": url, "date": date, "context": context})
    return rows


def to_record(state: str, agency: str, source: dict[str, Any], row: dict[str, str], keywords: list[str]) -> dict[str, str]:
    text = f"{row['title']} {row['context']}"
    record_type = record_type_for(text, str(source.get("record_type", "policy_update")))
    comment = record_type == "public_comment_notice" or "public comment" in text.lower()
    return state_update_record(
        state=state,
        source=str(source["key"]),
        source_record_id=source_id_from_url(row["url"]) or row["title"],
        record_type=record_type,
        title=row["title"],
        agency=agency,
        summary=f"Official {agency} policy/program update.",
        posted_date=row["date"],
        comment_required=comment,
        document_url=row["url"],
        source_url=str(source["url"]),
        keywords=keywords,
        raw={"source_page": source["url"], "dated_listing": True, "procurement_excluded": True},
    )


def date_from_text(value: str) -> str:
    text = clean_text(urllib.parse.unquote(str(value or "")))
    parsed = iso_date_text(text)
    if parsed:
        return parsed
    for regex, year_first in ((YEAR_DATE_RE, True), (SHORT_DATE_RE, False)):
        for match in regex.finditer(text):
            a, b, c = (int(part) for part in match.groups())
            year, month, day = (a, b, c) if year_first else (c + 2000 if c < 100 else c, a, b)
            try:
                return dt.date(year, month, day).isoformat()
            except ValueError:
                continue
    # A bulletin issue month is an official publication date granularity; use
    # its first day rather than probing document metadata or inventing a day.
    month_year_matches = list(MONTH_YEAR_RE.finditer(text))
    if len(month_year_matches) == 1 and not MONTH_RANGE_RE.search(text):
        return dt.datetime.strptime(month_year_matches[0].group(0), "%B %Y").date().isoformat()
    return ""


def first_value(row: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = clean_text(row.get(key, ""))
        if value:
            return value
    return ""


def term_match(text: str, terms: list[str]) -> bool:
    lower = text.lower()
    return any(str(term).strip().lower() in lower for term in terms if str(term).strip())


def looks_like_csv(value: str) -> bool:
    first_line = value.splitlines()[0].lower() if value.splitlines() else ""
    return "," in first_line and any(field in first_line for field in ("title", "name", "subject"))


def canonical_url(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), "", ""))


def is_block_page(payload: str) -> bool:
    lower = payload[:100_000].lower()
    return any(marker in lower for marker in BLOCK_MARKERS)


def emit(progress: Callable[[str], None] | None, message: str) -> None:
    if progress:
        progress(message)
