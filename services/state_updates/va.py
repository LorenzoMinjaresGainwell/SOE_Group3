from __future__ import annotations

import datetime as dt
import re
from typing import Any, Callable

from services.state_updates import state_update_record
from services.state_updates.common import absolute_url, clean_text, fetch_text, first_date_text, parse_tables, record_type_for, unique_records

VAMED_BASE = "https://vamedicaid.dmas.virginia.gov"
DMAS_BASE = "https://www.dmas.virginia.gov"
PROVIDER_LIBRARY_URL = f"{VAMED_BASE}/provider/library"
MEMO_YEAR_URLS = [
    f"{VAMED_BASE}/providers/memo-2026",
    f"{VAMED_BASE}/providers/memo-2025",
]
PRESS_RELEASES_URL = f"{DMAS_BASE}/news-updates/press-releases/"
WAIVER_URL = f"{DMAS_BASE}/about-us/1115-demonstration-waiver/"

PRESS_ITEM_RE = re.compile(
    r"(?is)<div\s+class=[\"'][^\"']*\bpressrelease_item\b[^\"']*[\"'][^>]*>.*?"
    r"<a\b[^>]*href=[\"'](?P<href>[^\"']+)[\"'][^>]*>.*?"
    r"<p\s+class=[\"'][^\"']*\bheading\b[^\"']*[\"'][^>]*>(?P<title>.*?)</p>.*?"
    r"<div\s+class=[\"']text-right[\"']>(?P<date>.*?)</div>.*?"
    r"<div\s+class=[\"'][^\"']*\bbase-text\b[^\"']*[\"'][^>]*>(?P<summary>.*?)</div>.*?</a>",
)

VA_PRESS_CONTEXT_TERMS = [
    "medicaid",
    "cardinal care",
    "managed care",
    "health plan",
    "health plans",
    "rural health",
    "rural healthcare",
    "rural health transformation",
    "behavioral health",
    "substance abuse",
    "substance use",
    "dental",
    "healthcare access",
    "health care access",
    "workforce",
    "federal requirements",
]


def fetch_updates(
    *,
    keywords: list[str],
    max_records: int,
    progress: Callable[[str], None] | None = None,
) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    scanned = 0

    memo_rows = fetch_memo_rows()
    scanned += len(memo_rows)
    for row in memo_rows:
        records.append(memo_record(row, keywords))
    emit(progress, f"VA: scanned {len(memo_rows)} Virginia Medicaid memo/bulletin rows")

    press_rows = fetch_press_release_rows()
    scanned += len(press_rows)
    for row in press_rows:
        if is_relevant_press_release(" ".join([row.get("title", ""), row.get("summary", "")]), keywords):
            records.append(press_release_record(row, keywords))
    emit(progress, f"VA: scanned {len(press_rows)} DMAS press release rows")

    waiver_rows = fetch_waiver_rows()
    scanned += len(waiver_rows)
    for row in waiver_rows:
        records.append(waiver_record(row, keywords))
    emit(progress, f"VA: scanned {len(waiver_rows)} DMAS 1115 waiver public-hearing rows")

    output = unique_records(records)
    emit(progress, f"VA: normalized {len(output)} records from {scanned} scanned rows")
    return output[:max_records]


def fetch_memo_rows() -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for url in MEMO_YEAR_URLS:
        markup = fetch_text(url, timeout=25, byte_limit=500_000)
        for table in parse_tables(markup):
            if not table or not has_memo_headers(table[0]):
                continue
            for cells in table[1:]:
                if len(cells) < 2:
                    continue
                posted_date = va_short_date_to_iso(cells[0].text)
                title_cell = cells[1]
                title = clean_text(title_cell.text)
                if not posted_date or not title:
                    continue
                link = title_cell.links[0].href if title_cell.links else ""
                rows.append(
                    {
                        "posted_date": posted_date,
                        "title": title,
                        "to": clean_text(cells[2].text) if len(cells) > 2 else "",
                        "url": absolute_url(url, link) if link else url,
                        "year_page": url,
                    }
                )
    return rows


def fetch_press_release_rows() -> list[dict[str, str]]:
    markup = fetch_text(PRESS_RELEASES_URL, timeout=25, byte_limit=500_000)
    rows: list[dict[str, str]] = []
    for match in PRESS_ITEM_RE.finditer(markup):
        title = clean_text(match.group("title"))
        posted_date = dotted_date_to_iso(match.group("date"))
        if not title or not posted_date:
            continue
        rows.append(
            {
                "title": title,
                "posted_date": posted_date,
                "summary": clean_text(match.group("summary")),
                "url": absolute_url(PRESS_RELEASES_URL, match.group("href")),
            }
        )
    return rows


