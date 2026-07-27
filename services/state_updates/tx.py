from __future__ import annotations

import datetime as dt
import html as html_lib
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

from services.state_updates import sort_key, state_update_record

TMHP_NEWS_URL = "https://www.tmhp.com/news"
USER_AGENT = "soe-group3-tx-state-updates/0.1"
MAX_TMHP_PAGES = 5

REJECTED_HHS_URLS = [
    "https://www.hhs.texas.gov/providers/communications/provider-alerts",
    "https://www.hhs.texas.gov/laws-regulations/policies-rules/waivers",
    "https://www.hhs.texas.gov/laws-regulations/policies-rules/medicaid-chip-state-plan",
]

POLICY_TERMS = [
    "medicaid",
    "chip",
    "hhsc",
    "tmhp",
    "provider letter",
    "provider manual",
    "provider enrollment",
    "claims",
    "reprocessing",
    "prior authorization",
    "procedure code",
    "diagnosis code",
    "rate",
    "billing",
    "benefit",
    "coverage",
    "eligibility",
    "enrollment",
    "managed care",
    "mco",
    "evv",
    "electronic visit verification",
    "texmedconnect",
    "iamonline",
    "steps",
    "critical incident management",
    "long-term care",
    "ltc",
    "icf/iid",
    "waiver",
    "1915",
    "cshcn",
    "healthcare",
    "cms",
    "oasis",
]

TRAINING_ONLY_TERMS = [
    "webinar",
    "recording available",
    "educational opportunities",
    "educational video",
    "training materials",
    "training opportunity",
    "training set",
    "conference",
    "register for",
    "workshop",
]

def fetch_updates(
    *,
    keywords: list[str],
    max_records: int,
    progress: Callable[[str], None] | None = None,
) -> list[dict[str, str]]:
    limit = max(1, max_records)
    records = fetch_tmhp_news(keywords=keywords, max_records=limit, progress=progress)
    return sorted(records, key=sort_key, reverse=True)[:limit]


def fetch_tmhp_news(
    *,
    keywords: list[str],
    max_records: int,
    progress: Callable[[str], None] | None = None,
) -> list[dict[str, str]]:
    first_html = http_text(TMHP_NEWS_URL)
    max_page = min(max_pager_page(first_html), MAX_TMHP_PAGES - 1)
    pages = [first_html]
    for page in range(1, max_page + 1):
        pages.append(http_text(f"{TMHP_NEWS_URL}?created=2&page={page}"))
        time.sleep(0.1)

    items = []
    for page_html in pages:
        items.extend(parse_news_items(page_html))

    records: list[dict[str, str]] = []
    seen: set[str] = set()
    scanned = 0
    for item in items:
        scanned += 1
        title = item["title"]
        if not is_policy_update_title(title, keywords):
            continue
        detail = fetch_detail(item["document_url"])
        text = " ".join([title, detail.get("summary", "")])
        source_record_id = slug_from_url(item["document_url"])
        record = state_update_record(
            state="TX",
            source="tx_tmhp_news",
            source_record_id=source_record_id,
            record_type=record_type_for(text),
            title=title,
            agency="Texas Medicaid & Healthcare Partnership / Texas HHSC",
            summary=detail.get("summary", "") or "TMHP provider news item in Texas Medicaid program context.",
            posted_date=item["posted_date"],
            updated_date=detail.get("updated_date", "") or item["posted_date"],
            effective_date=extract_effective_date(text, item["posted_date"]),
            document_url=item["document_url"],
            source_url=TMHP_NEWS_URL,
            keywords=keywords,
            raw={
                "list_datetime": item["posted_date"],
                "program_context": "Texas Medicaid provider news from TMHP, the official Texas Medicaid & Healthcare Partnership site.",
                "detail_status": detail.get("status", ""),
                "source_note": "Official TMHP dated news listing; TXSmartBuy/ESBD solicitations are intentionally excluded.",
                "rejected_hhs_urls": [
                    {"url": url, "reason": "HTTP 403 Access Denied from CLI with standard and browser User-Agent probes."}
                    for url in REJECTED_HHS_URLS
                ],
            },
        )
        if not record["matched_keywords"] and not has_policy_term(text):
            continue
        record_id = record.get("id", "")
        if record_id in seen:
            continue
        seen.add(record_id)
        records.append(record)
        if len(records) >= max_records:
            break

    emit(progress, f"TX TMHP news: scanned {scanned} dated rows, normalized {len(records)} records")
    return records[:max_records]


def parse_news_items(html: str) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    pattern = re.compile(
        r"<li>\s*<span class=\"views-field views-field-title\">.*?"
        r"<a href=\"([^\"]+)\"[^>]*>(.*?)</a>.*?"
        r"<time datetime=\"([^\"]+)\">(.*?)</time>.*?</li>",
        flags=re.IGNORECASE | re.DOTALL,
    )
    for match in pattern.finditer(html):
        href, title_html, posted, _label_date = match.groups()
        title = clean_text(strip_tags(title_html), 500)
        if not title:
            continue
        items.append(
            {
                "title": title,
                "posted_date": posted,
                "document_url": urllib.parse.urljoin(TMHP_NEWS_URL, html_lib.unescape(href)),
            }
        )
    return items


