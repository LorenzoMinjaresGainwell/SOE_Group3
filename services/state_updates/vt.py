from __future__ import annotations

import datetime as dt
import email.utils
import html
import json
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from typing import Any, Callable

from services.state_updates import emit, sort_key, state_update_record
from services.state_updates.common import absolute_url, clean_text, parse_links, record_type_for, unique_records

VT_MEDICAID_HOME = "https://www.vtmedicaid.com/"
VT_MEDICAID_ADVISORY_API = f"{VT_MEDICAID_HOME}api/advisory/"
VT_MEDICAID_BANNER_API = f"{VT_MEDICAID_HOME}api/banner/"
VT_MEDICAID_ADVISORY_PAGE = f"{VT_MEDICAID_HOME}#/advisory"
VT_MEDICAID_HOME_PAGE = f"{VT_MEDICAID_HOME}#/home"
DVHA_NEWS_FEED_URL = "https://dvha.vermont.gov/taxonomy/term/1/feed"
HCAR_PROPOSED_RULES_URL = "https://humanservices.vermont.gov/rules-policies/health-care-rules/health-care-administrative-rules-hcar/proposed-rules"
HBEE_RULES_URL = "https://humanservices.vermont.gov/rules-policies/health-care-rules/health-benefits-eligibility-and-enrollment-rules-hbee"
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
RECENT_YEARS = 2

VT_PROVIDER_POLICY_TERMS = [
    "vermont medicaid",
    "vt medicaid",
    "medicaid",
    "provider",
    "claim",
    "billing",
    "prior authorization",
    "fee schedule",
    "manual",
    "coverage",
    "eligibility",
    "enrollment",
    "revalidation",
    "reconsideration",
    "portal",
    "rate",
    "pharmacy",
    "dental",
    "behavioral",
    "hedis",
    "tpl",
    "evv",
    "aba",
    "dme",
    "nemt",
    "hcbs",
    "work requirement",
    "medicare",
]


def fetch_updates(
    *,
    keywords: list[str],
    max_records: int,
    progress: Callable[[str], None] | None = None,
) -> list[dict[str, str]]:
    limit = max(1, max_records)
    records: list[dict[str, str]] = []

    for fetcher in (fetch_provider_advisories, fetch_provider_banners, fetch_dvha_news, fetch_ahs_rule_notices):
        try:
            records.extend(fetcher(keywords=keywords, max_records=limit, progress=progress))
        except Exception as exc:
            emit(progress, f"VT: {fetcher.__name__} failed: {exc}")

    output = unique_records(records)
    emit(progress, f"VT: normalized {len(output)} total records")
    return sorted(output, key=sort_key, reverse=True)[:limit]


def fetch_provider_advisories(
    *,
    keywords: list[str],
    max_records: int,
    progress: Callable[[str], None] | None = None,
) -> list[dict[str, str]]:
    rows = [row for row in json_rows(VT_MEDICAID_ADVISORY_API) if recent_year(row.get("year"))]
    records: list[dict[str, str]] = []
    scanned = 0

    for row in sorted(rows, key=lambda item: str(item.get("date") or ""), reverse=True):
        topics = [clean_text(topic.get("value")) for topic in row.get("topics", []) if isinstance(topic, dict)]
        title = f"Vermont Medicaid {clean_text(row.get('month'))} {clean_text(row.get('year'))} Advisory"
        summary = clean_text("; ".join(topics))
        text = " ".join([title, summary])
        scanned += 1
        if not policy_context(text, keywords):
            continue
        year = int_or_zero(row.get("year"))
        pdf_url = f"{VT_MEDICAID_HOME}assets/advisories/{year}AdvisoryIndex.pdf" if year and year < dt.date.today().year else ""
        records.append(
            state_update_record(
                state="VT",
                source="vt_medicaid_provider_advisories",
                source_record_id=clean_text(row.get("_id")) or f"advisory:{row.get('date')}",
                record_type="provider_bulletin",
                title=title,
                agency="Department of Vermont Health Access / Vermont Medicaid Portal",
                summary=summary,
                posted_date=row.get("date", ""),
                document_url=pdf_url or VT_MEDICAID_ADVISORY_PAGE,
                source_url=VT_MEDICAID_ADVISORY_PAGE,
                keywords=keywords,
                raw={
                    "api_url": VT_MEDICAID_ADVISORY_API,
                    "topics": topics,
                    "source_note": "Official Vermont Medicaid Portal advisory JSON linked from DVHA provider resources.",
                },
            )
        )
        if len(records) >= max_records:
            break

    emit(progress, f"VT Medicaid advisories: scanned {scanned} current/prior-year rows, normalized {len(records)} records")
    return records[:max_records]


