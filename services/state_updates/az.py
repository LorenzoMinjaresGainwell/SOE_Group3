from __future__ import annotations

import datetime as dt
import re
import urllib.parse
from typing import Callable

from services.state_updates import sort_key, state_update_record
from services.state_updates.common import absolute_url, clean_text, fetch_text, iso_date_text, parse_links, record_type_for, source_id_from_url, strip_query, unique_records

NEWS_URL = "https://www.azahcccs.gov/shared/news.html"
PUBLIC_NOTICES_URL = "https://www.azahcccs.gov/AHCCCS/PublicNotices/"
WAIVER_RENEWAL_URL = "https://www.azahcccs.gov/Resources/Federal/waiverrenewalrequest.html"
PHARMACY_UPDATES_URL = "https://www.azahcccs.gov/Resources/GuidesManualsPolicies/pharmacyupdates.html"
RHTP_URL = "https://www.azahcccs.gov/AHCCCS/Initiatives/RHTP/"

AZ_CONTEXT_TERMS = [
    "ahcccs",
    "medicaid",
    "kids care",
    "kidscare",
    "health care cost containment",
    "provider",
    "managed care",
    "state plan",
    "waiver",
    "1115",
    "rural health",
    "rhtp",
    "fee-for-service",
    "prior authorization",
    "pharmacy",
]

NEWS_CONTEXT_TERMS = AZ_CONTEXT_TERMS + [
    "renewal",
    "eligibility",
    "enrollment",
    "behavioral health",
    "workforce",
    "telehealth",
    "dashboard",
    "provider locator",
    "differential adjusted payment",
]

PHARMACY_EXCLUDE_TERMS = [
    "agenda",
    "minutes",
    "presentation",
    "testimony",
    "manufacturer request",
    "member roster",
    "application",
    "conflict of interest",
]


def fetch_updates(
    *,
    keywords: list[str],
    max_records: int,
    progress: Callable[[str], None] | None = None,
) -> list[dict[str, str]]:
    limit = max(1, max_records)
    records: list[dict[str, str]] = []

    source_fetchers = [
        fetch_news_records,
        fetch_public_notice_records,
        fetch_waiver_records,
        fetch_pharmacy_records,
        fetch_rhtp_records,
    ]
    for fetcher in source_fetchers:
        try:
            batch, scanned = fetcher(keywords=keywords)
        except Exception as exc:
            emit(progress, f"AZ: {fetcher.__name__} failed: {exc}")
            continue
        records.extend(batch)
        emit(progress, f"AZ {fetcher.__name__}: scanned {scanned} rows, normalized {len(batch)} records")

    output = unique_records(records)
    emit(progress, f"AZ: normalized {len(output)} records")
    return sorted(output, key=sort_key, reverse=True)[:limit]


def fetch_news_records(*, keywords: list[str]) -> tuple[list[dict[str, str]], int]:
    markup = fetch_text(NEWS_URL, timeout=30, byte_limit=2_000_000)
    items = parse_news_items(markup)
    records = []
    for item in items:
        text = " ".join([item["title"], item["summary"], item["url"]])
        if not is_current_period(text) or not has_keyword_or_context(text, keywords, NEWS_CONTEXT_TERMS):
            continue
        records.append(
            state_update_record(
                state="AZ",
                source="az_ahcccs_news",
                source_record_id=source_id_from_url(item["url"]) or item["title"],
                record_type=record_type_for(text),
                title=item["title"],
                agency="Arizona Health Care Cost Containment System (AHCCCS)",
                summary=item["summary"],
                posted_date=item["posted_date"],
                document_url=item["url"],
                source_url=NEWS_URL,
                keywords=keywords,
                raw={"source_page": NEWS_URL, "source_note": "Official AHCCCS dated news and press release listing."},
            )
        )
    return records, len(items)


