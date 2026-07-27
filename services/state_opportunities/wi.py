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

from services.state_normalization import clean_text, compact_raw_json, iso_date, keyword_hits, parse_date, stable_id, term_matches

BASE_URL = "https://vendornet.wi.gov/"
BIDS_URL = urllib.parse.urljoin(BASE_URL, "Bids.aspx")
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
SOURCE_NOTE = (
    "Official Wisconsin VendorNet ASP.NET/Telerik public Bids.aspx table; initial grid and paging are exposed "
    "through standard Microsoft AJAX partial-postback responses."
)
PAGE_SIZE = 25
MAX_PAGES = 8
TAG_RE = re.compile(r"(?is)<[^>]+>")
ROW_RE = re.compile(r'<tr\b(?=[^>]*\bclass="rg(?:Alt)?Row")[^>]*>(.*?)</tr>', re.IGNORECASE | re.DOTALL)
NEXT_BUTTON_RE = re.compile(r'<button\b(?=[^>]*\btitle="Next Page")[^>]*>', re.IGNORECASE | re.DOTALL)
NAME_RE = re.compile(r'name="([^"]+)"', re.IGNORECASE)
LINK_RE = re.compile(r'<a\b[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.IGNORECASE | re.DOTALL)
CELL_RE = re.compile(r"<td\b[^>]*>(.*?)</td>", re.IGNORECASE | re.DOTALL)
CURRENT_PAGE_RE = re.compile(r'class="rgCurrentPage"[^>]*>(\d+)</a>', re.IGNORECASE)


class FormInputParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.fields: dict[str, str] = {}
        self.current_select: str | None = None
        self.select_has_value: dict[str, bool] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        data = attrs_dict(attrs)
        if tag == "input":
            name = data.get("name")
            if not name:
                return
            input_type = data.get("type", "text").lower()
            if input_type in {"checkbox", "radio"}:
                if "checked" not in data:
                    return
                self.fields[name] = data.get("value") or "on"
                return
            self.fields[name] = data.get("value") or ""
        elif tag == "select":
            name = data.get("name")
            self.current_select = name
            if name:
                self.fields.setdefault(name, "")
                self.select_has_value[name] = False
        elif tag == "option" and self.current_select:
            if "selected" in data or not self.select_has_value.get(self.current_select, False):
                self.fields[self.current_select] = data.get("value") or clean_text(data.get("text"))
                self.select_has_value[self.current_select] = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "select":
            self.current_select = None


class VendorNetClient:
    def __init__(self) -> None:
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor())
        self.fields: dict[str, str] = {}

    def initial_grid(self) -> str:
        page = self.request_text(BIDS_URL, referer=BASE_URL)
        self.fields = parse_form_inputs(page)
        return self.ajax_post(
            target="ctl00$RadAjaxManager1",
            argument="InitialPageLoad",
            script_left="ctl00$RadAjaxManager1SU",
        )

    def next_grid_page(self, current_html: str) -> str | None:
        target = next_page_target(current_html)
        if not target:
            return None
        return self.ajax_post(target=target, argument="")

    def ajax_post(self, *, target: str, argument: str, script_left: str = "ctl00$ctl00$MainContent$BidsGridPanel") -> str:
        payload = dict(self.fields)
        payload.update(
            {
                "ctl00$ScriptManager1": f"{script_left}|{target}",
                "__EVENTTARGET": target,
                "__EVENTARGUMENT": argument,
                "__ASYNCPOST": "true",
                "RadAJAXControlID": "ctl00_RadAjaxManager1",
            }
        )
        text = self.request_text(
            BIDS_URL,
            data=urllib.parse.urlencode(payload).encode("utf-8"),
            referer=BIDS_URL,
            accept="*/*",
            extra_headers={
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "Origin": BASE_URL.rstrip("/"),
                "X-MicrosoftAjax": "Delta=true",
                "X-Requested-With": "XMLHttpRequest",
            },
        )
        if text.startswith("0|error|"):
            raise RuntimeError(f"WI VendorNet AJAX postback failed: {text[:120]}")
        self.apply_delta(text)
        return text

    def apply_delta(self, text: str) -> None:
        for item_type, item_id, content in parse_delta_items(text):
            if item_type == "hiddenField":
                self.fields[item_id] = content
            elif item_type == "updatePanel":
                self.fields.update(parse_form_inputs(content))

    def request_text(
        self,
        url: str,
        *,
        data: bytes | None = None,
        referer: str,
        accept: str = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        extra_headers: dict[str, str] | None = None,
        timeout: int = 60,
    ) -> str:
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": accept,
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": referer,
        }
        headers.update(extra_headers or {})
        request = urllib.request.Request(url, data=data, headers=headers)
        try:
            with self.opener.open(request, timeout=timeout) as response:
                return response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            body = exc.read(800).decode("utf-8", "replace")
            raise RuntimeError(f"HTTP {exc.code} from WI VendorNet {url}: {body}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise RuntimeError(f"WI VendorNet request failed for {url}: {exc}") from exc


def fetch_opportunities(
    *,
    keywords: list[str],
    days_back: int,
    max_records: int,
    progress: Callable[[str], None] | None = None,
) -> list[dict[str, str]]:
    client = VendorNetClient()
    pages = fetch_grid_pages(client, max_records=max_records, progress=progress)
    rows = dedupe_rows([row for page in pages for row in parse_bid_rows(page)])
    emit(progress, f"WI VendorNet public bid rows: {len(rows)} rows across {len(pages)} pages")

    records: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in rows:
        record = normalize_bid_row(row, keywords=keywords)
        if not record.get("source_record_id") or record["id"] in seen:
            continue
        if record["status"].lower() in {"canceled", "cancelled"}:
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


def fetch_grid_pages(
    client: VendorNetClient,
    *,
    max_records: int,
    progress: Callable[[str], None] | None = None,
) -> list[str]:
    pages = [client.initial_grid()]
    emit(progress, f"WI VendorNet page {current_page_number(pages[-1])}: {len(parse_bid_rows(pages[-1]))} rows")
    scan_pages = min(MAX_PAGES, max(1, (max(max_records * 2, PAGE_SIZE * 4) + PAGE_SIZE - 1) // PAGE_SIZE))
    while len(pages) < scan_pages:
        try:
            next_page = client.next_grid_page(pages[-1])
        except Exception as exc:
            emit(progress, f"WI VendorNet next page failed: {exc}")
            break
        if not next_page:
            break
        pages.append(next_page)
        emit(progress, f"WI VendorNet page {current_page_number(next_page)}: {len(parse_bid_rows(next_page))} rows")
        time.sleep(0.15)
    return pages


def parse_delta_items(text: str) -> list[tuple[str, str, str]]:
    items: list[tuple[str, str, str]] = []
    index = 0
    length = len(text)
    while index < length:
        next_pipe = text.find("|", index)
        if next_pipe < 0:
            break
        try:
            item_length = int(text[index:next_pipe])
        except ValueError:
            index = next_pipe + 1
            continue
        type_end = text.find("|", next_pipe + 1)
        id_end = text.find("|", type_end + 1)
        if type_end < 0 or id_end < 0:
            break
        item_type = text[next_pipe + 1 : type_end]
        item_id = text[type_end + 1 : id_end]
        content_start = id_end + 1
        content_end = content_start + item_length
        if content_end > length or text[content_end : content_end + 1] != "|":
            index = next_pipe + 1
            continue
        items.append((item_type, item_id, text[content_start:content_end]))
        index = content_end + 1
    return items


def parse_form_inputs(markup: str) -> dict[str, str]:
    parser = FormInputParser()
    parser.feed(markup)
    return parser.fields


def parse_bid_rows(markup: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for match in ROW_RE.finditer(markup):
        block = match.group(1)
        cells = CELL_RE.findall(block)
        link_match = LINK_RE.search(block)
        if len(cells) < 5 or not link_match:
            continue
        href, link_text = link_match.groups()
        detail_url = urllib.parse.urljoin(BIDS_URL, html_lib.unescape(href))
        source_record_id = strip_html(link_text, 180)
        rows.append(
            {
                "source_record_id": source_record_id,
                "detail_url": detail_url,
                "detail_key": detail_key(detail_url),
                "title": strip_html(cells[1], 500),
                "agency": strip_html(cells[2], 180),
                "available_date": strip_html(cells[3], 80),
                "due_date": strip_html(cells[4], 80),
                "available_in_esupplier": "true" if e_supplier_available(cells[5] if len(cells) > 5 else "") else "false",
            }
        )
    return rows


def normalize_bid_row(row: dict[str, str], *, keywords: list[str]) -> dict[str, str]:
    source_record_id = clean_text(row.get("source_record_id"), 180)
    title = clean_text(row.get("title") or source_record_id, 500)
    agency = clean_text(row.get("agency"), 180)
    posted_date = iso_date(row.get("available_date"))
    due_date = iso_date(row.get("due_date"))
    status = status_from_due_date(due_date)
    detail_url = clean_text(row.get("detail_url"), 500)
    detail_key_value = clean_text(row.get("detail_key") or source_record_id, 180)
    search_text = expand_related_terms(" ".join([source_record_id, title, agency, status]))
    matched = keyword_hits(search_text, keywords)
    raw = {
        "source_key": "wi_vendornet",
        "source_note": SOURCE_NOTE,
        "solicitation_reference": source_record_id,
        "detail_key": detail_key_value,
        "detail_url": detail_url,
        "title": title,
        "agency": agency,
        "available_date_raw": clean_text(row.get("available_date"), 80),
        "due_date_raw": clean_text(row.get("due_date"), 80),
        "available_in_esupplier": row.get("available_in_esupplier") == "true",
    }

    return {
        "id": stable_id("WI", detail_key_value or source_record_id, prefix="wi-vendornet-bid"),
        "state": "WI",
        "source": "Wisconsin VendorNet Public Bids",
        "source_record_id": source_record_id,
        "title": title,
        "agency": agency,
        "document_type": document_type(source_record_id, title),
        "posted_date": posted_date,
        "due_date": due_date,
        "status": status,
        "amount": "",
        "document_url": detail_url,
        "source_url": BIDS_URL,
        "matched_keywords": ";".join(matched),
        "relevance_score": str(relevance_score(matched, status, search_text, due_date)),
        "raw_json": compact_raw_json(raw),
        "last_checked_at": now_iso(),
    }


def next_page_target(markup: str) -> str:
    for match in NEXT_BUTTON_RE.finditer(markup):
        name_match = NAME_RE.search(match.group(0))
        if name_match:
            return html_lib.unescape(name_match.group(1))
    return ""


def current_page_number(markup: str) -> str:
    match = CURRENT_PAGE_RE.search(markup)
    return match.group(1) if match else "?"


def detail_key(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qs(parsed.query)
    for key in ("Id", "id", "name"):
        value = clean_text((query.get(key) or [""])[0], 180)
        if value and value != "00000000-0000-0000-0000-000000000000":
            return value
    return clean_text(url, 180)


def e_supplier_available(cell_html: str) -> bool:
    lower = cell_html.lower()
    return "checked" in lower and "not available" not in lower


def document_type(source_record_id: str, title: str) -> str:
    text = " ".join([source_record_id, title]).upper()
    if code_matches(text, "RFI") or "REQUEST FOR INFORMATION" in text:
        return "VendorNet Request for Information"
    if code_matches(text, "RFP") or "REQUEST FOR PROPOSAL" in text or "REQUEST FOR PROPOSALS" in text:
        return "VendorNet Request for Proposal"
    if code_matches(text, "RFQ"):
        return "VendorNet Request for Quote"
    if code_matches(text, "RFB") or "REQUEST FOR BID" in text:
        return "VendorNet Request for Bid"
    if code_matches(text, "IFB") or code_matches(text, "ITB"):
        return "VendorNet Invitation for Bid"
    return "VendorNet Solicitation"


def expand_related_terms(text: str) -> str:
    expanded = text
    upper = text.upper()
    if "SCHOOL BASED HEALTH" in upper or "SBHC" in upper:
        expanded += " behavioral health health care"
    if "HEALTH SERVICES" in upper or "DEPT OF HEALTH" in upper:
        expanded += " health care medicaid"
    if "CHILDREN & FAMILIES" in upper or "CHILDREN AND FAMILIES" in upper:
        expanded += " health human services eligibility enrollment"
    return expanded


def useful_keyword_match(matches: list[str], text: str) -> bool:
    generic_terms = {"claims", "eligibility", "enrollment", "workforce"}
    matched_terms = {match.lower() for match in matches if match}
    if not matched_terms or not matched_terms <= generic_terms:
        return True
    context_terms = [
        "department of health services",
        "health services",
        "children & families",
        "children and families",
        "healthcare",
        "health care",
        "medicaid",
        "medicare",
        "medical",
        "behavioral",
        "managed care",
        "provider",
        "chip",
        "mmis",
        "dhs",
        "dcf",
        "hospital",
    ]
    return any(term_matches(text, term) for term in context_terms)


def relevance_score(matches: list[str], status: str, text: str, due_date: str) -> int:
    score = min(50, len([match for match in matches if match]) * 10)
    if any(term_matches(text, term) for term in ["Medicaid", "MMIS", "Department of Health Services", "Health Services", "Children & Families", "School Based Health"]):
        score += 25
    if any(term_matches(text, term) for term in ["eligibility", "claims", "enrollment", "managed care", "interoperability", "FHIR", "prior authorization", "provider data"]):
        score += 15
    if term_matches(text, "rural health"):
        score += 25
    if any(term_matches(text, term) for term in ["RFP", "RFI", "RFQ", "RFB", "software", "data", "cloud", "platform", "services"]):
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


def status_from_due_date(due_date: str) -> str:
    due = parse_date(due_date)
    if due and due < dt.date.today():
        return "Closed"
    return "Open"


def record_sort_key(row: dict[str, str]) -> tuple[int, str, str]:
    return (int_or_zero(row.get("relevance_score")), row.get("due_date", ""), row.get("posted_date", ""))


def dedupe_rows(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in rows:
        key = row.get("detail_key") or row.get("source_record_id") or row.get("detail_url", "")
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(row)
    return result


def strip_html(value: str, limit: int) -> str:
    return clean_text(TAG_RE.sub(" ", html_lib.unescape(value or "")), limit)


def code_matches(text: str, code: str) -> bool:
    return re.search(rf"(?<![A-Z0-9]){re.escape(code)}(?![A-Z0-9])", text, re.IGNORECASE) is not None


def attrs_dict(attrs: list[tuple[str, str | None]]) -> dict[str, str]:
    return {key: value or "" for key, value in attrs}


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
