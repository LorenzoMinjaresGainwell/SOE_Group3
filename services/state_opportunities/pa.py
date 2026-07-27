from __future__ import annotations

import datetime as dt
import html as html_lib
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from typing import Any, Callable

EMARKETPLACE_BASE_URL = "https://www.emarketplace.state.pa.us/"
SEARCH_URL = urllib.parse.urljoin(EMARKETPLACE_BASE_URL, "Search.aspx")
BID_AWARD_URL = urllib.parse.urljoin(EMARKETPLACE_BASE_URL, "BidAward.aspx")
EP_SEARCH_URL = urllib.parse.urljoin(EMARKETPLACE_BASE_URL, "EP_Search.aspx")
DHS_RHTP_URL = "https://www.pa.gov/agencies/dhs/programs-services/healthcare/rural-health/rhtp-funding-opportunities"
USER_AGENT = "soe-group3-pa-opportunities/0.1"

SOURCE_NOTES = {
    "Search.aspx": "Stable enough for current solicitations; ASP.NET WebForms with ViewState, parsed from grdResults.",
    "BidAward.aspx": "Stable enough for recent award watch; ASP.NET WebForms with ViewState, parsed from gvDGSBidAwards.",
    "EP_Search.aspx": "Stable enough for current emergency procurements; archived search should be manual/later.",
    "BidContracts.aspx": "HTML-only contract listing. Existing OpenBookPA contract JSON scraper is preferred for contracts.",
    "DHS RHTP": "Useful Medicaid/rural-health funding page. HTML-only, parsed conservatively for dated funding windows.",
}


def fetch_opportunities(
    *,
    keywords: list[str],
    days_back: int,
    max_records: int,
    progress: Callable[[str], None] | None = None,
) -> list[dict[str, str]]:
    limit = max(max_records, 1)
    records: list[dict[str, str]] = []
    seen: set[str] = set()

    fetchers = [
        fetch_current_solicitations,
        fetch_recent_bid_awards,
        fetch_current_emergency_procurements,
        fetch_dhs_rhtp_funding,
    ]
    for fetcher in fetchers:
        try:
            batch = fetcher(keywords=keywords, days_back=days_back, max_records=limit, progress=progress)
        except Exception as exc:  # Keep one PA source failure from hiding others.
            emit(progress, f"PA: {fetcher.__name__} failed: {exc}")
            continue
        for record in batch:
            record_id = record.get("id", "")
            if not record_id or record_id in seen:
                continue
            seen.add(record_id)
            records.append(record)

    return sorted(records, key=record_sort_key, reverse=True)[:limit]


def fetch_current_solicitations(
    *,
    keywords: list[str],
    days_back: int,
    max_records: int,
    progress: Callable[[str], None] | None = None,
) -> list[dict[str, str]]:
    client = WebFormsClient()
    html = client.get(SEARCH_URL)
    form = extract_form_fields(html)
    form.update(
        {
            "ctl00$MainBody$ddlRows": "32767",
            "ctl00$MainBody$rdoArch": "0",
            "ctl00$MainBody$btnSearch": "Search",
        }
    )
    html = client.post(SEARCH_URL, form)
    rows = parse_grid(html, "ctl00_MainBody_grdResults")
    records: list[dict[str, str]] = []

    for cells in rows:
        if len(cells) < 11 or cells[0].text.lower().startswith("solicitation"):
            continue
        source_record_id = first_token(cells[0].text)
        if not source_record_id or not source_record_id[0].isdigit():
            continue
        document_url = first_href(cells[0], SEARCH_URL) or urllib.parse.urljoin(
            EMARKETPLACE_BASE_URL, "Solicitations.aspx?" + urllib.parse.urlencode({"SID": source_record_id})
        )
        title = expanded_text(cells[2].text, 500)
        description = expanded_text(cells[3].text, 1500)
        agency = clean_text(cells[4].text, 180)
        document_type = clean_text(cells[1].text or "Solicitation", 80)
        posted_date = iso_date(cells[7].text or cells[6].text)
        due_date = iso_date(cells[8].text or cells[9].text)
        status = clean_text(cells[10].text or "Open", 80)
        search_text = " ".join([title, description, agency, document_type, status, cells[5].text])
        matched = keyword_hits(search_text, keywords)
        if not matched or not useful_keyword_match(matched, search_text) or not is_open_or_recent(posted_date, due_date, days_back):
            continue

        raw = {
            "solicitation_number": source_record_id,
            "type": document_type,
            "title": title,
            "description": description,
            "agency": agency,
            "county": clean_text(cells[5].text, 120),
            "amended_date": iso_date(cells[6].text),
            "solicitation_start_date": posted_date,
            "solicitation_due_date": due_date,
            "bid_opening_date": iso_date(cells[9].text),
            "status": status,
            "contact": clean_text(cells[11].text if len(cells) > 11 else "", 200),
        }
        records.append(
            opportunity_record(
                source_record_id=source_record_id,
                source="PA eMarketplace Current Solicitations",
                title=title,
                agency=agency,
                document_type=document_type,
                posted_date=posted_date,
                due_date=due_date,
                status=status,
                amount="",
                document_url=document_url,
                source_url=SEARCH_URL,
                matched=matched,
                score=relevance_score(matched, document_type, status, title + " " + description, ""),
                raw=raw,
                id_prefix="pa-emarketplace-solicitation",
            )
        )

    emit(progress, f"PA eMarketplace Search.aspx: {len(records)} keyword-matched current solicitations")
    return records[:max_records]