def fetch_waiver_rows() -> list[dict[str, str]]:
    markup = fetch_text(WAIVER_URL, timeout=25, byte_limit=300_000)
    rows: list[dict[str, str]] = []
    for table in parse_tables(markup):
        for cells in table:
            text = clean_text(" ".join(cell.text for cell in cells))
            if "1115" not in text and "Waiver" not in text and "Demonstration" not in text:
                continue
            posted_date = long_date_to_iso(first_date_text(text))
            if not posted_date:
                continue
            pdf_link = first_pdf_link(cells)
            rows.append(
                {
                    "title": waiver_title(text),
                    "posted_date": posted_date,
                    "summary": text,
                    "document_url": absolute_url(WAIVER_URL, pdf_link) if pdf_link else "",
                }
            )
    return rows


def memo_record(row: dict[str, str], keywords: list[str]) -> dict[str, str]:
    title = row.get("title", "")
    return state_update_record(
        state="VA",
        source="va_medicaid_provider_memos",
        source_record_id=source_id_from_url(row.get("url", "")) or title,
        record_type="provider_bulletin",
        title=title,
        agency="Virginia Department of Medical Assistance Services / Virginia Medicaid",
        summary=f"To: {row.get('to', '')}" if row.get("to") else "",
        posted_date=row.get("posted_date", ""),
        source_url=row.get("url", "") or row.get("year_page", ""),
        keywords=keywords,
        raw={"source_page": row.get("year_page", ""), "audience": row.get("to", "")},
    )


def press_release_record(row: dict[str, str], keywords: list[str]) -> dict[str, str]:
    title = row.get("title", "")
    summary = row.get("summary", "")
    return state_update_record(
        state="VA",
        source="va_dmas_press_releases",
        source_record_id=source_id_from_url(row.get("url", "")) or title,
        record_type=record_type_for(" ".join([title, summary])),
        title=title,
        agency="Virginia Department of Medical Assistance Services",
        summary=summary,
        posted_date=row.get("posted_date", ""),
        document_url=row.get("url", ""),
        source_url=PRESS_RELEASES_URL,
        keywords=keywords,
        raw={"source_page": PRESS_RELEASES_URL},
    )


def waiver_record(row: dict[str, str], keywords: list[str]) -> dict[str, str]:
    return state_update_record(
        state="VA",
        source="va_dmas_1115_waiver",
        source_record_id=f"1115:{row.get('posted_date', '')}:{row.get('title', '')[:80]}",
        record_type="waiver_notice",
        title=row.get("title", "DMAS 1115 Demonstration Waiver public notice"),
        agency="Virginia Department of Medical Assistance Services",
        summary=row.get("summary", ""),
        posted_date=row.get("posted_date", ""),
        comment_required=True,
        document_url=row.get("document_url", ""),
        source_url=WAIVER_URL,
        keywords=keywords,
        raw={"source_page": WAIVER_URL},
    )


def has_memo_headers(cells: list[Any]) -> bool:
    text = " | ".join(clean_text(cell.text).lower() for cell in cells)
    return "issue date" in text and "title" in text


def is_relevant_press_release(text: str, keywords: list[str]) -> bool:
    lower = clean_text(text).lower()
    if any(str(keyword).strip().lower() in lower for keyword in keywords if str(keyword).strip()):
        return True
    return any(term in lower for term in VA_PRESS_CONTEXT_TERMS)


def va_short_date_to_iso(value: str) -> str:
    text = clean_text(value)
    for fmt in ("%d-%b-%y", "%d-%B-%y", "%d-%b-%Y", "%d-%B-%Y"):
        try:
            return dt.datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            pass
    return long_date_to_iso(text)


def dotted_date_to_iso(value: str) -> str:
    text = clean_text(value)
    for fmt in ("%m.%d.%Y", "%m/%d/%Y", "%m-%d-%Y"):
        try:
            return dt.datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            pass
    return ""


def long_date_to_iso(value: str) -> str:
    text = clean_text(value)
    if not text:
        return ""
    for fmt in ("%B %d, %Y", "%b %d, %Y"):
        try:
            return dt.datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            pass
    return ""


def first_pdf_link(cells: list[Any]) -> str:
    for cell in cells:
        for link in cell.links:
            if ".pdf" in link.href.lower():
                return link.href
    return ""


def waiver_title(text: str) -> str:
    value = clean_text(text)
    match = re.match(r"(Public Hearing #[12]:\s*[^.]+?)(?:\s+o\s+|\s+Monday|\s+Tuesday|\s+June|\s+December|$)", value)
    if match:
        return clean_text(match.group(1),)[:500]
    return value[:500] or "DMAS 1115 Demonstration Waiver public notice"


def source_id_from_url(url: str) -> str:
    path = re.sub(r"\?.*$", "", str(url or "")).rstrip("/")
    return clean_text(path.rsplit("/", 1)[-1])[:220]


def emit(progress: Callable[[str], None] | None, message: str) -> None:
    if progress:
        progress(message)
