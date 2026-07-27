from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Callable

from services.state_updates import emit, state_update_record
from services.state_updates.common import (
    absolute_url,
    clean_text,
    data_rows,
    due_date_from_text,
    fetch_text,
    find_table,
    first_date_text,
    iso_date_text,
    matches_keywords_or_context,
    parse_links,
    parse_tables,
    record_type_for,
    source_id_from_url,
    strip_query,
    unique_records,
)

AGENCY = "Michigan Department of Health and Human Services"
SITEMAP_URL = "https://www.michigan.gov/mdhhs/sitemap.xml"
POLICY_BULLETIN_PAGES = [
    "https://www.michigan.gov/mdhhs/doing-business/providers/providers/medicaid/policyforms/2026-medicaid-policy-bulletins",
    "https://www.michigan.gov/mdhhs/doing-business/providers/providers/medicaid/policyforms/2025-medicaid-policy-bulletins",
]
PROVIDER_ALERTS_PAGE = (
    "https://www.michigan.gov/mdhhs/assistance-programs/medicaid/portalhome/"
    "medicaid-providers/medicaid-provider-alerts/all-alerts-and-updates"
)
PUBLIC_NOTICES_PAGE = "https://www.michigan.gov/mdhhs/assistance-programs/medicaid/portalhome/resources/public-notices"
RHTP_UPDATES_PAGE = "https://www.michigan.gov/mdhhs/assistance-programs/medicaid/rural-health-transformation-program/rhtp-updates"
CONTEXT_TERMS = [
    "medicaid",
    "provider alert",
    "provider bulletin",
    "policy bulletin",
    "state plan amendment",
    "public notice",
    "waiver",
    "managed care",
    "mi coordinated health",
    "rural health transformation",
    "rhtp",
    "champs",
    "claims",
    "eligibility",
    "behavioral health",
]


def fetch_updates(
    *,
    keywords: list[str],
    max_records: int,
    progress: Callable[[str], None] | None = None,
) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    sitemap_dates = fetch_sitemap_dates(progress=progress)
    records.extend(fetch_policy_bulletins(keywords=keywords, progress=progress))
    records.extend(fetch_provider_alerts(keywords=keywords, sitemap_dates=sitemap_dates, limit=80, progress=progress))
    records.extend(fetch_public_notices(keywords=keywords, progress=progress))
    records.extend(fetch_rhtp_updates(keywords=keywords, progress=progress))
    emit(progress, f"MI: normalized {len(records)} records from official MDHHS update sources")
    return unique_records(records)[:max_records]


def fetch_policy_bulletins(*, keywords: list[str], progress: Callable[[str], None] | None) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    scanned = 0
    for page_url in POLICY_BULLETIN_PAGES:
        markup = fetch_text(page_url)
        table = find_table(parse_tables(markup), ["issue date", "bulletin number", "subject"])
        for row in data_rows(table):
            if len(row) < 4:
                continue
            scanned += 1
            issue_date, bulletin, consultation, subject = row[:4]
            if not bulletin.links:
                continue
            bulletin_number = clean_text(bulletin.links[0].text or bulletin.text)
            title = clean_text(f"{bulletin_number} - {subject.text}")
            search_text = " ".join([title, consultation.text, "Michigan Medicaid policy bulletin provider"])
            if not matches_keywords_or_context(search_text, keywords, CONTEXT_TERMS):
                continue
            document_url = absolute_url(page_url, bulletin.links[0].href)
            posted_date = iso_date_text(first_date_text(issue_date.text))
            records.append(
                state_update_record(
                    state="MI",
                    source="mi_mdhhs_policy_bulletins",
                    source_record_id=bulletin_number or source_id_from_url(document_url),
                    record_type="provider_bulletin",
                    title=title,
                    agency=AGENCY,
                    summary="MDHHS Medicaid policy bulletin.",
                    posted_date=posted_date,
                    document_url=document_url,
                    source_url=page_url,
                    keywords=keywords,
                    raw={"issue_date": issue_date.text, "bulletin": bulletin.text, "subject": subject.text},
                )
            )
    emit(progress, f"MI Medicaid policy bulletins: scanned {scanned}, kept {len(records)}")
    return records


def fetch_provider_alerts(
    *,
    keywords: list[str],
    sitemap_dates: dict[str, str],
    limit: int,
    progress: Callable[[str], None] | None,
) -> list[dict[str, str]]:
    markup = fetch_text(PROVIDER_ALERTS_PAGE)
    records: list[dict[str, str]] = []
    scanned = 0
    seen: set[str] = set()
    for link in parse_links(markup, PROVIDER_ALERTS_PAGE):
        if "/medicaid-provider-alerts/all-alerts-and-updates/" not in link.href:
            continue
        document_url = strip_query(link.href)
        if document_url in seen:
            continue
        seen.add(document_url)
        scanned += 1
        title = clean_text(link.text) or clean_text(link.href.rsplit("/", 1)[-1])
        search_text = " ".join([title, document_url, "Michigan Medicaid provider alert"])
        if not matches_keywords_or_context(search_text, keywords, CONTEXT_TERMS):
            continue
        posted_date = sitemap_dates.get(document_url, "")
        records.append(
            state_update_record(
                state="MI",
                source="mi_mdhhs_provider_alerts",
                source_record_id=source_id_from_url(document_url),
                record_type="guidance",
                title=title,
                agency=AGENCY,
                summary="MDHHS Medicaid provider alert/update.",
                posted_date=posted_date,
                updated_date=posted_date,
                document_url=document_url,
                source_url=PROVIDER_ALERTS_PAGE,
                keywords=keywords,
                raw={"sitemap_lastmod": posted_date, "href": link.href},
            )
        )
        if scanned >= limit:
            break
    emit(progress, f"MI Medicaid provider alerts: scanned {scanned}, kept {len(records)}")
    return records