def fetch_recent_bid_awards(
    *,
    keywords: list[str],
    days_back: int,
    max_records: int,
    progress: Callable[[str], None] | None = None,
) -> list[dict[str, str]]:
    client = WebFormsClient()
    html = client.get(BID_AWARD_URL)
    form = extract_form_fields(html)
    form.update(
        {
            "ctl00$MainBody$ddlPages": page_size_value(max_records),
            "ctl00$MainBody$grprdo": "rdoOpen",
            "ctl00$MainBody$btnSearch": "Search",
        }
    )
    html = client.post(BID_AWARD_URL, form)
    rows = parse_grid(html, "ctl00_MainBody_gvDGSBidAwards")
    records: list[dict[str, str]] = []

    for cells in rows:
        if len(cells) < 7 or cells[0].text.lower().startswith("purchase order"):
            continue
        tokens = cells[0].text.split()
        contract_no = tokens[0] if tokens else ""
        record_no = tokens[-1] if len(tokens) > 1 and tokens[-1].isdigit() else contract_no
        if not contract_no:
            continue
        posted_date = iso_date(cells[1].text)
        if not within_days(posted_date, days_back):
            continue
        bid_no = clean_text(cells[2].text, 120)
        title = expanded_text(cells[3].text, 500)
        agency = clean_text(cells[4].text, 180)
        awarded_to = clean_text(cells[5].text, 180)
        amount = normalize_amount(cells[6].text)
        status = "Cancelled" if awarded_to.lower() == "cancelled" else "Awarded"
        search_text = " ".join([contract_no, bid_no, title, agency, awarded_to, amount])
        matched = keyword_hits(search_text, keywords)
        if not matched or not useful_keyword_match(matched, search_text):
            continue
        document_url = urllib.parse.urljoin(
            EMARKETPLACE_BASE_URL, "BidAwardDetails.aspx?" + urllib.parse.urlencode({"RecordNo": record_no})
        )
        raw = {
            "purchase_order_or_contract_no": contract_no,
            "record_no": record_no,
            "posted_date": posted_date,
            "bid_no": bid_no,
            "short_description": title,
            "agency": agency,
            "awarded_to": awarded_to,
            "dollar_amount": amount,
            "source_note": "Open awards only; PA archives awards after 90 days.",
        }
        records.append(
            opportunity_record(
                source_record_id=record_no,
                source="PA eMarketplace Bid Awards",
                title=title,
                agency=agency,
                document_type="Bid Award",
                posted_date=posted_date,
                due_date="",
                status=status,
                amount=amount,
                document_url=document_url,
                source_url=BID_AWARD_URL,
                matched=matched,
                score=relevance_score(matched, "Bid Award", status, search_text, amount),
                raw=raw,
                id_prefix="pa-emarketplace-award",
            )
        )

    emit(progress, f"PA eMarketplace BidAward.aspx: {len(records)} keyword-matched recent awards")
    return records[:max_records]


