from __future__ import annotations

import datetime as dt
import html as html_lib
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from typing import Any, Callable

from services.state_updates import sort_key, state_update_record

DHS_RHTP_URL = "https://www.pa.gov/agencies/dhs/programs-services/healthcare/rural-health/rhtp-funding-opportunities"
DHS_MEDICAID_CHANGES_URL = "https://www.pa.gov/agencies/dhs/resources/medicaid/medicaid-changes"
DHS_BULLETIN_SEARCH_URL = "https://www.pa.gov/agencies/dhs/resources/for-providers/bulletin-search"
DHS_PROVIDER_QUICK_TIPS_URL = "https://www.pa.gov/agencies/dhs/resources/for-providers/providers-quick-tips"
USER_AGENT = "soe-group3-pa-state-updates/0.1"

RHTP_SOURCES = [
    {
        "marker": "Upcoming EHR & HIO",
        "source_record_id": "rhtp-ehr-hio-2026",
        "document_hint": "EHR.HIO Program Payment Certification Webform",
        "notice_hint": "56-28/987.html",
    },
    {
        "marker": "FQHC & FQHC Look-Alikes",
        "source_record_id": "rhtp-fqhc-ehr-hio-2026",
        "document_hint": "FQHC EHR.HIO Program Payment Certification Webform",
        "notice_hint": "56-27/949.html",
    },
    {
        "marker": "Rapid Response Stabilization: RHTP Payments",
        "source_record_id": "rhtp-rapid-response-stabilization-2026",
        "document_hint": "Rapid Response Stabilization Program Payment Certification",
        "notice_hint": "56-17/595.html",
    },
]


def fetch_updates(
    *,
    keywords: list[str],
    max_records: int,
    progress: Callable[[str], None] | None = None,
) -> list[dict[str, str]]:
    limit = max(1, max_records)
    records: list[dict[str, str]] = []
    seen: set[str] = set()

    for fetcher in (fetch_dhs_rhtp_updates, fetch_dhs_medicaid_changes):
        try:
            batch = fetcher(keywords=keywords, max_records=limit, progress=progress)
        except Exception as exc:  # Keep one PA source failure from hiding others.
            emit(progress, f"PA: {fetcher.__name__} failed: {exc}")
            continue
        for record in batch:
            record_id = record.get("id", "")
            if not record_id or record_id in seen:
                continue
            seen.add(record_id)
            records.append(record)
            if len(records) >= limit:
                break
        if len(records) >= limit:
            break

    return sorted(records, key=sort_key, reverse=True)[:limit]


def fetch_dhs_rhtp_updates(
    *,
    keywords: list[str],
    max_records: int,
    progress: Callable[[str], None] | None = None,
) -> list[dict[str, str]]:
    html = decoded_html(http_text(DHS_RHTP_URL))
    page_text = decoded_text(html)
    page_updated = latest_modify_date(html)
    links = extract_links(html, DHS_RHTP_URL)
    records: list[dict[str, str]] = []

    for config in RHTP_SOURCES:
        segment = segment_after(page_text, config["marker"], 4200)
        if "Application Dates:" not in segment or "Total Available Funding:" not in segment:
            continue
        title = normalize_title(text_before(segment, "Application Dates:"))
        dates_text = text_between(segment, "Application Dates:", "Total Available Funding:")
        amount_text = text_between(segment, "Total Available Funding:", "Qualified Entities:")
        entities_text = text_between(segment, "Qualified Entities:", "Resources")
        start_date, due_date = parse_application_dates(dates_text)
        document_url = find_link(links, str(config["document_hint"])) or DHS_RHTP_URL
        notice_url = find_link(links, str(config["notice_hint"]))
        summary = clean_text(
            " ".join(
                [
                    "Pennsylvania Rural Health Transformation Plan (RHTP) funding update.",
                    f"Application Dates: {dates_text}",
                    f"Total Available Funding: {amount_text}",
                    f"Qualified Entities: {entities_text}",
                ]
            ),
            1200,
        )
        raw = {
            "application_dates": dates_text,
            "total_available_funding": amount_text,
            "qualified_entities": entities_text,
            "program_context": "Pennsylvania Rural Health Transformation Plan (RHTP)",
            "public_notice_url": notice_url,
            "source_note": "Official PA DHS RHTP page with dated funding windows; not PA eMarketplace bids.",
        }
        record = state_update_record(
            state="PA",
            source="pa_dhs_rhtp",
            source_record_id=str(config["source_record_id"]),
            record_type="grant_notice",
            title=title,
            agency="Pennsylvania Department of Human Services",
            summary=summary,
            posted_date=page_updated,
            due_date=due_date,
            effective_date=start_date,
            action_required_by=due_date,
            document_url=document_url,
            source_url=DHS_RHTP_URL,
            keywords=keywords,
            raw=raw,
        )
        if record["matched_keywords"] or record["rht_flag"] == "true":
            records.append(record)

    emit(progress, f"PA DHS RHTP funding page: scanned {len(RHTP_SOURCES)} windows, normalized {len(records)} records")
    return records[:max_records]