def fetch_public_notices(*, keywords: list[str], progress: Callable[[str], None] | None) -> list[dict[str, str]]:
    markup = fetch_text(PUBLIC_NOTICES_PAGE)
    table = find_table(parse_tables(markup), ["release date", "comment due", "public notice"])
    records: list[dict[str, str]] = []
    scanned = 0
    for row in data_rows(table):
        if len(row) < 3:
            continue
        scanned += 1
        release, comment_due, description = row[:3]
        if not description.links:
            continue
        posted_date = iso_date_text(first_date_text(release.text))
        due_date = due_date_from_text(comment_due.text, posted_date)
        if not (posted_date or due_date):
            continue
        for link in description.links:
            title = clean_text(link.text)
            search_text = " ".join([title, release.text, comment_due.text, "Michigan Medicaid public notice state plan amendment"])
            if not matches_keywords_or_context(search_text, keywords, CONTEXT_TERMS):
                continue
            document_url = absolute_url(PUBLIC_NOTICES_PAGE, link.href)
            records.append(
                state_update_record(
                    state="MI",
                    source="mi_mdhhs_public_notices",
                    source_record_id=source_id_from_url(document_url),
                    record_type=record_type_for(title, "public_comment_notice"),
                    title=title,
                    agency=AGENCY,
                    summary=clean_text(f"Comment due date: {comment_due.text}"),
                    posted_date=posted_date,
                    due_date=due_date,
                    comment_required=bool(due_date or "comment" in comment_due.text.lower()),
                    document_url=document_url,
                    source_url=PUBLIC_NOTICES_PAGE,
                    keywords=keywords,
                    raw={"release": release.text, "comment_due": comment_due.text, "href": link.href},
                )
            )
    emit(progress, f"MI Medicaid public notices: scanned {scanned}, kept {len(records)}")
    return records


def fetch_rhtp_updates(*, keywords: list[str], progress: Callable[[str], None] | None) -> list[dict[str, str]]:
    markup = fetch_text(RHTP_UPDATES_PAGE)
    records: list[dict[str, str]] = []
    scanned = 0
    for link in parse_links(markup, RHTP_UPDATES_PAGE):
        if "/inside-mdhhs/newsroom/" not in link.href:
            continue
        title = clean_text(link.text)
        if "rural health transformation" not in title.lower() and "rht" not in title.lower():
            continue
        scanned += 1
        nearby_date = date_after_link(markup, link.href) or iso_date_text(first_date_text(link.text))
        search_text = " ".join([title, "Rural Health Transformation Program CMS grant funding Medicaid"])
        if not matches_keywords_or_context(search_text, keywords, CONTEXT_TERMS):
            continue
        document_url = absolute_url(RHTP_UPDATES_PAGE, link.href)
        records.append(
            state_update_record(
                state="MI",
                source="mi_mdhhs_rhtp_updates",
                source_record_id=source_id_from_url(document_url),
                record_type=record_type_for(title, "rht_notice"),
                title=title,
                agency=AGENCY,
                summary="MDHHS Rural Health Transformation Program update.",
                posted_date=nearby_date,
                document_url=document_url,
                source_url=RHTP_UPDATES_PAGE,
                keywords=keywords,
                raw={"href": link.href, "date": nearby_date},
            )
        )
    emit(progress, f"MI RHTP updates: scanned {scanned}, kept {len(records)}")
    return records


def fetch_sitemap_dates(*, progress: Callable[[str], None] | None) -> dict[str, str]:
    markup = fetch_text(SITEMAP_URL)
    root = ET.fromstring(markup.encode("utf-8"))
    ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    dates: dict[str, str] = {}
    for item in root.findall("sm:url", ns):
        loc = item.findtext("sm:loc", default="", namespaces=ns)
        lastmod = item.findtext("sm:lastmod", default="", namespaces=ns)
        if loc and lastmod:
            dates[strip_query(loc)] = iso_date_text(lastmod)
    emit(progress, f"MI sitemap: scanned {len(dates)} URLs for lastmod metadata")
    return dates


def date_after_link(markup: str, href: str) -> str:
    short_href = href.replace("https://www.michigan.gov", "")
    no_scheme_href = href.replace("https://", "")
    for needle in (href, no_scheme_href, short_href):
        if not needle:
            continue
        index = markup.find(needle)
        if index == -1:
            continue
        return iso_date_text(first_date_text(markup[index : index + 1000]))
    return ""