def fetch_current_emergency_procurements(
    *,
    keywords: list[str],
    days_back: int,
    max_records: int,
    progress: Callable[[str], None] | None = None,
) -> list[dict[str, str]]:
    client = WebFormsClient()
    html = client.get(EP_SEARCH_URL)
    form = extract_form_fields(html)
    form.update(
        {
            "ctl00$MainBody$chkPage": "on",
            "ctl00$MainBody$grpArchive": "rdoNew",
            "ctl00$MainBody$btnSearch": "Search",
        }
    )
    html = client.post(EP_SEARCH_URL, form)
    rows = parse_grid(html, "ctl00_MainBody_gdvSearchData")
    records: list[dict[str, str]] = []

    for cells in rows:
        if len(cells) < 7 or cells[0].text.lower() == "id":
            continue
        source_record_id = first_token(cells[0].text)
        if not source_record_id or not source_record_id.isdigit():
            continue
        posted_date = iso_date(cells[1].text)
        if not within_days(posted_date, days_back):
            continue
        title = expanded_text(cells[2].text, 500)
        agency = clean_text(cells[3].text, 180)
        supplier = clean_text(cells[4].text, 180)
        status = clean_text(cells[5].text, 80)
        search_text = " ".join([source_record_id, title, agency, supplier, status])
        matched = keyword_hits(search_text, keywords)
        if not matched or not useful_keyword_match(matched, search_text):
            continue
        document_url = first_href(cells[-1], EP_SEARCH_URL) or urllib.parse.urljoin(
            EMARKETPLACE_BASE_URL, "EP_Details.aspx?" + urllib.parse.urlencode({"id": source_record_id})
        )
        raw = {
            "id": source_record_id,
            "date_approved": posted_date,
            "description": title,
            "agency": agency,
            "proposed_supplier": supplier,
            "status": status,
            "extensions": clean_text(cells[6].text, 40),
        }
        records.append(
            opportunity_record(
                source_record_id=source_record_id,
                source="PA eMarketplace Emergency Procurements",
                title=title,
                agency=agency,
                document_type="Emergency Procurement",
                posted_date=posted_date,
                due_date="",
                status=status,
                amount="",
                document_url=document_url,
                source_url=EP_SEARCH_URL,
                matched=matched,
                score=relevance_score(matched, "Emergency Procurement", status, search_text, ""),
                raw=raw,
                id_prefix="pa-emarketplace-emergency",
            )
        )

    emit(progress, f"PA eMarketplace EP_Search.aspx: {len(records)} keyword-matched current emergency procurements")
    return records[:max_records]


def fetch_dhs_rhtp_funding(
    *,
    keywords: list[str],
    days_back: int,
    max_records: int,
    progress: Callable[[str], None] | None = None,
) -> list[dict[str, str]]:
    html = http_text(DHS_RHTP_URL)
    decoded_html = decoded_page_html(html)
    text = decoded_page_text(decoded_html)
    links = unique_urls(clean_url(href) for href, _text in extract_links(decoded_html) if "forms.cloud.microsoft" in href)
    link_by_marker = {
        "EHR & HIO": links[0] if links else DHS_RHTP_URL,
        "FQHC & FQHC Look-Alikes": links[1] if len(links) > 1 else (links[0] if links else DHS_RHTP_URL),
    }
    records: list[dict[str, str]] = []

    for marker in ["EHR & HIO", "FQHC & FQHC Look-Alikes"]:
        segment = segment_after(text, marker, 2800)
        if "Application Dates:" not in segment or "Total Available Funding:" not in segment:
            continue
        title = clean_text(text_before(segment, "Application Dates:"), 500)
        title = title.replace("\u2014", "-").replace("\u2013", "-")
        dates = text_between(segment, "Application Dates:", "Total Available Funding:")
        amount_text = text_between(segment, "Total Available Funding:", "Qualified Entities:")
        entities = clean_fragment_text(text_between(segment, "Qualified Entities:", "Resources"), 2000)
        posted_date, due_date = parse_application_dates(dates)
        if not is_open_or_recent(posted_date, due_date, days_back):
            continue
        amount = normalize_amount(amount_text)
        status = status_for_window(posted_date, due_date)
        search_text = " ".join([title, dates, amount_text, entities, text[:1200]])
        matched = keyword_hits(search_text, keywords)
        if not matched or not useful_keyword_match(matched, search_text):
            continue
        source_record_id = "dhs-rhtp-fqhc-ehr-hio-2026" if marker.startswith("FQHC") else "dhs-rhtp-ehr-hio-2026"
        raw = {
            "title": title,
            "application_dates": clean_text(dates, 400),
            "total_available_funding": clean_text(amount_text, 160),
            "qualified_entities": clean_fragment_text(entities, 600),
            "source_note": SOURCE_NOTES["DHS RHTP"],
        }
        records.append(
            opportunity_record(
                source_record_id=source_record_id,
                source="PA DHS Rural Health Transformation Plan Funding Opportunities",
                title=title,
                agency="Department of Human Services",
                document_type="DHS Funding Opportunity",
                posted_date=posted_date,
                due_date=due_date,
                status=status,
                amount=amount,
                document_url=link_by_marker[marker],
                source_url=DHS_RHTP_URL,
                matched=matched,
                score=relevance_score(matched, "DHS Funding Opportunity", status, search_text, amount),
                raw=raw,
                id_prefix="pa-dhs-rhtp",
            )
        )

    emit(progress, f"PA DHS RHTP funding page: {len(records)} keyword-matched funding opportunities")
    return records[:max_records]


