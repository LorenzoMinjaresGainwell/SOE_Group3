from __future__ import annotations

import datetime as dt
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from typing import Any, Callable

from services.state_normalization import clean_text, compact_raw_json, iso_date, keyword_hits, parse_date, stable_id, term_matches

BASE_URL = "https://pr-webs-vendor.des.wa.gov/"
LEGACY_SOURCE_URL = "https://fortress.wa.gov/ga/webs/"
BID_CALENDAR_URL = urllib.parse.urljoin(BASE_URL, "BidCalendar.aspx")
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
SOURCE_NOTE = "Official WEBS public BidCalendar.aspx ASP.NET table; detail links require vendor login, so public row text is normalized."
MAX_PAGES = 8
BLOCK_RE = re.compile(r'(?is)<table[^>]+width=["\']550px["\'][^>]*>.*?</table>\s*<HR[^>]*>')
TAG_RE = re.compile(r"(?is)<[^>]+>")
URL_RE = re.compile(r"https?://[^\s<>'\"]+")
PAGE_TARGET_RE = re.compile(r"__doPostBack\(&#39;(DataGrid1\$_ctl29\$_ctl\d+)&#39;,&#39;&#39;\)")


class WebsClient:
    def __init__(self) -> None:
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor())

    def get_calendar(self) -> str:
        return self._open(BID_CALENDAR_URL, data=None, referer=LEGACY_SOURCE_URL)

    def post_page(self, base_html: str, event_target: str) -> str:
        form = extract_form_fields(base_html)
        form.update(
            {
                "__EVENTTARGET": event_target,
                "__EVENTARGUMENT": "",
                "ddlOrgNames": form.get("ddlOrgNames") or "0",
            }
        )
        data = urllib.parse.urlencode(form).encode("utf-8")
        return self._open(BID_CALENDAR_URL, data=data, referer=BID_CALENDAR_URL)

    def _open(self, url: str, *, data: bytes | None, referer: str) -> str:
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": referer,
        }
        if data is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        last_error: Exception | None = None
        for attempt in range(3):
            request = urllib.request.Request(url, data=data, headers=headers)
            try:
                with self.opener.open(request, timeout=60) as response:
                    return response.read().decode("utf-8", "replace")
            except urllib.error.HTTPError as exc:
                body = exc.read(600).decode("utf-8", "replace")
                raise RuntimeError(f"HTTP {exc.code} from {url}: {body}") from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = exc
                time.sleep(1 + attempt)
        raise RuntimeError(f"WA WEBS request failed: {last_error}")


class FormParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.fields: dict[str, str] = {}
        self.current_select: str | None = None
        self.select_has_value: dict[str, bool] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        data = attrs_dict(attrs)
        if tag == "input":
            name = data.get("name")
            if name and ((data.get("type") or "").lower() == "hidden" or "value" in data):
                self.fields[name] = data.get("value") or ""
        elif tag == "select":
            name = data.get("name")
            self.current_select = name
            if name:
                self.fields.setdefault(name, "")
                self.select_has_value[name] = False
        elif tag == "option" and self.current_select:
            selected = "selected" in data
            if selected or not self.select_has_value.get(self.current_select, False):
                self.fields[self.current_select] = data.get("value") or ""
                self.select_has_value[self.current_select] = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "select":
            self.current_select = None


def fetch_opportunities(
    *,
    keywords: list[str],
    days_back: int,
    max_records: int,
    progress: Callable[[str], None] | None = None,
) -> list[dict[str, str]]:
    client = WebsClient()
    pages = fetch_calendar_pages(client, progress=progress)
    rows = dedupe_rows([row for html in pages for row in parse_calendar_rows(html)])
    emit(progress, f"WA WEBS BidCalendar.aspx: {len(rows)} public rows across {len(pages)} pages")

    records: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in rows:
        record = normalize_calendar_row(row, keywords=keywords)
        if not record.get("source_record_id") or record["id"] in seen:
            continue
        if record["status"].lower() == "cancelled":
            continue
        if not is_open_or_recent(record["posted_date"], record["due_date"], days_back):
            continue
        if keywords and not record["matched_keywords"]:
            continue
        if not useful_keyword_match(record["matched_keywords"].split(";"), record["raw_json"]):
            continue
        seen.add(record["id"])
        records.append(record)

    return sorted(records, key=record_sort_key, reverse=True)[: max(1, max_records)]


