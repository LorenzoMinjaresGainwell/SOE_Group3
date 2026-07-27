from __future__ import annotations

import re
import time
from typing import Callable

from services.state_http import fetch_url
from services.state_updates import emit, state_update_record
from services.state_updates.common import absolute_url, clean_text, first_date_text, iso_date_text, matches_keywords_or_context, parse_links, record_type_for, source_id_from_url, unique_records

AGENCY = "TennCare"
PUBLIC_NOTICES_URL = "https://www.tn.gov/tenncare/policy-guidelines/waiver-and-state-plan-public-notices.html"
NEWS_URL = "https://www.tn.gov/tenncare/news.html"
PROVIDER_NEWS_FORMS_URL = "https://www.tn.gov/tenncare/providers/tenncare-provider-news-notices-forms.html"
USER_AGENT = "Mozilla/5.0 (compatible; soe-group3-tn-state-updates/0.1)"
CONTEXT_TERMS = [
    "tenncare",
    "medicaid",
    "cms",
    "state plan",
    "state plan amendment",
    "waiver",
    "1115 demonstration",
    "1915(c)",
    "public notice",
    "public comment",
    "managed care",
    "quality improvement strategy",
    "rural health clinic",
    "federally qualified health center",
    "eligibility",
    "claims",
    "provider",
]
SKIP_SUPPLEMENTAL_PREFIXES = ("draft ", "draft version", "redline ", "2025 draft")


def fetch_updates(
    *,
    keywords: list[str],
    max_records: int,
    progress: Callable[[str], None] | None = None,
) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    for fetcher in (fetch_public_notices, fetch_news_items):
        try:
            records.extend(fetcher(keywords=keywords, progress=progress))
        except Exception as exc:  # Keep one TennCare source failure from hiding another.
            emit(progress, f"TN: {fetcher.__name__} failed: {exc}")
    output = unique_records(records)
    emit(progress, f"TN: normalized {len(output)} records from official TennCare update sources")
    return output[:max_records]


def fetch_public_notices(*, keywords: list[str], progress: Callable[[str], None] | None) -> list[dict[str, str]]:
    markup = fetch_tn_text(PUBLIC_NOTICES_URL)
    records: list[dict[str, str]] = []
    scanned = 0
    for block in rte_blocks(markup):
        block_text = clean_text(block)
        block_date = iso_date_text(first_date_text(block_text))
        links = parse_links(block, PUBLIC_NOTICES_URL)
        if not links:
            continue
        for link in links:
            title = clean_text(link.text)
            if not is_notice_link(title):
                continue
            scanned += 1
            link_date = iso_date_text(first_date_text(title))
            posted_date = link_date or block_date
            if not posted_date:
                continue
            search_text = " ".join([title, block_text, "TennCare Medicaid waiver state plan public notice"])
            if not matches_keywords_or_context(search_text, keywords, CONTEXT_TERMS):
                continue
            document_url = absolute_url(PUBLIC_NOTICES_URL, link.href)
            records.append(
                state_update_record(
                    state="TN",
                    source="tn_tenncare_public_notices",
                    source_record_id=source_id_from_url(document_url) or f"notice:{posted_date}:{title[:80]}",
                    record_type=tn_record_type(search_text),
                    title=title,
                    agency=AGENCY,
                    summary=block_text[:1200] or "TennCare waiver/state plan public notice.",
                    posted_date=posted_date,
                    effective_date=effective_date_from_text(search_text),
                    comment_required="public comment" in block_text.lower(),
                    document_url=document_url,
                    source_url=PUBLIC_NOTICES_URL,
                    keywords=keywords,
                    raw={"source_page": PUBLIC_NOTICES_URL, "block_text": block_text[:2000]},
                )
            )
    emit(progress, f"TN TennCare waiver/state-plan public notices: scanned {scanned}, kept {len(records)}")
    return records


def fetch_news_items(*, keywords: list[str], progress: Callable[[str], None] | None) -> list[dict[str, str]]:
    markup = fetch_tn_text(NEWS_URL)
    records: list[dict[str, str]] = []
    scanned = 0
    seen: set[str] = set()
    for link in parse_links(markup, NEWS_URL):
        title = clean_text(link.text)
        if not title or title.lower() == "read full story" or link.href in seen:
            continue
        posted_date = date_from_news_url(link.href)
        if not posted_date:
            continue
        seen.add(link.href)
        scanned += 1
        search_text = " ".join([title, "TennCare Medicaid CMS managed care waiver news"])
        if not matches_keywords_or_context(search_text, keywords, CONTEXT_TERMS):
            continue
        records.append(
            state_update_record(
                state="TN",
                source="tn_tenncare_news",
                source_record_id=source_id_from_url(link.href),
                record_type=record_type_for(title, "policy_update"),
                title=title,
                agency=AGENCY,
                summary="Official TennCare news item relevant to Medicaid/CMS program policy.",
                posted_date=posted_date,
                document_url=link.href,
                source_url=NEWS_URL,
                keywords=keywords,
                raw={"source_page": NEWS_URL},
            )
        )
    emit(progress, f"TN TennCare news: scanned {scanned}, kept {len(records)}")
    return records


def rte_blocks(markup: str) -> list[str]:
    return re.findall(r'<div class="tn-rte">\s*(.*?)\s*</div>', markup, flags=re.IGNORECASE | re.DOTALL)


def is_notice_link(title: str) -> bool:
    lower = title.lower()
    if not title or lower.startswith(SKIP_SUPPLEMENTAL_PREFIXES):
        return False
    return any(term in lower for term in ("notice", "proposed", "responses to public comment", "waiver", "state plan amendment"))


def tn_record_type(text: str) -> str:
    lower = text.lower()
    if "state plan" in lower or re.search(r"\bspa\b", lower):
        return "spa_notice"
    if "waiver" in lower or "1115" in lower or "1915" in lower or "demonstration" in lower:
        return "waiver_notice"
    if "public notice" in lower or "public comment" in lower or "public forum" in lower:
        return "public_comment_notice"
    return record_type_for(text, "policy_update")


def effective_date_from_text(text: str) -> str:
    match = re.search(r"\beffective\s+(?:on\s+)?([A-Z][a-z]+\s+\d{1,2},\s+20\d{2})", text, flags=re.IGNORECASE)
    return iso_date_text(match.group(1)) if match else ""


def date_from_news_url(url: str) -> str:
    match = re.search(r"/news/(20\d{2})/(\d{1,2})/(\d{1,2})/", url)
    if not match:
        return ""
    year, month, day = match.groups()
    return f"{year}-{int(month):02d}-{int(day):02d}"


def fetch_tn_text(url: str, timeout: int = 35) -> str:
    headers = {"Accept": "text/html,application/xhtml+xml,*/*", "Accept-Language": "en-US,en;q=0.9"}
    last_error = ""
    for attempt in range(3):
        result = fetch_url(url, headers=headers, timeout=timeout, byte_limit=1_500_000, user_agent=USER_AGENT)
        if result.ok:
            return result.body_text()
        last_error = result.error or f"HTTP {result.status_code}"
        time.sleep(1 + attempt)
    raise RuntimeError(f"TennCare request failed for {url}: {last_error}")
