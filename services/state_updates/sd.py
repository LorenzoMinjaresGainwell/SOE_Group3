from __future__ import annotations

import datetime as dt
import html
import re
import urllib.parse
from typing import Any, Callable

from services.state_updates import emit, sort_key, state_update_record
from services.state_updates.common import absolute_url, clean_text, fetch_text, parse_links, parse_tables, unique_records

DSS_PROVIDER_COMMUNICATIONS_URL = "https://dss.sd.gov/medicaid/providers/communication.aspx"
DSS_STATE_PLAN_URL = "https://dss.sd.gov/medicaid/medicaidstateplan.aspx"
DOH_SITEMAP_URL = "https://doh.sd.gov/sitemap.xml"
DOH_RHT_PROJECT_URL = "https://doh.sd.gov/healthcare-professionals/rural-health/rural-health-transformation-project/"
DOH_RHT_RESOURCES_URL = f"{DOH_RHT_PROJECT_URL}rht-resources-faqs/"
DOH_RHT_PRESS_URL = f"{DOH_RHT_PROJECT_URL}rht-press-releases/"

PROVIDER_DOC_RE = re.compile(r"/(?:Communication|ProviderBulletins)/(20\d{2})/([^/?#]+)$", re.I)
FILE_DATE_RE = re.compile(r"^(\d{1,2})[._-](\d{1,2})(?:[._-](\d{2,4}))?[_-]")
SIX_DIGIT_DATE_RE = re.compile(r"(?<!\d)(\d{2})(\d{2})(20\d{2})(?!\d)")
RECENT_PROVIDER_YEARS = 3


def fetch_updates(
    *,
    keywords: list[str],
    max_records: int,
    progress: Callable[[str], None] | None = None,
) -> list[dict[str, str]]:
    limit = max(1, max_records)
    records: list[dict[str, str]] = []

    for fetcher in (fetch_provider_communications, fetch_state_plan_amendments, fetch_doh_rht_records):
        try:
            records.extend(fetcher(keywords=keywords, max_records=limit, progress=progress))
        except Exception as exc:
            emit(progress, f"SD: {fetcher.__name__} failed: {exc}")

    output = unique_records(records)
    emit(progress, f"SD: normalized {len(output)} total records")
    return sorted(output, key=sort_key, reverse=True)[:limit]


def fetch_provider_communications(
    *,
    keywords: list[str],
    max_records: int,
    progress: Callable[[str], None] | None = None,
) -> list[dict[str, str]]:
    markup = fetch_text(DSS_PROVIDER_COMMUNICATIONS_URL, timeout=30, byte_limit=1_000_000)
    records: list[dict[str, str]] = []
    scanned = 0
    min_year = dt.date.today().year - RECENT_PROVIDER_YEARS + 1

    for table in parse_tables(markup):
        for row in table:
            row_text = clean_text(" ".join(cell.text for cell in row))
            for cell in row:
                for link in cell.links:
                    document_url = absolute_url(DSS_PROVIDER_COMMUNICATIONS_URL, link.href)
                    posted_date = provider_date_from_url(document_url)
                    if not posted_date:
                        continue
                    if year_from_iso(posted_date) < min_year:
                        continue
                    scanned += 1
                    title = clean_text(link.text) or title_from_url(document_url)
                    records.append(
                        state_update_record(
                            state="SD",
                            source="sd_dss_medicaid_provider_communications",
                            source_record_id=source_id_from_url(document_url),
                            record_type="provider_bulletin",
                            title=title,
                            agency="South Dakota Department of Social Services",
                            summary=clean_text(f"South Dakota Medicaid provider communication. Listing context: {row_text}"),
                            posted_date=posted_date,
                            document_url=document_url,
                            source_url=DSS_PROVIDER_COMMUNICATIONS_URL,
                            keywords=keywords,
                            raw={
                                "source_page": DSS_PROVIDER_COMMUNICATIONS_URL,
                                "row_text": row_text,
                                "source_note": "Official DSS Medicaid provider communications page; dated provider PDF links only.",
                            },
                        )
                    )
                    if len(records) >= max_records:
                        emit(progress, f"SD DSS provider communications: scanned {scanned} dated links, normalized {len(records)} records")
                        return records

    emit(progress, f"SD DSS provider communications: scanned {scanned} dated links, normalized {len(records)} records")
    return records[:max_records]