def fetch_calendar_pages(client: WebsClient, *, progress: Callable[[str], None] | None = None) -> list[str]:
    initial = client.get_calendar()
    pages = [initial]
    targets = parse_page_targets(initial)[: max(0, MAX_PAGES - 1)]
    for target in targets:
        try:
            pages.append(client.post_page(initial, target))
        except Exception as exc:
            emit(progress, f"WA page target {target} failed: {exc}")
            break
    return pages


def parse_calendar_rows(html: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for match in BLOCK_RE.finditer(html):
        block = match.group(0)
        link_match = re.search(r'id="DataGrid1__ctl\d+_DetailsHyperlink"\s+href="([^"]+)"[^>]*>(.*?)</a>', block, re.IGNORECASE | re.DOTALL)
        if not link_match:
            continue
        detail_href = clean_url(link_match.group(1))
        title = strip_html(link_match.group(2), 500)
        full_text = strip_html(block, 3000)
        description = first_match(block, r'<td\s+colspan="2"\s+class="text-small"\s+width="330px"[^>]*>(.*?)</td>', 1500)
        detail_id = query_value(detail_href, "ID")
        rows.append(
            {
                "detail_id": detail_id,
                "detail_url": urllib.parse.urljoin(BID_CALENDAR_URL, detail_href),
                "title": title,
                "source_record_id": first_match(block, r"<b>\s*Ref #:\s*</b>\s*([^<]+)", 160) or detail_id,
                "contact": first_match(block, r'id="DataGrid1__ctl\d+_Label4"[^>]*>(.*?)</span>', 160),
                "due_date": first_match(block, r'id="DataGrid1__ctl\d+_Label1"[^>]*>(.*?)</span>', 80),
                "amendment_date": first_match(block, r'id="DataGrid1__ctl\d+_lblAmendmentDate"[^>]*>(.*?)</span>', 80),
                "description": description,
                "pre_bid_conference": label_value(block, "PreBidConference"),
                "questions_deadline": label_value(block, "QAPeriod"),
                "inclusion_plan": label_value(block, "InclusionPlan"),
                "external_urls": ";".join(extract_urls(description)),
                "full_text": full_text,
            }
        )
    return rows


def normalize_calendar_row(row: dict[str, str], *, keywords: list[str]) -> dict[str, str]:
    detail_id = clean_text(row.get("detail_id"), 80)
    source_record_id = clean_text(row.get("source_record_id") or detail_id, 180)
    title = clean_text(row.get("title") or source_record_id, 500)
    description = clean_text(row.get("description"), 1500)
    agency = infer_agency(" ".join([title, description, row.get("full_text", "")]))
    posted_date = iso_date(row.get("amendment_date"))
    due_date = iso_date(row.get("due_date"))
    status = status_from_text(" ".join([title, description]), due_date)
    external_urls = [url for url in row.get("external_urls", "").split(";") if url]
    document_url = external_urls[0] if external_urls else clean_text(row.get("detail_url"), 500)
    search_text = " ".join(
        [
            source_record_id,
            title,
            description,
            agency,
            clean_text(row.get("contact"), 160),
            clean_text(row.get("pre_bid_conference"), 120),
            clean_text(row.get("questions_deadline"), 120),
            clean_text(row.get("full_text"), 3000),
        ]
    )
    matched = keyword_hits(search_text, keywords)
    raw = {
        "source_key": "wa_webs",
        "source_note": SOURCE_NOTE,
        "detail_id": detail_id,
        "ref_number": source_record_id,
        "title": title,
        "description": description,
        "contact": clean_text(row.get("contact"), 160),
        "agency_inferred": agency,
        "due_date_raw": clean_text(row.get("due_date"), 80),
        "amendment_date_raw": clean_text(row.get("amendment_date"), 80),
        "pre_bid_conference": clean_text(row.get("pre_bid_conference"), 120),
        "questions_deadline": clean_text(row.get("questions_deadline"), 120),
        "inclusion_plan": clean_text(row.get("inclusion_plan"), 20),
        "external_urls": external_urls,
    }

    return {
        "id": stable_id("WA", detail_id or source_record_id, prefix="wa-webs-bid"),
        "state": "WA",
        "source": "Washington WEBS Bid Calendar",
        "source_record_id": source_record_id,
        "title": title,
        "agency": agency,
        "document_type": document_type(source_record_id, title, description),
        "posted_date": posted_date,
        "due_date": due_date,
        "status": status,
        "amount": "",
        "document_url": document_url,
        "source_url": BID_CALENDAR_URL,
        "matched_keywords": ";".join(matched),
        "relevance_score": str(relevance_score(matched, status, search_text, due_date)),
        "raw_json": compact_raw_json(raw),
        "last_checked_at": now_iso(),
    }


def parse_page_targets(html: str) -> list[str]:
    targets: list[str] = []
    seen: set[str] = set()
    for match in PAGE_TARGET_RE.finditer(html):
        target = match.group(1)
        if target in seen:
            continue
        seen.add(target)
        targets.append(target)
    return targets


def dedupe_rows(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in rows:
        key = row.get("detail_id") or row.get("source_record_id") or row.get("title", "")
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(row)
    return result


def document_type(source_record_id: str, title: str, description: str) -> str:
    text = " ".join([source_record_id, title, description]).upper()
    if code_matches(text, "RFI"):
        return "WEBS Request for Information"
    if code_matches(text, "RFQQ"):
        return "WEBS Request for Qualifications and Quotations"
    if code_matches(text, "RFQ"):
        return "WEBS Request for Quote"
    if code_matches(text, "RFP"):
        return "WEBS Request for Proposal"
    if code_matches(text, "IFB"):
        return "WEBS Invitation for Bid"
    if "BONFIRE" in text or "COURTESY NOTIFICATION" in text:
        return "WEBS Courtesy Solicitation Notice"
    return "WEBS Solicitation"


def status_from_text(text: str, due_date: str) -> str:
    if any(term in text.lower() for term in ["cancelled", "canceled"]):
        return "Cancelled"
    due = parse_date(due_date)
    if due and due < dt.date.today():
        return "Closed"
    return "Open"


def infer_agency(text: str) -> str:
    cleaned = clean_text(text, 3000)
    if re.search(r"\bDSHS\b", cleaned, re.IGNORECASE):
        return "Department of Social & Health Services"
    patterns = [
        r"\b(Health Care Authority)\b",
        r"\b(Dept\.? of Social & Health Services)\b",
        r"\b(Department of Social & Health Services)\b",
        r"\b(Department of Health)\b",
        r"\b(Department of Children Youth and Families)\b",
        r"\b(Dept\.? of Enterprise Services)\b",
        r"\b(Enterprise Services \(DES\), Dept\. of)\b",
        r"\b(UW Medicine)\b",
        r"\b(University of Washington(?: Medicine)?)\b",
        r"\b(Washington Technology Solutions \(WaTech\))\b",
        r"\b(Commerce Office of Economic Development and Competitiveness)\b",
        r"\b(Pacific Mountain Workforce Development Council)\b",
        r"\b(Department of Natural Resources)\b",
        r"\b(Department of Veterans Affairs)\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, cleaned, re.IGNORECASE)
        if match:
            return clean_text(match.group(1), 180)
    return ""


def useful_keyword_match(matches: list[str], text: str) -> bool:
    generic_terms = {"claims", "eligibility", "enrollment"}
    matched_terms = {match.lower() for match in matches if match}
    if not matched_terms or not matched_terms <= generic_terms:
        return True
    context_terms = [
        "health care authority",
        "department of social and health services",
        "department of social & health services",
        "department of health",
        "healthcare",
        "health care",
        "medicaid",
        "medicare",
        "medical",
        "hospital",
        "behavioral",
        "managed care",
        "provider",
        "chip",
        "mmis",
        "hca",
        "dshs",
    ]
    return any(term_matches(text, term) for term in context_terms)


def relevance_score(matches: list[str], status: str, text: str, due_date: str) -> int:
    score = min(50, len([match for match in matches if match]) * 10)
    if any(term_matches(text, term) for term in ["Medicaid", "MMIS", "Health Care Authority", "Department of Social & Health Services", "DSHS", "Department of Health"]):
        score += 25
    if any(term_matches(text, term) for term in ["eligibility", "claims", "enrollment", "managed care", "interoperability", "FHIR", "prior authorization", "provider data"]):
        score += 15
    if term_matches(text, "rural health"):
        score += 25
    if any(term_matches(text, term) for term in ["RFP", "RFI", "RFQ", "software", "data", "cloud", "platform", "services"]):
        score += 12
    if status.lower() == "open":
        score += 10
    due = parse_date(due_date)
    if due and due >= dt.date.today():
        score += 8
    return min(score, 100)


def is_open_or_recent(posted_date: str, due_date: str, days_back: int) -> bool:
    due = parse_date(due_date)
    if due and due >= dt.date.today():
        return True
    if days_back <= 0:
        return True
    posted = parse_date(posted_date)
    return not posted or (dt.date.today() - posted).days <= days_back


def record_sort_key(row: dict[str, str]) -> tuple[int, str, str]:
    return (int_or_zero(row.get("relevance_score")), row.get("due_date", ""), row.get("posted_date", ""))


def extract_form_fields(html: str) -> dict[str, str]:
    parser = FormParser()
    parser.feed(html)
    return dict(parser.fields)


def first_match(html: str, pattern: str, limit: int) -> str:
    match = re.search(pattern, html, re.IGNORECASE | re.DOTALL)
    return strip_html(match.group(1), limit) if match else ""


def label_value(html: str, label_name: str) -> str:
    pattern = rf'id="DataGrid1__ctl\d+_{re.escape(label_name)}CaptionLabel"[^>]*>.*?</span>\s*<span[^>]*id="DataGrid1__ctl\d+_{re.escape(label_name)}Label"[^>]*>(.*?)</span>'
    return first_match(html, pattern, 120)


def strip_html(value: str, limit: int) -> str:
    return clean_text(TAG_RE.sub(" ", value), limit)


def clean_url(value: str) -> str:
    return clean_text(value, 500).strip().strip("'\"")


def extract_urls(text: str) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for match in URL_RE.finditer(text):
        url = match.group(0).rstrip(".,);]")
        if url in seen:
            continue
        seen.add(url)
        urls.append(url)
    return urls


def query_value(url: str, key: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    values = urllib.parse.parse_qs(parsed.query).get(key)
    return clean_text(values[0], 80) if values else ""


def attrs_dict(attrs: list[tuple[str, str | None]]) -> dict[str, str]:
    return {key: value or "" for key, value in attrs}


def code_matches(text: str, code: str) -> bool:
    return re.search(rf"(?<![A-Z0-9]){re.escape(code)}(?![A-Z0-9])", text, re.IGNORECASE) is not None


def int_or_zero(value: Any) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def emit(progress: Callable[[str], None] | None, message: str) -> None:
    if progress:
        progress(message)