def fetch_provider_banners(
    *,
    keywords: list[str],
    max_records: int,
    progress: Callable[[str], None] | None = None,
) -> list[dict[str, str]]:
    rows = [row for row in json_rows(VT_MEDICAID_BANNER_API) if recent_year(row.get("year"))]
    records: list[dict[str, str]] = []
    scanned = 0

    for row in sorted(rows, key=lambda item: str(item.get("date") or ""), reverse=True):
        title = clean_text(row.get("topic"))
        summary = clean_text(row.get("view"),)
        text = " ".join([title, summary, groups_text(row.get("groups"))])
        scanned += 1
        if not title or not policy_context(text, keywords):
            continue
        document_url = first_url(summary) or VT_MEDICAID_HOME_PAGE
        records.append(
            state_update_record(
                state="VT",
                source="vt_medicaid_provider_banners",
                source_record_id=clean_text(row.get("_id")) or clean_text(row.get("entry")),
                record_type=provider_record_type(text),
                title=title,
                agency="Department of Vermont Health Access / Vermont Medicaid Portal",
                summary=summary,
                posted_date=row.get("date", ""),
                document_url=document_url,
                source_url=VT_MEDICAID_HOME_PAGE,
                keywords=keywords,
                raw={
                    "api_url": VT_MEDICAID_BANNER_API,
                    "entry": row.get("entry"),
                    "groups": row.get("groups"),
                    "source_note": "Official Vermont Medicaid Portal public banner/notices JSON; procurement rows are not used.",
                },
            )
        )
        if len(records) >= max_records:
            break

    emit(progress, f"VT Medicaid provider banners: scanned {scanned} current/prior-year rows, normalized {len(records)} records")
    return records[:max_records]


def fetch_dvha_news(
    *,
    keywords: list[str],
    max_records: int,
    progress: Callable[[str], None] | None = None,
) -> list[dict[str, str]]:
    markup = http_text(DVHA_NEWS_FEED_URL, accept="application/rss+xml,application/xml,text/xml,*/*")
    rows = rss_items(markup)
    records: list[dict[str, str]] = []
    scanned = 0

    for row in rows:
        text = " ".join([row.get("title", ""), row.get("summary", "")])
        scanned += 1
        if not policy_context(text, keywords):
            continue
        link = row.get("link", "")
        records.append(
            state_update_record(
                state="VT",
                source="vt_dvha_news_rss",
                source_record_id=source_id_from_url(link) or row.get("title", ""),
                record_type=record_type_for(text, default="policy_update"),
                title=row.get("title", ""),
                agency="Department of Vermont Health Access",
                summary=row.get("summary", ""),
                posted_date=row.get("posted_date", ""),
                document_url=link,
                source_url=DVHA_NEWS_FEED_URL,
                keywords=keywords,
                raw={"source_note": "Official DVHA news RSS feed with pubDate and item links."},
            )
        )
        if len(records) >= max_records:
            break

    emit(progress, f"VT DVHA news RSS: scanned {scanned} feed items, normalized {len(records)} records")
    return records[:max_records]


def fetch_ahs_rule_notices(
    *,
    keywords: list[str],
    max_records: int,
    progress: Callable[[str], None] | None = None,
) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    scanned = 0
    sources = [
        ("vt_ahs_hcar_proposed_rules", HCAR_PROPOSED_RULES_URL, "Health Care Administrative Rules proposed Medicaid rule"),
        ("vt_ahs_hbee_proposed_rules", HBEE_RULES_URL, "Health Benefits Eligibility and Enrollment proposed Medicaid rule"),
    ]

    for source, page_url, context in sources:
        markup, page_last_modified = http_text_with_last_modified(page_url)
        for link in parse_links(markup, page_url):
            if not accepted_rule_link(link.text, link.href, source):
                continue
            scanned += 1
            title = clean_text(link.text) or title_from_url(link.href)
            text = " ".join([title, context, link.href])
            if not policy_context(text, keywords):
                continue
            modified = head_last_modified(link.href) or page_last_modified
            records.append(
                state_update_record(
                    state="VT",
                    source=source,
                    source_record_id=source_id_from_url(link.href),
                    record_type="public_comment_notice",
                    title=title,
                    agency="Vermont Agency of Human Services",
                    summary=f"{context}; official AHS rule page link.",
                    posted_date=modified,
                    updated_date=modified,
                    comment_required=True,
                    document_url=link.href,
                    source_url=page_url,
                    keywords=keywords,
                    raw={
                        "source_page": page_url,
                        "page_last_modified": page_last_modified,
                        "source_note": "Official AHS HCAR/HBEE proposed-rule pages; Global Commitment Register links probed but returned 404 from CLI.",
                    },
                )
            )
            if len(records) >= max_records:
                emit(progress, f"VT AHS proposed Medicaid rules: scanned {scanned} links, normalized {len(records)} records")
                return records

    emit(progress, f"VT AHS proposed Medicaid rules: scanned {scanned} links, normalized {len(records)} records")
    return records[:max_records]