def fetch_detail(url: str) -> dict[str, str]:
    try:
        html = http_text(url, timeout=30)
    except Exception as exc:
        return {"status": f"detail fetch failed: {exc}"}
    summary = meta_content(html, "description")
    updated_date = ""
    match = re.search(r"Last updated on.*?<time datetime=\"([^\"]+)\"", html, flags=re.IGNORECASE | re.DOTALL)
    if match:
        updated_date = match.group(1)
    return {"summary": summary, "updated_date": updated_date, "status": "ok"}


def is_policy_update_title(title: str, keywords: list[str]) -> bool:
    lower = title.lower()
    if any(term in lower for term in TRAINING_ONLY_TERMS):
        return False
    return has_keyword(title, keywords) or has_policy_term(title)


def has_policy_term(text: str) -> bool:
    lower = text.lower()
    return any(term in lower for term in POLICY_TERMS)


def has_keyword(text: str, keywords: list[str]) -> bool:
    lower = text.lower()
    return any(keyword.strip().lower() in lower for keyword in keywords if keyword.strip())


def record_type_for(text: str) -> str:
    lower = text.lower()
    if "public comment" in lower:
        return "public_comment_notice"
    if "state plan amendment" in lower or re.search(r"\bspa\b", lower):
        return "spa_notice"
    if "waiver" in lower or "1915" in lower:
        return "waiver_notice"
    if "provider letter" in lower or "provider manual" in lower:
        return "provider_bulletin"
    if any(term in lower for term in ["claims", "procedure code", "diagnosis code", "rate", "billing", "prior authorization", "evv", "provider enrollment", "managed care"]):
        return "provider_bulletin"
    if any(term in lower for term in ["iamonline", "texmedconnect", "portal", "system outage", "steps"]):
        return "guidance"
    return "guidance"


def extract_effective_date(text: str, posted_date: str) -> str:
    match = re.search(
        r"\bEffective\s+(?:on\s+)?"
        r"(Jan(?:uary)?\.?|Feb(?:ruary)?\.?|Mar(?:ch)?\.?|Apr(?:il)?\.?|May|Jun(?:e)?\.?|Jul(?:y)?\.?|Aug(?:ust)?\.?|Sep(?:tember)?\.?|Oct(?:ober)?\.?|Nov(?:ember)?\.?|Dec(?:ember)?\.?)"
        r"\s+(\d{1,2})(?:,\s*(20\d{2}))?",
        text,
        flags=re.IGNORECASE,
    )
    if not match:
        return ""
    month = month_number(match.group(1))
    day = int(match.group(2))
    posted = parse_iso_date(posted_date)
    year = int(match.group(3)) if match.group(3) else (posted.year if posted else dt.date.today().year)
    if posted and not match.group(3) and month < posted.month:
        year += 1
    try:
        return dt.date(year, month, day).isoformat()
    except ValueError:
        return ""


def max_pager_page(html: str) -> int:
    pages = [int(value) for value in re.findall(r"[?&]page=(\d+)", html)]
    return max(pages) if pages else 0


def http_text(url: str, timeout: int = 60) -> str:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    request = urllib.request.Request(url, headers=headers)
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            body = exc.read(600).decode("utf-8", "replace")
            raise RuntimeError(f"HTTP {exc.code} from {url}: {body}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            last_error = exc
            time.sleep(1 + attempt)
    raise RuntimeError(f"TX request failed: {last_error}")


def meta_content(html: str, name: str) -> str:
    pattern = rf'<meta\s+name="{re.escape(name)}"\s+content="([^"]*)"'
    match = re.search(pattern, html, flags=re.IGNORECASE)
    return clean_text(match.group(1), 1200) if match else ""


def strip_tags(value: str) -> str:
    return re.sub(r"<[^>]+>", " ", value)


def slug_from_url(url: str) -> str:
    path = urllib.parse.urlparse(url).path.rstrip("/")
    return path.rsplit("/", 1)[-1] or clean_text(url, 120)


def month_number(value: str) -> int:
    key = value.lower().replace(".", "")[:3]
    months = {
        "jan": 1,
        "feb": 2,
        "mar": 3,
        "apr": 4,
        "may": 5,
        "jun": 6,
        "jul": 7,
        "aug": 8,
        "sep": 9,
        "oct": 10,
        "nov": 11,
        "dec": 12,
    }
    return months.get(key, 1)


def parse_iso_date(value: str) -> dt.date | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def clean_text(value: Any, limit: int) -> str:
    text = html_lib.unescape(str(value or ""))
    text = text.replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[: max(0, limit - 3)].rstrip() + "..."


def emit(progress: Callable[[str], None] | None, message: str) -> None:
    if progress:
        progress(message)