def fetch_dhs_medicaid_changes(
    *,
    keywords: list[str],
    max_records: int,
    progress: Callable[[str], None] | None = None,
) -> list[dict[str, str]]:
    html = decoded_html(http_text(DHS_MEDICAID_CHANGES_URL))
    page_text = decoded_text(html)
    page_updated = latest_modify_date(html)
    title = page_title(html) or "Changes Coming to Medicaid in Pennsylvania in January 2027"
    title = title.replace(" | Department of Human Services | Commonwealth of Pennsylvania", "")
    summary = summary_after(page_text, "Because of this new federal law", 1100)
    if not summary:
        summary = summary_after(page_text, "Changes Coming to Medicaid", 1100)
    effective_date = first_long_date(page_text, default="January 1, 2027")
    raw = {
        "source_note": "Official PA DHS Medicaid policy page with effective-date milestones for 2027 Medicaid changes.",
        "rejected_related_sources": [
            {
                "url": DHS_BULLETIN_SEARCH_URL,
                "reason": "Coveo search UI exposed filter labels but no stable dated bulletin rows in static HTML.",
            },
            {
                "url": DHS_PROVIDER_QUICK_TIPS_URL,
                "reason": "Provider Quick Tips table has titles/PDF links and page modify date, but no row-level issue/effective dates in listing.",
            },
        ],
    }
    record = state_update_record(
        state="PA",
        source="pa_dhs_medicaid_changes",
        source_record_id="medicaid-changes-2027",
        record_type="policy_update",
        title=title,
        agency="Pennsylvania Department of Human Services",
        summary=summary,
        posted_date=page_updated,
        effective_date=effective_date,
        document_url=DHS_MEDICAID_CHANGES_URL,
        source_url=DHS_MEDICAID_CHANGES_URL,
        keywords=keywords,
        raw=raw,
    )
    records = [record] if record["matched_keywords"] else []
    emit(progress, f"PA DHS Medicaid changes page: scanned 1 page, normalized {len(records)} records")
    return records[:max_records]


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
    raise RuntimeError(f"PA request failed: {last_error}")


class LinkParser(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__()
        self.base_url = base_url
        self.links: list[tuple[str, str]] = []
        self.current_href: str | None = None
        self.current_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        data = {key: value or "" for key, value in attrs}
        href = data.get("href")
        if href:
            self.current_href = urllib.parse.urljoin(self.base_url, clean_url(href))
            self.current_text = []

    def handle_data(self, data: str) -> None:
        if self.current_href is not None:
            self.current_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self.current_href is not None:
            self.links.append((self.current_href, clean_text(" ".join(self.current_text), 240)))
            self.current_href = None
            self.current_text = []


def extract_links(html: str, base_url: str) -> list[tuple[str, str]]:
    parser = LinkParser(base_url)
    parser.feed(html)
    return parser.links


def find_link(links: list[tuple[str, str]], hint: str) -> str:
    lower_hint = hint.lower()
    for url, label in links:
        if lower_hint in label.lower() or lower_hint in url.lower():
            return url
    return ""


def decoded_html(value: str) -> str:
    text = value
    for _ in range(3):
        text = html_lib.unescape(text)
    return text


def decoded_text(value: str) -> str:
    text = value.replace("\\r", " ").replace("\\n", " ").replace("\\t", " ")
    text = re.sub(r"<[^>]+>", " ", text)
    return clean_text(text, 140000)


def latest_modify_date(html: str) -> str:
    values = re.findall(r'repo:modifyDate":"(\d{4}-\d{2}-\d{2}T[^"]+)', html)
    return sorted(set(values))[-1] if values else ""


def page_title(html: str) -> str:
    match = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    return clean_text(match.group(1), 300) if match else ""


def normalize_title(value: str) -> str:
    title = clean_text(value.replace("\u2014", "-").replace("\u2013", "-"), 500)
    title = re.sub(r"^(Upcoming|Past Funding Opportunities)\s+", "", title, flags=re.IGNORECASE)
    return title


def parse_application_dates(value: str) -> tuple[str, str]:
    dates = re.findall(r"\b[A-Z][a-z]+\s+\d{1,2},\s+\d{4}\b", value)
    if len(dates) < 2:
        dates = re.findall(r"\b[A-Z][a-z]+\s+\d{1,2}\b", value)
        year_match = re.search(r"\b(20\d{2})\b", value)
        year = year_match.group(1) if year_match else str(dt.date.today().year)
        dates = [f"{date}, {year}" if not re.search(r"\b20\d{2}\b", date) else date for date in dates]
    return iso_long_date(dates[0]) if dates else "", iso_long_date(dates[1]) if len(dates) > 1 else ""


def first_long_date(value: str, *, default: str = "") -> str:
    match = re.search(r"\b[A-Z][a-z]+\s+\d{1,2},\s+\d{4}\b", value)
    return iso_long_date(match.group(0) if match else default)


def iso_long_date(value: str) -> str:
    for fmt in ("%B %d, %Y", "%b %d, %Y"):
        try:
            return dt.datetime.strptime(value.strip().replace(".", ""), fmt).date().isoformat()
        except ValueError:
            pass
    return ""


def segment_after(text: str, marker: str, limit: int) -> str:
    index = text.find(marker)
    return "" if index < 0 else text[index : index + limit]


def text_before(text: str, marker: str) -> str:
    index = text.find(marker)
    return text if index < 0 else text[:index]


def text_between(text: str, start: str, end: str) -> str:
    start_index = text.find(start)
    if start_index < 0:
        return ""
    start_index += len(start)
    end_index = text.find(end, start_index)
    if end_index < 0:
        end_index = len(text)
    return clean_text(text[start_index:end_index], 1600)


def summary_after(text: str, marker: str, limit: int) -> str:
    index = text.find(marker)
    if index < 0:
        return ""
    return clean_text(text[index : index + limit], limit)


def clean_url(value: str) -> str:
    return html_lib.unescape(value).replace('\\"', '"').strip().strip("\"'")


def clean_text(value: Any, limit: int) -> str:
    text = html_lib.unescape(str(value or ""))
    text = text.replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[: max(0, limit - 3)].rstrip() + "..."


def emit(progress: Callable[[str], None] | None, message: str) -> None:
    if progress:
        progress(message)
