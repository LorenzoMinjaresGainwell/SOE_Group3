from __future__ import annotations

import datetime as dt
import html
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from html.parser import HTMLParser
from http.cookiejar import CookieJar
from typing import Any, Callable

from services.state_contracts.keyword_context import useful_keyword_match as keyword_context_match
from services.state_normalization import clean_text, iso_date, keyword_hits, months_until, stable_id, term_matches

CONTRACT_BROWSE_URL = "https://app.az.gov/page.aspx/en/ctr/contract_browse_public"
CONTRACT_AJAX_URL = "https://app.az.gov/ajax.aspx/en/ctr/contract_browse_public"
SOURCE_NAME = "Arizona Procurement Portal Public Contracts"
USER_AGENT = "soe-group3-az-app-contracts/0.1"
TABLE_ID = "body_x_grid_grd"
PAGE_SIZE = 15
MAX_PUBLIC_PAGES = 5_000
TAG_RE = re.compile(r"(?is)<[^>]+>")

HEADERS = [
    "editing",
    "contract_number",
    "amendment_number",
    "title",
    "supplier",
    "contract_type",
    "effective_date",
    "sourcing_project",
    "extended_end_date",
    "owner_last_name",
    "owner_first_name",
    "owner",
    "initial_end_date",
    "statewide_contract",
]


@dataclass(frozen=True)
class PublicBrowseResult:
    rows: list[dict[str, Any]]
    first_page_rows: int
    total_pages_discovered: int
    total_rows_reported: int | None
    rows_scanned: int


class FormInputParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.inputs: list[dict[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "input":
            self.inputs.append({name.lower(): value or "" for name, value in attrs})


def fetch_contracts(
    *,
    vendor_terms: list[str],
    keywords: list[str],
    max_per_vendor: int,
    progress: Callable[[str], None] | None = None,
) -> list[dict[str, str]]:
    browse = fetch_public_browse(progress=progress)
    emit(
        progress,
        "AZ APP public contract browse: "
        f"first page {browse.first_page_rows} rows; "
        f"scanned {browse.rows_scanned} rows across {browse.total_pages_discovered} public AJAX pages"
        + (f"; portal count {browse.total_rows_reported}" if browse.total_rows_reported is not None else ""),
    )

    records: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in browse.rows:
        record = normalize_row(row, vendor_terms=vendor_terms, keywords=keywords)
        if not record or record["id"] in seen:
            continue
        seen.add(record["id"])
        records.append(record)

    emit(progress, f"AZ APP public contract browse: normalized {len(records)} matching contract rows")
    limit = max(1, max_per_vendor) * max(1, len(unique_terms(vendor_terms)))
    return sorted(records, key=contract_sort_key, reverse=True)[:limit]


def fetch_public_rows() -> list[dict[str, Any]]:
    return fetch_public_browse().rows


def fetch_public_browse(progress: Callable[[str], None] | None = None) -> PublicBrowseResult:
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))
    first_html = fetch_text(
        opener,
        CONTRACT_BROWSE_URL,
        headers={"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"},
    )
    first_rows = parse_contract_rows(first_html)
    if not first_rows:
        raise RuntimeError("AZ public contract browse returned no parseable rows")

    total_rows = fetch_exact_row_count(opener, first_html)
    total_pages = total_pages_from(total_rows)
    emit(
        progress,
        "AZ APP public contract browse: "
        f"discovered {total_pages} public AJAX pages"
        + (f" from count {total_rows}" if total_rows is not None else " from pager"),
    )

    rows = list(first_rows)
    for page_index in range(1, total_pages):
        page_html = fetch_page(opener, first_html, page_index)
        page_rows = parse_contract_rows(page_html)
        if not page_rows:
            raise RuntimeError(f"AZ public contract browse page {page_index + 1} returned no parseable rows")
        rows.extend(page_rows)
        if progress and (page_index + 1) % 100 == 0:
            emit(progress, f"AZ APP public contract browse: scanned {len(rows)} rows through page {page_index + 1}/{total_pages}")

    return PublicBrowseResult(
        rows=rows,
        first_page_rows=len(first_rows),
        total_pages_discovered=total_pages,
        total_rows_reported=total_rows,
        rows_scanned=len(rows),
    )