class WebFormsClient:
    def __init__(self) -> None:
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor())

    def get(self, url: str) -> str:
        return self._open(url, None, url)

    def post(self, url: str, form: dict[str, str]) -> str:
        data = urllib.parse.urlencode(form).encode("utf-8")
        return self._open(url, data, url)

    def _open(self, url: str, data: bytes | None, referer: str) -> str:
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
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
            except (urllib.error.URLError, TimeoutError) as exc:
                last_error = exc
                time.sleep(1 + attempt)
        raise RuntimeError(f"PA request failed: {last_error}")


def http_text(url: str, data: bytes | None = None, headers: dict[str, str] | None = None, timeout: int = 60) -> str:
    request_headers = {"User-Agent": USER_AGENT}
    if headers:
        request_headers.update(headers)
    request = urllib.request.Request(url, data=data, headers=request_headers)
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


class FormParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.fields: dict[str, str] = {}
        self.current_select: str | None = None
        self.current_option: dict[str, Any] | None = None
        self.selects: dict[str, list[dict[str, Any]]] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        data = attrs_dict(attrs)
        if tag == "input":
            name = data.get("name")
            input_type = (data.get("type") or "").lower()
            if not name:
                return
            if input_type in {"hidden", "text"}:
                self.fields[name] = data.get("value") or ""
            elif input_type in {"radio", "checkbox"} and "checked" in data:
                self.fields[name] = data.get("value") or "on"
        elif tag == "select":
            name = data.get("name")
            self.current_select = name
            if name:
                self.selects[name] = []
        elif tag == "option" and self.current_select:
            self.current_option = {"value": data.get("value") or "", "selected": "selected" in data, "text": ""}

    def handle_data(self, data: str) -> None:
        if self.current_option is not None:
            self.current_option["text"] += data

    def handle_endtag(self, tag: str) -> None:
        if tag == "option" and self.current_option is not None and self.current_select:
            self.selects[self.current_select].append(self.current_option)
            self.current_option = None
        elif tag == "select":
            self.current_select = None


def extract_form_fields(html: str) -> dict[str, str]:
    parser = FormParser()
    parser.feed(html)
    fields = dict(parser.fields)
    for name, options in parser.selects.items():
        selected = next((option for option in options if option["selected"]), options[0] if options else None)
        if selected is not None:
            fields[name] = str(selected["value"])
    return fields


class GridCell:
    def __init__(self) -> None:
        self.parts: list[str] = []
        self.hrefs: list[str] = []

    @property
    def text(self) -> str:
        return clean_text(" ".join(self.parts), 2000)