def fetch_state_plan_amendments(
    *,
    keywords: list[str],
    max_records: int,
    progress: Callable[[str], None] | None = None,
) -> list[dict[str, str]]:
    markup = fetch_text(DSS_STATE_PLAN_URL, timeout=30, byte_limit=1_000_000)
    records: list[dict[str, str]] = []
    scanned = 0

    for table in parse_tables(markup):
        status = clean_text(table[0][0].text) if table and table[0] else "South Dakota Medicaid State Plan Amendment"
        header_index = spa_header_index(table)
        if header_index < 0:
            continue
        for row in table[header_index + 1 :]:
            if len(row) < 5:
                continue
            spa_number = clean_text(row[0].text)
            if not spa_number or "SPA" in spa_number.upper():
                continue
            scanned += 1
            title_cell = row[1]
            title = clean_text(title_cell.links[0].text if title_cell.links else first_sentence(title_cell.text))
            document_url = absolute_url(DSS_STATE_PLAN_URL, title_cell.links[0].href) if title_cell.links else DSS_STATE_PLAN_URL
            effective_date = sd_table_date(row[2].text)
            comment_start = sd_table_date(row[3].text)
            comment_end = sd_table_date(row[4].text)
            submitted = sd_table_date(row[5].text) if len(row) > 5 else ""
            approved = sd_table_date(row[6].text) if len(row) > 6 else ""
            comment_required = "public comment" in status.lower()
            summary = clean_text(f"{title_cell.text} Status: {status}.")
            records.append(
                state_update_record(
                    state="SD",
                    source="sd_dss_medicaid_state_plan_amendments",
                    source_record_id=f"{clean_key(status)}:{spa_number}",
                    record_type="spa_notice",
                    title=f"SPA {spa_number}: {title}",
                    agency="South Dakota Department of Social Services",
                    summary=summary,
                    posted_date=comment_start or submitted or approved,
                    updated_date=approved or submitted,
                    due_date=comment_end if comment_required else "",
                    effective_date=effective_date,
                    comment_required=comment_required,
                    action_required_by=comment_end if comment_required else "",
                    document_url=document_url,
                    source_url=DSS_STATE_PLAN_URL,
                    keywords=keywords,
                    raw={
                        "status": status,
                        "spa_number": spa_number,
                        "public_comment_start": comment_start,
                        "public_comment_end": comment_end,
                        "submitted_to_cms": submitted,
                        "approved": approved,
                        "source_note": "Official DSS Medicaid State Plan table with SPA number, effective date, comment period, and document link.",
                    },
                )
            )
            if len(records) >= max_records:
                emit(progress, f"SD DSS Medicaid state plan: scanned {scanned} SPA rows, normalized {len(records)} records")
                return records

    emit(progress, f"SD DSS Medicaid state plan: scanned {scanned} SPA rows, normalized {len(records)} records")
    return records[:max_records]


def fetch_doh_rht_records(
    *,
    keywords: list[str],
    max_records: int,
    progress: Callable[[str], None] | None = None,
) -> list[dict[str, str]]:
    lastmods = doh_sitemap_lastmods()
    records: list[dict[str, str]] = []
    scanned = 0

    for page_url in (DOH_RHT_PROJECT_URL, DOH_RHT_RESOURCES_URL, DOH_RHT_PRESS_URL):
        markup = fetch_text(page_url, timeout=30, byte_limit=1_000_000)
        for link in parse_links(markup, page_url):
            if not accepted_rht_link(link.text, link.href):
                continue
            scanned += 1
            clean_url = strip_fragment(link.href)
            posted_date = date_from_lastmod(lastmods.get(strip_query(clean_url), "")) or date_from_url(clean_url)
            if not posted_date:
                continue
            title = clean_text(link.text)
            if not title or title.lower() in {"rural health transformation", "view webinar"}:
                title = title_from_url(clean_url)
            source_text = " ".join([title, clean_url])
            record_type = "grant_notice" if grant_signal(source_text) else "rht_notice"
            records.append(
                state_update_record(
                    state="SD",
                    source="sd_doh_rural_health_transformation",
                    source_record_id=source_id_from_url(clean_url),
                    record_type=record_type,
                    title=title,
                    agency="South Dakota Department of Health",
                    summary="Official South Dakota Rural Health Transformation project update or resource.",
                    posted_date=posted_date,
                    updated_date=date_from_lastmod(lastmods.get(strip_query(clean_url), "")),
                    document_url=clean_url,
                    source_url=page_url,
                    keywords=keywords,
                    raw={
                        "source_page": page_url,
                        "sitemap_lastmod": lastmods.get(strip_query(clean_url), ""),
                        "source_note": "Official DOH RHT project/resources/press pages with sitemap lastmod dates; procurement portal rows are excluded.",
                    },
                )
            )
            if len(records) >= max_records:
                emit(progress, f"SD DOH RHT pages: scanned {scanned} dated RHT links, normalized {len(records)} records")
                return records

    emit(progress, f"SD DOH RHT pages: scanned {scanned} dated RHT links, normalized {len(records)} records")
    return records[:max_records]