def fetch_exact_row_count(opener: urllib.request.OpenerDirector, page_html: str) -> int:
    fields = ajax_grid_fields(page_html)
    max_page = fields.get("maxpageindexbody_x_grid_grd", "")
    current_page = fields.get("hdnCurrentPageIndexbody_x_grid_grd", "0")
    fields["__EVENTTARGET"] = TABLE_ID
    fields["__EVENTARGUMENT"] = f"GetCount|all&maxpageindexbody_x_grid_grd={max_page}&hdnCurrentPageIndexbody_x_grid_grd={current_page}"
    text = fetch_text(
        opener,
        CONTRACT_AJAX_URL,
        method="POST",
        data=fields,
        headers={
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Origin": "https://app.az.gov",
            "Referer": CONTRACT_BROWSE_URL,
            "X-Requested-With": "XMLHttpRequest",
            "IV-AjaxControl": "grid",
            "IV-AjaxControl-ID": TABLE_ID,
        },
        byte_limit=100_000,
    )
    match = re.search(r"\bcount\s*:\s*(\d+)", text)
    if not match:
        raise RuntimeError("AZ public contract browse exact row count unavailable; refusing partial initial-pager scan")
    return int(match.group(1))


def fetch_page(opener: urllib.request.OpenerDirector, first_page_html: str, page_index: int) -> str:
    fields = ajax_grid_fields(first_page_html)
    fields["__EVENTTARGET"] = TABLE_ID
    fields["__EVENTARGUMENT"] = f"Page|{page_index}"
    return fetch_text(
        opener,
        CONTRACT_AJAX_URL,
        method="POST",
        data=fields,
        headers={
            "Accept": "*/*",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Origin": "https://app.az.gov",
            "Referer": CONTRACT_BROWSE_URL,
            "X-Requested-With": "XMLHttpRequest",
            "IV-AjaxControl": "grid",
            "IV-AjaxControl-ID": TABLE_ID,
        },
    )