def fetch_public_notice_records(*, keywords: list[str]) -> tuple[list[dict[str, str]], int]:
    rows = dated_document_links(
        PUBLIC_NOTICES_URL,
        url_terms=["/ahcccs/downloads/publicnotices/"],
        title_terms=["notice", "rate", "state plan", "spa", "comment", "waiver", "path", "fee-for-service"],
        skip_terms=[],
    )
    records = []
    for row in rows:
        text = " ".join([row["title"], row["url"], "AHCCCS Medicaid public notice"])
        if not is_current_period(row["date"]) or not has_keyword_or_context(text, keywords, AZ_CONTEXT_TERMS):
            continue
        rtype = record_type_for(text, "public_comment_notice")
        effective_date = row["date"] if "effective" in text.lower() or "dates of service" in text.lower() else ""
        records.append(
            state_update_record(
                state="AZ",
                source="az_ahcccs_public_notices",
                source_record_id=source_id_from_url(row["url"]) or row["title"],
                record_type=rtype,
                title=row["title"],
                agency="Arizona Health Care Cost Containment System (AHCCCS)",
                summary="Official AHCCCS Medicaid/public notice document.",
                posted_date="" if effective_date else row["date"],
                effective_date=effective_date,
                comment_required="comment" in text.lower() or "preliminary" in text.lower(),
                document_url=row["url"],
                source_url=PUBLIC_NOTICES_URL,
                keywords=keywords,
                raw={"source_page": PUBLIC_NOTICES_URL},
            )
        )
    return records, len(rows)


def fetch_waiver_records(*, keywords: list[str]) -> tuple[list[dict[str, str]], int]:
    markup = fetch_text(WAIVER_RENEWAL_URL, timeout=30, byte_limit=1_500_000)
    rows = []
    for link in parse_links(markup, WAIVER_RENEWAL_URL):
        title = clean_text(link.text)
        date = date_from_text(title)
        if not title or not date or "2026" not in date:
            continue
        if "1115" not in title.lower() and "waiver" not in title.lower() and "demonstration" not in title.lower() and "budget neutrality" not in title.lower():
            continue
        rows.append({"title": re.sub(r"^\d{1,2}/\d{1,2}/\d{4}\s*[-\u2013]\s*", "", title), "url": link.href, "date": date})

    records = []
    for row in rows:
        text = " ".join([row["title"], row["url"], "AHCCCS 1115 Medicaid waiver renewal"])
        if not has_keyword_or_context(text, keywords, AZ_CONTEXT_TERMS):
            continue
        records.append(
            state_update_record(
                state="AZ",
                source="az_ahcccs_1115_waiver",
                source_record_id=source_id_from_url(row["url"]) or row["title"],
                record_type="waiver_notice",
                title=row["title"],
                agency="Arizona Health Care Cost Containment System (AHCCCS)",
                summary="Official AHCCCS Section 1115 Medicaid Demonstration waiver renewal document.",
                posted_date=row["date"],
                comment_required="draft" in text.lower() or "public" in text.lower(),
                document_url=row["url"],
                source_url=WAIVER_RENEWAL_URL,
                keywords=keywords,
                raw={"source_page": WAIVER_RENEWAL_URL},
            )
        )
    return records, len(rows)


def fetch_pharmacy_records(*, keywords: list[str]) -> tuple[list[dict[str, str]], int]:
    rows = dated_document_links(
        PHARMACY_UPDATES_URL,
        url_terms=["/resources/downloads/pharmacyupdates/"],
        title_terms=["effective", "prior authorization", "preferred drug", "drug list", "contractor notice", "pharmacy"],
        skip_terms=PHARMACY_EXCLUDE_TERMS,
    )
    records = []
    for row in rows:
        text = " ".join([row["title"], row["url"], "AHCCCS Medicaid pharmacy provider policy"])
        if not is_current_period(text) or not has_keyword_or_context(text, keywords, AZ_CONTEXT_TERMS):
            continue
        records.append(
            state_update_record(
                state="AZ",
                source="az_ahcccs_pharmacy_updates",
                source_record_id=source_id_from_url(row["url"]) or row["title"],
                record_type="guidance",
                title=row["title"],
                agency="Arizona Health Care Cost Containment System (AHCCCS)",
                summary="Official AHCCCS pharmacy/prior-authorization policy update document.",
                effective_date=row["date"] if "effective" in text.lower() else "",
                posted_date="" if "effective" in text.lower() else row["date"],
                document_url=row["url"],
                source_url=PHARMACY_UPDATES_URL,
                keywords=keywords,
                raw={"source_page": PHARMACY_UPDATES_URL},
            )
        )
    return records, len(rows)