def json_rows(url: str) -> list[dict[str, Any]]:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json,text/plain,*/*",
            "Referer": VT_MEDICAID_HOME,
        },
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        data = json.loads(response.read().decode("utf-8", "replace"))
    return [row for row in data if isinstance(row, dict)] if isinstance(data, list) else []


def http_text(url: str, *, accept: str = "text/html,application/xhtml+xml,*/*") -> str:
    return http_text_with_last_modified(url, accept=accept)[0]


def http_text_with_last_modified(url: str, *, accept: str = "text/html,application/xhtml+xml,*/*") -> tuple[str, str]:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": accept,
            "Accept-Language": "en-US,en;q=0.9",
        },
    )
    with urllib.request.urlopen(request, timeout=45) as response:
        body = response.read().decode("utf-8", "replace")
        return body, iso_http_date(response.headers.get("Last-Modified", ""))


def head_last_modified(url: str) -> str:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "*/*", "Referer": referer_for(url)},
        method="HEAD",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return iso_http_date(response.headers.get("Last-Modified", ""))
    except OSError:
        return ""


def rss_items(markup: str) -> list[dict[str, str]]:
    try:
        root = ET.fromstring(markup)
    except ET.ParseError:
        return []
    rows = []
    for item in root.findall(".//item"):
        title = clean_text(child_text(item, "title"))
        link = clean_text(child_text(item, "link"))
        summary = clean_text(child_text(item, "description"),)
        posted_date = iso_http_date(child_text(item, "pubDate"))
        if title and link:
            rows.append({"title": title, "link": link, "summary": summary, "posted_date": posted_date})
    return rows


def child_text(item: ET.Element, name: str) -> str:
    child = item.find(name)
    return "" if child is None or child.text is None else html.unescape(child.text)


def accepted_rule_link(title: str, url: str, source: str) -> bool:
    lower = " ".join([title, url]).lower()
    if not url.lower().endswith(".pdf"):
        return False
    if source == "vt_ahs_hbee_proposed_rules":
        return "proposed" in lower or re.search(r"/26-0\d+", lower) is not None
    return any(term in lower for term in ("proposed", "estate recovery", "third party liability", "brain injury"))


def policy_context(text: str, keywords: list[str]) -> bool:
    lower = clean_text(text).lower()
    if any(str(keyword).strip().lower() in lower for keyword in keywords if str(keyword).strip()):
        return True
    return any(term in lower for term in VT_PROVIDER_POLICY_TERMS)


def provider_record_type(text: str) -> str:
    lower = text.lower()
    if "banner" in lower or "webinar" in lower:
        return "guidance"
    if any(term in lower for term in ("provider", "claim", "billing", "prior authorization", "fee schedule", "manual", "portal")):
        return "provider_bulletin"
    return record_type_for(text, default="guidance")


def recent_year(value: Any) -> bool:
    year = int_or_zero(value)
    return year >= dt.date.today().year - RECENT_YEARS + 1


def groups_text(value: Any) -> str:
    if not isinstance(value, list):
        return ""
    return " ".join(clean_text(item.get("description")) for item in value if isinstance(item, dict))


def first_url(value: str) -> str:
    match = re.search(r"https?://[^\s),.]+(?:\.[^\s),.]+)*", value)
    return clean_text(match.group(0))[:1000] if match else ""


def iso_http_date(value: str) -> str:
    try:
        parsed = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return ""
    return parsed.date().isoformat() if parsed else ""


def source_id_from_url(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    path = urllib.parse.unquote(parts.path.strip("/"))
    if parts.query:
        path = f"{path}?{parts.query}"
    return clean_text(path.replace("/", ":"))[:240]


def title_from_url(url: str) -> str:
    name = urllib.parse.urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1]
    name = re.sub(r"\.(pdf|html)$", "", urllib.parse.unquote(name), flags=re.I)
    return clean_text(re.sub(r"[_-]+", " ", name).title())


def referer_for(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, "/", "", ""))


def int_or_zero(value: Any) -> int:
    try:
        return int(float(str(value or "0")))
    except ValueError:
        return 0