def fetch_text(
    opener: urllib.request.OpenerDirector,
    url: str,
    *,
    method: str = "GET",
    data: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
    byte_limit: int = 2_000_000,
) -> str:
    request_headers = {"User-Agent": USER_AGENT, **(headers or {})}
    body = urllib.parse.urlencode(data).encode("utf-8") if data is not None else None
    request = urllib.request.Request(url, data=body, headers=request_headers, method=method)
    try:
        with opener.open(request, timeout=60) as response:
            raw = response.read(byte_limit + 1)
            if len(raw) > byte_limit:
                raise RuntimeError(f"AZ public contract browse response exceeded {byte_limit} bytes")
            text = raw.decode("utf-8", "replace")
            reject_browser_check(response.geturl(), text)
            return text
    except urllib.error.HTTPError as exc:
        text = exc.read(2000).decode("utf-8", "replace")
        reject_browser_check(exc.geturl(), text)
        raise RuntimeError(f"HTTP request failed ({exc.code}) for {url}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(f"HTTP request failed for {url}: {exc}") from exc


def reject_browser_check(final_url: str, text: str) -> None:
    if "/bas/browser_check" in final_url or "/bas/browser_check" in text or "Browser check:" in text:
        raise RuntimeError("AZ public contract browse redirected to browser_check")


def form_fields(page_html: str) -> dict[str, str]:
    parser = FormInputParser()
    parser.feed(page_html)
    fields: dict[str, str] = {}
    for attrs in parser.inputs:
        name = attrs.get("name")
        input_type = attrs.get("type", "").lower()
        if not name or input_type in {"submit", "button", "image", "file"}:
            continue
        if input_type in {"checkbox", "radio"} and "checked" not in attrs:
            continue
        fields[name] = attrs.get("value", "")
    return fields


def ajax_grid_fields(page_html: str) -> dict[str, str]:
    fields = form_fields(page_html)
    keep = {
        "hdnSortExpressionbody_x_grid_grd",
        "hdnSortDirectionbody_x_grid_grd",
        "hdnCurrentPageIndexbody_x_grid_grd",
        "hdnRowCountbody_x_grid_grd",
        "maxpageindexbody_x_grid_grd",
        "ajaxrowsiscountedbody_x_grid_grd",
        "body:x:txtQuery",
        "body:x:txtOrgaSearch",
        "body:x:txtCtrRef_2",
        "body:x:txtCtrValidityStatus",
        "body:x:selStatusCode_1",
    }
    return {name: fields.get(name, "") for name in keep if name in fields}


def total_pages_from(total_rows: int) -> int:
    pages = (total_rows + PAGE_SIZE - 1) // PAGE_SIZE
    if pages < 1:
        raise RuntimeError("AZ public contract browse advertised no pages")
    if pages > MAX_PUBLIC_PAGES:
        raise RuntimeError(f"AZ public contract browse advertised {pages} pages, above safety cap {MAX_PUBLIC_PAGES}")
    return pages


def parse_contract_rows(page_html: str) -> list[dict[str, Any]]:
    table_match = re.search(rf'(?is)<table\b[^>]*id=["\']{re.escape(TABLE_ID)}["\'][^>]*>(.*?)</table>', page_html)
    if not table_match:
        return []

    rows: list[dict[str, Any]] = []
    for attrs, row_html in re.findall(r"(?is)<tr\b([^>]*)>(.*?)</tr>", table_match.group(1)):
        cells = [parse_cell(cell_html) for cell_html in re.findall(r"(?is)<t[dh]\b[^>]*>(.*?)</t[dh]>", row_html)]
        if len(cells) < len(HEADERS) or cells[1]["text"].lower() == "contract":
            continue
        row = {name: cells[index]["text"] for index, name in enumerate(HEADERS)}
        hrefs = [href for cell in cells for href in cell["hrefs"]]
        detail_url = next((href for href in hrefs if "/ctr/contract_manage_public/" in href), hrefs[0] if hrefs else "")
        row.update(
            {
                "row_id": first_match(attrs, r'data-id=["\']([^"\']+)') or first_match(attrs, r'id=["\'][^"\']*tr_(\d+)'),
                "detail_url": detail_url,
                "detail_url_note": "Detail route redirects to /bas/browser_check from CLI; public browse row fields are used.",
            }
        )
        rows.append(row)
    return rows


def parse_cell(cell_html: str) -> dict[str, Any]:
    hrefs = [urllib.parse.urljoin(CONTRACT_BROWSE_URL, html.unescape(href)) for href in re.findall(r"(?is)<a\b[^>]*href=[\"']([^\"']+)", cell_html)]
    text = clean_text(html.unescape(TAG_RE.sub(" ", cell_html)), 4000)
    return {"text": text, "hrefs": hrefs}


def normalize_row(row: dict[str, Any], *, vendor_terms: list[str], keywords: list[str]) -> dict[str, str] | None:
    contract_number = clean_text(row.get("contract_number"), 120)
    title = clean_text(row.get("title") or contract_number, 500)
    vendor_name = clean_text(row.get("supplier"), 180)
    end_date = iso_date(row.get("extended_end_date") or row.get("initial_end_date"))
    if not contract_number or not title or not vendor_name or not end_date:
        return None

    search_text = " ".join(
        [
            contract_number,
            title,
            vendor_name,
            clean_text(row.get("contract_type"), 120),
            clean_text(row.get("sourcing_project"), 1000),
            clean_text(row.get("statewide_contract"), 80),
        ]
    )
    vendor_hits = keyword_hits(vendor_name, unique_terms(vendor_terms))
    matched = keyword_hits(search_text, keywords)
    if not vendor_hits and not useful_keyword_match(matched, search_text):
        return None

    months = months_until(end_date)
    recompete = recompete_signal(months)
    record_type = contract_record_type(row.get("contract_type"))
    source_record_id = contract_number
    if clean_text(row.get("amendment_number")) not in {"", "0"}:
        source_record_id = f"{contract_number}-amendment-{clean_text(row.get('amendment_number'), 20)}"

    raw = dict(row)
    raw["source_key"] = "az_app"
    raw["source_note"] = "Official Arizona Procurement Portal public contract browse table with public AJAX pagination; detail pages browser-check gate from CLI."

    return {
        "id": stable_id("AZ", source_record_id, vendor_name, prefix="az-app-contract"),
        "state": "AZ",
        "source": SOURCE_NAME,
        "source_record_id": source_record_id,
        "parent_id": contract_number,
        "contract_record_type": record_type,
        "vendor_name": vendor_name,
        "vendor_query": ";".join(vendor_hits),
        "agency": "Arizona Department of Administration, State Procurement Office",
        "contract_number": contract_number,
        "title": title,
        "amount": "0",
        "execution_date": "",
        "start_date": iso_date(row.get("effective_date")),
        "end_date": end_date,
        "months_to_end": "" if months is None else str(months),
        "recompete_signal": recompete,
        "document_type": clean_text(row.get("contract_type") or "Arizona Public Contract", 120),
        "document_url": CONTRACT_BROWSE_URL,
        "source_url": CONTRACT_BROWSE_URL,
        "matched_keywords": ";".join(matched),
        "relevance_score": str(relevance_score(vendor_hits, matched, recompete, search_text, record_type)),
        "raw_json": json.dumps(raw, ensure_ascii=True, sort_keys=True, separators=(",", ":")),
        "last_checked_at": now_iso(),
    }


def useful_keyword_match(matches: list[str], text: str) -> bool:
    return keyword_context_match(matches, text)


def contract_record_type(contract_type: Any) -> str:
    lower = clean_text(contract_type).lower()
    if "master" in lower:
        return "master_agreement"
    if "cooperative" in lower:
        return "cooperative_contract"
    if "sole source" in lower or "competition impracticable" in lower:
        return "award"
    return "parent_contract"


def relevance_score(vendor_hits: list[str], matches: list[str], recompete: str, text: str, record_type: str) -> int:
    score = min(45, len(matches) * 8)
    if vendor_hits:
        score += 35
    if any(term_matches(text, term) for term in ["Medicaid", "MMIS", "AHCCCS", "managed care", "provider data"]):
        score += 25
    if any(term_matches(text, term) for term in ["health", "medical", "claims", "eligibility", "interoperability"]):
        score += 12
    if recompete == "Expiring soon":
        score += 25
    elif recompete == "Recompete watch":
        score += 18
    if record_type in {"master_agreement", "cooperative_contract"}:
        score += 8
    return max(0, min(score, 100))


def recompete_signal(months_to_end: int | None) -> str:
    if months_to_end is None:
        return "Unknown end date"
    if months_to_end < 0:
        return "Expired/past award"
    if months_to_end > 600:
        return "Open-ended/placeholder end date"
    if months_to_end <= 18:
        return "Expiring soon"
    if months_to_end <= 36:
        return "Recompete watch"
    return "Longer-term contract"


def contract_sort_key(row: dict[str, str]) -> tuple[int, int, str, str]:
    return (
        int_or_zero(row.get("relevance_score")),
        1 if row.get("vendor_query") else 0,
        row.get("end_date", ""),
        row.get("title", ""),
    )


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


def first_match(text: str, pattern: str) -> str:
    match = re.search(pattern, text, re.IGNORECASE)
    return clean_text(match.group(1), 120) if match else ""


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