def fetch_rhtp_records(*, keywords: list[str]) -> tuple[list[dict[str, str]], int]:
    markup = fetch_text(RHTP_URL, timeout=30, byte_limit=1_000_000)
    page_text = strip_tags(markup)
    rows = []
    application_due = date_from_text(segment_after(page_text, "Applications must be submitted", 220))
    if application_due:
        rows.append(
            {
                "title": "Arizona Rural Health Transformation Program RFA is open",
                "url": RHTP_URL,
                "date": application_due,
                "summary": "Official AHCCCS RHTP page states RFA applications must be submitted by the listed deadline.",
                "due_date": application_due,
            }
        )
    rows.extend(
        dated_document_links(
            RHTP_URL,
            url_terms=["/ahcccs/downloads/initiatives/rhtp/"],
            title_terms=["rhtp", "rural", "grant", "budget", "application", "eligibility", "faq", "template"],
            skip_terms=[],
        )
    )

    records = []
    for row in rows:
        text = " ".join([row["title"], row["url"], row.get("summary", ""), "RHTP rural health transformation"])
        if not has_keyword_or_context(text, keywords, AZ_CONTEXT_TERMS):
            continue
        records.append(
            state_update_record(
                state="AZ",
                source="az_ahcccs_rhtp",
                source_record_id=source_id_from_url(row["url"]) or row["title"],
                record_type="grant_notice" if "grant" in text.lower() or "rfa" in text.lower() else "rht_notice",
                title=row["title"],
                agency="Arizona Health Care Cost Containment System (AHCCCS)",
                summary=row.get("summary", "Official AHCCCS Rural Health Transformation Program update document."),
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


def parse_news_items(markup: str) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    pattern = re.compile(r'(?is)<div\s+class="news-item[^>]*>(?P<block>.*?)(?=<div\s+class="news-item|</div>\s*</div>\s*<!--|\Z)')
    for match in pattern.finditer(markup):
        block = match.group("block")
        title_match = re.search(r"(?is)<h[23][^>]*>(.*?)</h[23]>", block)
        date_match = re.search(r'(?is)<label\s+class="date hidden">(.*?)</label>', block)
        if not title_match or not date_match:
            continue
        title = clean_text(strip_tags(title_match.group(1)))
        posted_date = iso_date_text(date_match.group(1))
        if not title or not posted_date:
            continue
        read_more = first_read_more(block, NEWS_URL)
        summary = limit_text(clean_text(strip_tags(remove_read_more(block))), 1000)
        items.append({"title": title, "posted_date": posted_date, "summary": summary, "url": read_more or NEWS_URL})
    return items


def dated_document_links(base_url: str, *, url_terms: list[str], title_terms: list[str], skip_terms: list[str]) -> list[dict[str, str]]:
    markup = fetch_text(base_url, timeout=30, byte_limit=2_000_000)
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
        if not date:
            continue
        rows.append({"title": title, "url": url, "date": date})
    return rows


def first_read_more(block: str, base_url: str) -> str:
    for href, label in re.findall(r'(?is)<a\b[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', block):
        if "read more" in strip_tags(label).lower():
            return absolute_url(base_url, href)
    return ""


def remove_read_more(block: str) -> str:
    return re.sub(r"(?is)<p>\s*<em>\s*<a[^>]*>\s*Read more.*?</a>\s*</em>\s*</p>", " ", block)


def strip_tags(value: str) -> str:
    return re.sub(r"<[^>]+>", " ", value)


def limit_text(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[: max(0, limit - 3)].rstrip() + "..."


def has_keyword_or_context(text: str, keywords: list[str], context_terms: list[str]) -> bool:
    lower = clean_text(text).lower()
    if any(str(keyword).strip().lower() in lower for keyword in keywords if str(keyword).strip()):
        return True
    return any(term in lower for term in context_terms)


def is_current_period(text: str) -> bool:
    return any(year in text for year in ("2026", "2025"))


def segment_after(text: str, marker: str, limit: int) -> str:
    index = text.find(marker)
    return "" if index < 0 else text[index : index + limit]


def date_from_text(value: str) -> str:
    text = clean_text(value)
    parsed = iso_date_text(text)
    if parsed:
        return parsed
    match = re.search(r"(?<!\d)(20\d{2})[-_/]?(\d{2})[-_/]?(\d{2})(?!\d)", text)
    if match:
        try:
            return dt.date(int(match.group(1)), int(match.group(2)), int(match.group(3))).isoformat()
        except ValueError:
            return ""
    match = re.search(r"(?<!\d)(\d{1,2})[-._/](\d{1,2})[-._/](20\d{2}|\d{2})(?!\d)", text)
    if match:
        month, day = int(match.group(1)), int(match.group(2))
        year = int(match.group(3))
        if year < 100:
            year += 2000
        try:
            return dt.date(year, month, day).isoformat()
        except ValueError:
            return ""
    return ""


def emit(progress: Callable[[str], None] | None, message: str) -> None:
    if progress:
        progress(message)
