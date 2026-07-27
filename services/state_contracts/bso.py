from __future__ import annotations

import datetime as dt
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from html.parser import HTMLParser
from http.cookiejar import CookieJar
from typing import Any, Callable

from services.state_normalization import (
    amount_string,
    clean_text,
    compact_raw_json,
    iso_date,
    keyword_hits,
    months_until,
    parse_amount,
    parse_date,
    stable_id,
    term_matches,
)

BSO_ACTIVE_CONTRACTS_PATH = "view/search/external/advancedSearchContractBlanket.xhtml?view=activeContracts"
BSO_ACTIVE_CONTRACTS_POST_PATH = "view/search/external/advancedSearchContractBlanket.xhtml"
BSO_CONTRACT_RESULTS_TBODY_ID = "contractBlanketSearchResultsForm:contractResultId_data"
USER_AGENT = "soe-group3-bso-contracts/0.1"
PSEUDO_VENDOR_NAMES = {
    "",
    "conversion vendor",
    "multiple award vendors",
    "multiple vendors",
    "select vendor",
    "solicitation enabled",
}


@dataclass(frozen=True)
class BsoContractConfig:
    state: str
    source_name: str
    base_url: str
    source_key: str = ""
    source_note: str = "Public BSO active-contracts JSF route; searches current contract/blanket records, not open bids."

    @property
    def active_contracts_url(self) -> str:
        return urllib.parse.urljoin(self.base_url.rstrip("/") + "/", BSO_ACTIVE_CONTRACTS_PATH)

    @property
    def active_contracts_post_url(self) -> str:
        return urllib.parse.urljoin(self.base_url.rstrip("/") + "/", BSO_ACTIVE_CONTRACTS_POST_PATH)


@dataclass(frozen=True)
class BsoContractRow:
    cells: list["BsoCell"]

    def cell_text(self, index: int) -> str:
        return self.cells[index].text if index < len(self.cells) else ""

    def cell_href(self, index: int, base_url: str) -> str:
        if index >= len(self.cells):
            return ""
        for href in self.cells[index].hrefs:
            if href and href != "#" and not href.lower().startswith("javascript:"):
                return urllib.parse.urljoin(base_url, href)
        return ""


class BsoCell:
    def __init__(self) -> None:
        self.parts: list[str] = []
        self.hrefs: list[str] = []

    @property
    def text(self) -> str:
        return clean_text(" ".join(self.parts), 2000)


class BsoContractResultsParser(HTMLParser):
    def __init__(self, tbody_id: str = BSO_CONTRACT_RESULTS_TBODY_ID) -> None:
        super().__init__()
        self.tbody_id = tbody_id
        self.in_results = False
        self.current_row: list[BsoCell] | None = None
        self.current_cell: BsoCell | None = None
        self.rows: list[BsoContractRow] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        data = attrs_dict(attrs)
        if tag == "tbody" and data.get("id") == self.tbody_id:
            self.in_results = True
            return
        if not self.in_results:
            return
        if tag == "tr":
            self.current_row = []
        elif tag in {"td", "th"} and self.current_row is not None:
            self.current_cell = BsoCell()
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
        if not self.in_results:
            return
        if tag in {"td", "th"} and self.current_cell is not None and self.current_row is not None:
            self.current_row.append(self.current_cell)
            self.current_cell = None
        elif tag == "tr" and self.current_row is not None:
            if self.current_row:
                self.rows.append(BsoContractRow(self.current_row))
            self.current_row = None
        elif tag == "tbody":
            self.in_results = False


class BsoFormParser(HTMLParser):
    def __init__(self, form_id: str) -> None:
        super().__init__()
        self.form_id = form_id
        self.in_form = False
        self.fields: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        data = attrs_dict(attrs)
        if tag == "form" and data.get("id") == self.form_id:
            self.in_form = True
            return
        if not self.in_form or tag not in {"input", "select", "textarea"}:
            return
        name = data.get("name")
        if not name:
            return
        field_type = data.get("type", "").lower()
        if field_type in {"checkbox", "radio"} and "checked" not in data:
            return
        self.fields[name] = data.get("value", "")

    def handle_endtag(self, tag: str) -> None:
        if tag == "form" and self.in_form:
            self.in_form = False


def fetch_bso_active_contracts(
    *,
    config: BsoContractConfig,
    vendor_terms: list[str],
    keywords: list[str],
    max_per_vendor: int,
    progress: Callable[[str], None] | None = None,
) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    seen: set[str] = set()
    terms = unique_terms(vendor_terms)
    limit = max(1, max_per_vendor)

    for term in terms:
        html, opener = fetch_contract_search_page(config)
        source, update = extract_search_ajax(html)
        fields = extract_form_fields(html, "contractBlanketSearchForm")
        response = post_contract_search(opener, config=config, fields=fields, source=source, update=update, vendor_term=term)
        rows = parse_contract_rows(response)
        portal_total = parse_portal_total(response, fallback=len(rows))
        emit(progress, f"{config.state} BSO active contracts: vendor={term}: scanned {len(rows)} of {portal_total} public result rows")

        accepted_for_term = 0
        for row in rows:
            record = normalize_contract_row(row, config=config, vendor_query=term, vendor_terms=terms, keywords=keywords)
            if not record or record["id"] in seen:
                continue
            seen.add(record["id"])
            records.append(record)
            accepted_for_term += 1
            if accepted_for_term >= limit:
                break

    return sorted(records, key=contract_sort_key, reverse=True)