def provider_date_from_url(url: str) -> str:
    match = PROVIDER_DOC_RE.search(urllib.parse.unquote(url))
    if not match:
        return ""
    year = int(match.group(1))
    filename = match.group(2).strip()
    date_match = FILE_DATE_RE.search(filename)
    if not date_match:
        return ""
    month = int(date_match.group(1))
    day = int(date_match.group(2))
    year_text = date_match.group(3)
    if year_text:
        year = int(year_text)
        if year < 100:
            year += 2000
    return safe_date(year, month, day)


def sd_table_date(value: str) -> str:
    text = clean_text(value)
    if not text:
        return ""
    for fmt in ("%m/%d/%y", "%m/%d/%Y", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            pass
    return ""


def date_from_url(url: str) -> str:
    match = SIX_DIGIT_DATE_RE.search(urllib.parse.unquote(url))
    if not match:
        return ""
    return safe_date(int(match.group(3)), int(match.group(1)), int(match.group(2)))


def doh_sitemap_lastmods() -> dict[str, str]:
    markup = fetch_text(DOH_SITEMAP_URL, timeout=30, byte_limit=3_000_000)
    output: dict[str, str] = {}
    for block in re.findall(r"<url>(.*?)</url>", markup, flags=re.S | re.I):
        loc_match = re.search(r"<loc>(.*?)</loc>", block, flags=re.S | re.I)
        lastmod_match = re.search(r"<lastmod>(.*?)</lastmod>", block, flags=re.S | re.I)
        if not loc_match or not lastmod_match:
            continue
        loc = strip_query(html.unescape(clean_text(loc_match.group(1))))
        lastmod = html.unescape(clean_text(lastmod_match.group(1)))
        if loc and lastmod and ("rht" in loc.lower() or "rural-health-transformation" in loc.lower()):
            output[loc] = lastmod
    return output


def date_from_lastmod(value: str) -> str:
    match = re.search(r"(20\d{2})-(\d{2})-(\d{2})", html.unescape(value or ""))
    if not match:
        return ""
    return safe_date(int(match.group(1)), int(match.group(2)), int(match.group(3)))


def accepted_rht_link(title: str, url: str) -> bool:
    clean_url = strip_fragment(url)
    lower = " ".join([title, clean_url]).lower()
    if not clean_url.startswith("https://doh.sd.gov/"):
        return False
    if clean_url.rstrip("/") in {DOH_RHT_PROJECT_URL.rstrip("/"), DOH_RHT_RESOURCES_URL.rstrip("/"), DOH_RHT_PRESS_URL.rstrip("/")}:
        return False
    if "rht" not in lower and "rural-health-transformation" not in lower and "rural health transformation" not in lower:
        return False
    return clean_url.lower().endswith(".pdf") or "/press-releases/" in clean_url.lower()


def grant_signal(text: str) -> bool:
    lower = text.lower()
    return any(term in lower for term in ("funding", "grant", "award", "request for proposal", "rfp", "application"))


def spa_header_index(table: list[list[Any]]) -> int:
    for index, row in enumerate(table[:4]):
        text = " | ".join(clean_text(cell.text).lower() for cell in row)
        if "spa" in text and "effective" in text and "public comment" in text:
            return index
    return -1


def safe_date(year: int, month: int, day: int) -> str:
    try:
        return dt.date(year, month, day).isoformat()
    except ValueError:
        return ""


def year_from_iso(value: str) -> int:
    try:
        return dt.date.fromisoformat(value).year
    except ValueError:
        return 0


def first_sentence(value: str) -> str:
    text = clean_text(value)
    match = re.match(r"(.+?[.!?])\s", text)
    return match.group(1) if match else text[:180]


def source_id_from_url(url: str) -> str:
    path = urllib.parse.urlsplit(strip_query(url)).path.strip("/")
    return clean_text(urllib.parse.unquote(path).replace("/", ":"))[:240]


def title_from_url(url: str) -> str:
    name = urllib.parse.urlsplit(strip_query(url)).path.rstrip("/").rsplit("/", 1)[-1]
    name = re.sub(r"\.(pdf|html|aspx)$", "", urllib.parse.unquote(name), flags=re.I)
    name = re.sub(r"[_-]+", " ", name)
    name = re.sub(r"\bRht\b", "RHT", name.title())
    return clean_text(name)


def strip_query(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def strip_fragment(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))


def clean_key(value: str) -> str:
    text = re.sub(r"[^a-z0-9]+", "_", value.lower())
    return text.strip("_")