class GridParser(HTMLParser):
    def __init__(self, table_id: str) -> None:
        super().__init__()
        self.table_id = table_id
        self.in_table = False
        self.table_depth = 0
        self.current_row: list[GridCell] | None = None
        self.current_cell: GridCell | None = None
        self.rows: list[list[GridCell]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        data = attrs_dict(attrs)
        if tag == "table":
            if self.in_table:
                self.table_depth += 1
            elif data.get("id") == self.table_id:
                self.in_table = True
                self.table_depth = 1
        if not self.in_table:
            return
        if tag == "tr":
            self.current_row = []
        elif tag in {"td", "th"} and self.current_row is not None:
            self.current_cell = GridCell()
        elif tag == "a" and self.current_cell is not None:
            href = data.get("href")
            if href:
                self.current_cell.hrefs.append(href)
        elif tag == "br" and self.current_cell is not None:
            self.current_cell.parts.append(" ")

    def handle_data(self, data: str) -> None:
        if self.current_cell is not None:
            self.current_cell.parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if not self.in_table:
            return
        if tag in {"td", "th"} and self.current_cell is not None and self.current_row is not None:
            self.current_row.append(self.current_cell)
            self.current_cell = None
        elif tag == "tr" and self.current_row is not None:
            if self.current_row:
                self.rows.append(self.current_row)
            self.current_row = None
        elif tag == "table":
            self.table_depth -= 1
            if self.table_depth <= 0:
                self.in_table = False


def parse_grid(html: str, table_id: str) -> list[list[GridCell]]:
    parser = GridParser(table_id)
    parser.feed(html)
    return parser.rows


class LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[tuple[str, str]] = []
        self.current_href: str | None = None
        self.current_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            href = attrs_dict(attrs).get("href")
            if href:
                self.current_href = href
                self.current_text = []

    def handle_data(self, data: str) -> None:
        if self.current_href is not None:
            self.current_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self.current_href is not None:
            self.links.append((self.current_href, clean_text(" ".join(self.current_text), 200)))
            self.current_href = None
            self.current_text = []


def extract_links(html: str) -> list[tuple[str, str]]:
    parser = LinkParser()
    parser.feed(html)
    return parser.links


def opportunity_record(
    *,
    source_record_id: str,
    source: str,
    title: str,
    agency: str,
    document_type: str,
    posted_date: str,
    due_date: str,
    status: str,
    amount: str,
    document_url: str,
    source_url: str,
    matched: list[str],
    score: int,
    raw: dict[str, Any],
    id_prefix: str,
) -> dict[str, str]:
    return {
        "id": f"{id_prefix}-{slug_id(source_record_id)}",
        "state": "PA",
        "source": source,
        "source_record_id": source_record_id,
        "title": title,
        "agency": agency,
        "document_type": document_type,
        "posted_date": posted_date,
        "due_date": due_date,
        "status": status,
        "amount": amount,
        "document_url": document_url,
        "source_url": source_url,
        "matched_keywords": ";".join(matched),
        "relevance_score": str(score),
        "raw_json": json.dumps(raw, ensure_ascii=False, sort_keys=True),
        "last_checked_at": now_iso(),
    }


def keyword_hits(text: str, keywords: list[str]) -> list[str]:
    hits: set[str] = set()
    for keyword in keywords:
        term = keyword.strip()
        if not term:
            continue
        pattern = re.escape(term).replace(r"\ ", r"\s+")
        if re.fullmatch(r"[A-Za-z0-9 ]+", term):
            pattern = rf"(?<![A-Za-z0-9]){pattern}(?![A-Za-z0-9])"
        if re.search(pattern, text, flags=re.IGNORECASE):
            hits.add(keyword)
    return sorted(hits, key=str.lower)


def useful_keyword_match(matches: list[str], text: str) -> bool:
    generic_terms = {"claims", "eligibility", "enrollment"}
    matched_terms = {match.lower() for match in matches}
    if not matched_terms <= generic_terms:
        return True
    lower = text.lower()
    context_terms = [
        "department of human services",
        "prior dpw",
        "office of income maintenance",
        "long term living",
        "medical",
        "medicaid",
        "medicare",
        "health",
        "hospital",
        "behavioral",
        "omap",
        "chip",
        "patient",
    ]
    return any(term in lower for term in context_terms)


def relevance_score(matches: list[str], document_type: str, status: str, text: str, amount: str) -> int:
    lower = text.lower()
    score = min(50, len(matches) * 10)
    if any(term in lower for term in ["medicaid", "mmis", "medical assistance"]):
        score += 25
    if any(term in lower for term in ["eligibility", "claims", "enrollment"]):
        score += 15
    if any(term in lower for term in ["rural health", "rhtp", "rural health transformation"]):
        score += 25
    if any(term in document_type.upper() for term in ["RFP", "RFA", "RFI", "RFQ", "ITQ", "SFP"]):
        score += 12
    if status.lower() in {"open", "upcoming", "current", "approved", "awarded"}:
        score += 10
    amount_value = int_or_zero(amount)
    if amount_value >= 1_000_000:
        score += 8
    elif amount_value >= 100_000:
        score += 4
    return min(score, 100)


def record_sort_key(row: dict[str, str]) -> tuple[int, str, str, int]:
    return (
        int_or_zero(row.get("relevance_score")),
        row.get("due_date", ""),
        row.get("posted_date", ""),
        int_or_zero(row.get("amount")),
    )


def is_open_or_recent(posted_date: str, due_date: str, days_back: int) -> bool:
    due = parse_date(due_date)
    if due and due >= dt.date.today():
        return True
    return within_days(posted_date, days_back)


def within_days(value: str, days_back: int) -> bool:
    if days_back <= 0:
        return True
    parsed = parse_date(value)
    if not parsed:
        return True
    return (dt.date.today() - parsed).days <= days_back


def status_for_window(posted_date: str, due_date: str) -> str:
    today = dt.date.today()
    start = parse_date(posted_date)
    due = parse_date(due_date)
    if start and start > today:
        return "Upcoming"
    if due and due < today:
        return "Closed"
    return "Open"


def parse_application_dates(value: str) -> tuple[str, str]:
    dates = re.findall(r"[A-Z][a-z]+\s+\d{1,2},\s+\d{4}", value)
    start = iso_date(dates[0]) if dates else ""
    due = iso_date(dates[1]) if len(dates) > 1 else ""
    return start, due


def parse_date(value: Any) -> dt.date | None:
    if not value:
        return None
    text = clean_text(value, 80)
    numeric = re.search(r"\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b", text)
    long_date = re.search(r"\b[A-Z][a-z]+\s+\d{1,2},\s+\d{4}\b", text)
    if numeric:
        text = numeric.group(0)
    elif long_date:
        text = long_date.group(0)
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%m-%d-%Y", "%m-%d-%y", "%B %d, %Y", "%b %d, %Y"):
        try:
            return dt.datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    return None


def iso_date(value: Any) -> str:
    parsed = parse_date(value)
    return parsed.isoformat() if parsed else ""


def normalize_amount(value: Any) -> str:
    text = clean_text(value, 120).replace("$", "").replace(",", "")
    match = re.search(r"(\d+(?:\.\d+)?)", text)
    if not match:
        return ""
    amount = float(match.group(1))
    lower = text.lower()
    if "billion" in lower:
        amount *= 1_000_000_000
    elif "million" in lower:
        amount *= 1_000_000
    elif "thousand" in lower:
        amount *= 1_000
    return str(int(amount))


def page_size_value(max_records: int) -> str:
    if max_records <= 10:
        return "10"
    if max_records <= 20:
        return "20"
    if max_records <= 50:
        return "50"
    return "100"


def first_token(value: str) -> str:
    tokens = value.split()
    return tokens[0] if tokens else ""


def first_href(cell: GridCell, base_url: str) -> str:
    for href in cell.hrefs:
        if href and href != "#" and not href.lower().startswith("javascript:"):
            return urllib.parse.urljoin(base_url, href)
    return ""


def clean_url(value: str) -> str:
    return html_lib.unescape(value).replace('\\"', '"').strip().strip('"\'')


def unique_urls(values: Any) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value and value not in seen:
            seen.add(value)
            urls.append(value)
    return urls


def decoded_page_html(html: str) -> str:
    text = html
    for _ in range(3):
        text = html_lib.unescape(text)
    return text


def decoded_page_text(html: str) -> str:
    text = html.replace("\\r", " ").replace("\\n", " ").replace("\\t", " ")
    text = re.sub(r"<[^>]+>", " ", text)
    return clean_text(text, 120000)


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
    return clean_text(text[start_index:end_index], 2000)


def attrs_dict(attrs: list[tuple[str, str | None]]) -> dict[str, str]:
    return {key: value or "" for key, value in attrs}


def slug_id(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip()).strip("-").lower()
    return slug or "unknown"


def int_or_zero(value: str | None) -> int:
    try:
        return int(float(value or 0))
    except ValueError:
        return 0


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def expanded_text(value: Any, limit: int) -> str:
    text = clean_text(value, limit * 2)
    marker = " ... "
    if marker in text:
        before, after = text.split(marker, 1)
        prefix = before[: min(len(before), 24)].lower()
        if after and after.lower().startswith(prefix):
            text = after
    return clean_text(text, limit)


def clean_fragment_text(value: Any, limit: int) -> str:
    text = str(value or "").split('"}}">', 1)[0]
    return clean_text(text, limit)


def clean_text(value: Any, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "..."


def emit(progress: Callable[[str], None] | None, message: str) -> None:
    if progress:
        progress(message)