def fetch_contract_search_page(config: BsoContractConfig) -> tuple[str, urllib.request.OpenerDirector]:
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))
    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Referer": config.base_url,
    }
    return http_text(opener, config.active_contracts_url, headers=headers), opener


def post_contract_search(
    opener: urllib.request.OpenerDirector,
    *,
    config: BsoContractConfig,
    fields: dict[str, str],
    source: str,
    update: str,
    vendor_term: str,
) -> str:
    payload = dict(fields)
    payload.update(
        {
            "contractBlanketSearchForm": "contractBlanketSearchForm",
            "contractBlanketSearchForm:vendorName": vendor_term,
            "javax.faces.partial.ajax": "true",
            "javax.faces.source": source,
            "javax.faces.partial.execute": "@all",
            "javax.faces.partial.render": update,
            source: source,
        }
    )
    headers = {
        "Accept": "application/xml, text/xml, */*; q=0.01",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Faces-Request": "partial/ajax",
        "Origin": portal_origin(config.base_url),
        "Referer": config.active_contracts_url,
        "X-Requested-With": "XMLHttpRequest",
    }
    return http_text(opener, config.active_contracts_post_url, data=payload, headers=headers)


def http_text(
    opener: urllib.request.OpenerDirector,
    url: str,
    *,
    data: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = 60,
) -> str:
    body = None if data is None else urllib.parse.urlencode(data).encode("utf-8")
    request_headers = {"User-Agent": USER_AGENT, **(headers or {})}
    last_error: Exception | None = None
    for attempt in range(3):
        request = urllib.request.Request(url, data=body, headers=request_headers)
        try:
            with opener.open(request, timeout=timeout) as response:
                return response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read(600).decode("utf-8", "replace")
            raise RuntimeError(f"HTTP {exc.code} from {url}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = exc
            time.sleep(1 + attempt)
    raise RuntimeError(f"BSO contract request failed for {url}: {last_error}")


def extract_form_fields(html: str, form_id: str) -> dict[str, str]:
    parser = BsoFormParser(form_id)
    parser.feed(html)
    if not parser.fields:
        raise RuntimeError(f"BSO form not found: {form_id}")
    return parser.fields


def extract_search_ajax(html: str) -> tuple[str, str]:
    match = re.search(
        r"searchNew\s*=\s*function\(\)\s*\{return PrimeFaces\.ab\(\{s:\"([^\"]+)\",f:\"contractBlanketSearchForm\",u:\"([^\"]+)\"",
        html,
    )
    if not match:
        raise RuntimeError("BSO active-contract search AJAX source not found")
    return match.group(1), match.group(2)


def parse_contract_rows(response: str) -> list[BsoContractRow]:
    chunks = re.findall(r"<!\[CDATA\[(.*?)\]\]>", response, flags=re.DOTALL)
    html = "\n".join(chunks) if chunks else response
    parser = BsoContractResultsParser()
    parser.feed(html)
    return [row for row in parser.rows if row.cell_text(0) and "No records found" not in row.cell_text(0)]


def parse_portal_total(response: str, *, fallback: int) -> int:
    row_count = re.search(r"rowCount:(\d+)", response)
    if row_count:
        return int(row_count.group(1))
    paginator = re.search(r">\s*\d+\s*-\s*\d+\s+of\s+(\d+)\s*<", response)
    return int(paginator.group(1)) if paginator else fallback


def normalize_contract_row(
    row: BsoContractRow,
    *,
    config: BsoContractConfig,
    vendor_query: str,
    vendor_terms: list[str],
    keywords: list[str],
) -> dict[str, str] | None:
    contract_number = row.cell_text(0)
    bid_id = row.cell_text(2)
    title = row.cell_text(4)
    vendor_name = row.cell_text(5)
    type_code = row.cell_text(6)
    amount = amount_string(row.cell_text(7)) or "0"
    agency = row.cell_text(8)
    status = row.cell_text(9)
    start_date = iso_date(row.cell_text(10))
    end_date, months, recompete = normalized_end_date_fields(row.cell_text(11))
    document_url = row.cell_href(0, config.active_contracts_url)

    if not has_required_contract_fields(contract_number, title, vendor_name, agency, document_url):
        return None
    if not vendor_matches(vendor_name, vendor_query, vendor_terms):
        return None

    matched = keyword_hits(" ".join([vendor_name, vendor_query, agency, title, type_code, contract_number, bid_id]), keywords)
    record_type = contract_record_type(type_code)
    raw = {
        "source_key": config.source_key,
        "contract_number": contract_number,
        "bid_solicitation_number": bid_id,
        "description": title,
        "vendor": vendor_name,
        "type_code": type_code,
        "dollars_spent_to_date": row.cell_text(7),
        "organization": agency,
        "status": status,
        "begin_date": row.cell_text(10),
        "end_date": row.cell_text(11),
        "source_note": config.source_note,
    }

    return {
        "id": stable_id(config.state, config.source_key, contract_number, vendor_name, prefix=f"{config.state.lower()}-bso-contract"),
        "state": config.state,
        "source": config.source_name,
        "source_record_id": contract_number,
        "parent_id": contract_number,
        "contract_record_type": record_type,
        "vendor_name": clean_text(vendor_name, 180),
        "vendor_query": vendor_query,
        "agency": clean_text(agency, 180),
        "contract_number": contract_number,
        "title": clean_text(title, 500),
        "amount": amount,
        "execution_date": "",
        "start_date": start_date,
        "end_date": end_date,
        "months_to_end": "" if months is None else str(months),
        "recompete_signal": recompete,
        "document_type": clean_text(type_code or "BSO Active Contract", 120),
        "document_url": document_url,
        "source_url": config.active_contracts_url,
        "matched_keywords": ";".join(matched),
        "relevance_score": str(relevance_score(matched, parse_amount(amount) or 0, recompete, title, vendor_name, record_type)),
        "raw_json": compact_raw_json(raw),
        "last_checked_at": now_iso(),
    }


def has_required_contract_fields(contract_number: str, title: str, vendor_name: str, agency: str, document_url: str) -> bool:
    return bool(contract_number and title and agency and document_url and normalized_vendor_name(vendor_name))


def normalized_vendor_name(value: str) -> str:
    vendor = clean_text(value, 180)
    return "" if vendor.lower() in PSEUDO_VENDOR_NAMES else vendor


def vendor_matches(vendor_name: str, vendor_query: str, vendor_terms: list[str]) -> bool:
    vendor = normalized_vendor_name(vendor_name)
    if not vendor:
        return False
    candidates = unique_terms([vendor_query, *vendor_terms])
    return any(term_matches(vendor, term) for term in candidates)


def normalized_end_date_fields(value: Any) -> tuple[str, int | None, str]:
    end_date = iso_date(value)
    if is_placeholder_end_date(end_date):
        return "", None, "Open-ended/placeholder end date"
    months = months_until(end_date)
    return end_date, months, recompete_signal(months)


def is_placeholder_end_date(value: Any) -> bool:
    parsed = parse_date(value)
    return bool(parsed and parsed.year >= 2090)


def recompete_signal(months_to_end: int | None) -> str:
    if months_to_end is None:
        return "Unknown end date/current active route"
    if months_to_end < 0:
        return "Expired/past award"
    if months_to_end > 600:
        return "Open-ended/placeholder end date"
    if months_to_end <= 18:
        return "Expiring soon"
    if months_to_end <= 36:
        return "Recompete watch"
    return "Longer-term contract"


def contract_record_type(type_code: str) -> str:
    text = type_code.lower()
    if "statewide" in text or "coop" in text or "cooperative" in text:
        return "master_agreement"
    return "parent_contract"


def contract_sort_key(row: dict[str, str]) -> tuple[int, int, str, int]:
    return (
        int_or_zero(row.get("relevance_score")),
        1 if row.get("contract_record_type") in {"parent_contract", "master_agreement"} else 0,
        row.get("end_date", ""),
        int_or_zero(row.get("amount")),
    )


def relevance_score(keywords: list[str], amount: int, recompete: str, title: str, vendor_name: str, record_type: str) -> int:
    score = min(45, len(keywords) * 8)
    text = " ".join([title, vendor_name])
    if any(term_matches(text, term) for term in ["Medicaid", "MMIS", "METS", "health information"]):
        score += 25
    if any(term_matches(text, term) for term in ["information technology", "technology", "software", "systems", "data"]):
        score += 12
    if amount >= 1_000_000:
        score += 10
    if recompete == "Expiring soon":
        score += 25
    elif recompete == "Recompete watch":
        score += 18
    elif recompete in {"Open-ended/placeholder end date", "Unknown end date/current active route"}:
        score += 6
    if record_type == "master_agreement":
        score += 8
    return max(0, min(score, 100))


def unique_terms(terms: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for term in terms:
        cleaned = clean_text(term, 100)
        key = cleaned.lower()
        if cleaned and key not in seen:
            seen.add(key)
            result.append(cleaned)
    return result


def portal_origin(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, "", "", ""))


def int_or_zero(value: Any) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


def attrs_dict(attrs: list[tuple[str, str | None]]) -> dict[str, str]:
    return {key: value or "" for key, value in attrs}


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def emit(progress: Callable[[str], None] | None, message: str) -> None:
    if progress:
        progress(message)
