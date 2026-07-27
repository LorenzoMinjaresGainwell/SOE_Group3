from __future__ import annotations

import datetime as dt
import re
import urllib.parse
from typing import Any, Callable

from services.state_http import fetch_url
from services.state_contracts.keyword_context import useful_keyword_match as keyword_context_match
from services.state_normalization import clean_text, compact_raw_json, iso_date, keyword_hits, months_until, stable_id, term_matches

SOURCE_URL = "https://bgs.vermont.gov/purchasing-contracting/contract-info/current"
BID_SYSTEM_AWARDS_URL = "https://www.vermontbusinessregistry.com/BidSearch.aspx?type=10"
SOURCE_NAME = "Vermont BGS Current Statewide Contracts"
SOURCE_NOTE = (
    "Official BGS current statewide contracts HTML page. Tables expose contract number, supplier, "
    "contract PDF/source link, and expiration date. Vermont Business Registry BidSearch type=10 was "
    "probed but not used because award pages expose awarded vendor/amount without contract term/end dates."
)
USER_AGENT = "Mozilla/5.0 soe-group3-vt-bgs-contracts/0.1"
TAG_RE = re.compile(r"(?is)<[^>]+>")
TOKEN_RE = re.compile(r"(?is)<h[2-4]\b[^>]*>.*?</h[2-4]>|<table\b[^>]*>.*?</table>")
CELL_RE = re.compile(r"(?is)<t[dh]\b[^>]*>(.*?)</t[dh]>")
ROW_RE = re.compile(r"(?is)<tr\b[^>]*>(.*?)</tr>")
LINK_RE = re.compile(r"(?is)<a\b[^>]*href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>")
PSEUDO_VENDOR_TERMS = {"", "tbd", "new contract in process", "bgs print shop"}


def fetch_contracts(
    *,
    vendor_terms: list[str],
    keywords: list[str],
    max_per_vendor: int,
    progress: Callable[[str], None] | None = None,
) -> list[dict[str, str]]:
    html = fetch_html(SOURCE_URL)
    rows = parse_current_contract_rows(html)
    emit(progress, f"VT BGS current statewide contracts: scanned {len(rows)} public table rows")

    terms = unique_terms(vendor_terms)
    records: list[dict[str, str]] = []
    seen: set[str] = set()
    query_counts: dict[str, int] = {}
    limit = max(1, max_per_vendor)

    for row in rows:
        record = normalize_contract_row(row, vendor_terms=terms, keywords=keywords)
        if not record:
            continue
        query = record["vendor_query"]
        if query_counts.get(query, 0) >= limit:
            continue
        if record["id"] in seen:
            continue
        seen.add(record["id"])
        query_counts[query] = query_counts.get(query, 0) + 1
        records.append(record)

    emit(progress, f"VT BGS current statewide contracts: normalized {len(records)} records")
    return sorted(records, key=contract_sort_key, reverse=True)


def fetch_html(url: str) -> str:
    result = fetch_url(
        url,
        headers={"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"},
        timeout=60,
        byte_limit=1_500_000,
        user_agent=USER_AGENT,
    )
    result.raise_for_status()
    return result.body_text()


def parse_current_contract_rows(html: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    current_heading = ""
    for match in TOKEN_RE.finditer(html):
        token = match.group(0)
        if token.lower().startswith("<h"):
            heading = strip_html(token, 300)
            if useful_heading(heading):
                current_heading = heading.rstrip(":")
            continue
        rows.extend(parse_table(token, heading=current_heading))
    return rows


def parse_table(table_html: str, *, heading: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    current_title = heading
    for tr in ROW_RE.findall(table_html):
        cell_htmls = CELL_RE.findall(tr)
        cells = [strip_html(cell, 1000) for cell in cell_htmls]
        cells = trim_empty_tail(cells)
        if not cells or is_header_row(cells):
            continue
        if not is_contract_id(cells[0]):
            title = row_title(cells)
            if title:
                current_title = title
            continue

        vendor_name = clean_text(cells[1] if len(cells) > 1 else "", 180)
        end_date = iso_date(cells[-1] if cells else "")
        document_url = first_contract_link(cell_htmls, base_url=SOURCE_URL)
        contract_number = clean_text(cells[0], 120)
        if not contract_number or not normalized_vendor(vendor_name) or not end_date or not document_url:
            continue
        rows.append(
            {
                "contract_number": contract_number,
                "vendor_name": vendor_name,
                "title": clean_text(current_title or heading or "Statewide Contract", 500),
                "contact": clean_text(cells[2] if len(cells) > 2 else "", 180),
                "phone": clean_text(cells[3] if len(cells) > 3 else "", 80),
                "end_date_raw": clean_text(cells[-1], 80),
                "document_url": document_url,
            }
        )
    return rows


def normalize_contract_row(row: dict[str, str], *, vendor_terms: list[str], keywords: list[str]) -> dict[str, str]:
    contract_number = row["contract_number"]
    vendor_name = row["vendor_name"]
    title = row["title"]
    end_date = iso_date(row["end_date_raw"])
    if not contract_number or not vendor_name or not title or not end_date:
        return {}

    search_text = " ".join([contract_number, vendor_name, title, row.get("contact", "")])
    vendor_hits = keyword_hits(vendor_name, vendor_terms)
    matched = keyword_hits(search_text, keywords)
    if not vendor_hits and not useful_keyword_match(matched, search_text):
        return {}

    query = vendor_hits[0] if vendor_hits else matched[0]
    months = months_until(end_date)
    record_type = contract_record_type(title)
    raw = {
        "source_key": "vt_bgs_statewide_contracts",
        "source_note": SOURCE_NOTE,
        "bid_system_awards_url_rejected": BID_SYSTEM_AWARDS_URL,
        "row": row,
    }
    return {
        "id": stable_id("VT", contract_number, vendor_name, prefix="vt-bgs-contract"),
        "state": "VT",
        "source": SOURCE_NAME,
        "source_record_id": contract_number,
        "parent_id": contract_number,
        "contract_record_type": record_type,
        "vendor_name": vendor_name,
        "vendor_query": query,
        "agency": "Vermont Buildings and General Services Office of Purchasing and Contracting",
        "contract_number": contract_number,
        "title": title,
        "amount": "0",
        "execution_date": "",
        "start_date": "",
        "end_date": end_date,
        "months_to_end": "" if months is None else str(months),
        "recompete_signal": recompete_signal(months),
        "document_type": "Vermont Current Statewide Contract",
        "document_url": row["document_url"],
        "source_url": SOURCE_URL,
        "matched_keywords": ";".join(matched),
        "relevance_score": str(relevance_score(vendor_hits, matched, months, search_text, record_type)),
        "raw_json": compact_raw_json(raw),
        "last_checked_at": now_iso(),
    }


def first_contract_link(cell_htmls: list[str], *, base_url: str) -> str:
    for cell in cell_htmls[:2]:
        for href, _text in LINK_RE.findall(cell):
            if href.lower().startswith("mailto:"):
                continue
            return urllib.parse.urljoin(base_url, clean_text(href, 800))
    return ""


def row_title(cells: list[str]) -> str:
    if len(cells) > 2:
        return ""
    text = clean_text(" ".join(cells), 300).rstrip(":")
    lower = text.lower()
    if not text or text in {"&nbsp;", ""}:
        return ""
    if any(skip in lower for skip in ["related products", "related services", "click here", "national association"]):
        return ""
    if len(text) > 180:
        return ""
    return text


def useful_heading(value: str) -> bool:
    text = clean_text(value, 300)
    return bool(text and not text.lower().startswith(("contract", "current statewide")))


def is_header_row(cells: list[str]) -> bool:
    lower = [cell.lower() for cell in cells]
    return bool(lower and lower[0] == "contract" and any("supplier" in cell for cell in lower))


def is_contract_id(value: str) -> bool:
    text = clean_text(value, 120)
    if not text or len(text) > 80:
        return False
    lower = text.lower()
    if lower.startswith(("contract", "see ", "new contract", "information ")):
        return False
    return bool(re.search(r"\d", text))


def normalized_vendor(value: str) -> str:
    vendor = clean_text(value, 180)
    return "" if vendor.lower() in PSEUDO_VENDOR_TERMS else vendor


def useful_keyword_match(matches: list[str], text: str) -> bool:
    return keyword_context_match(matches, text)


def contract_record_type(title: str) -> str:
    lower = title.lower()
    if "naspo" in lower or "sourcewell" in lower or "cooperative" in lower:
        return "cooperative_contract"
    return "master_agreement"


def relevance_score(vendor_hits: list[str], matches: list[str], months_to_end: int | None, text: str, record_type: str) -> int:
    score = min(45, len(matches) * 8)
    if vendor_hits:
        score += 35
    if any(term_matches(text, term) for term in ["Medicaid", "MMIS", "Medicare", "managed care", "provider data"]):
        score += 25
    if any(term_matches(text, term) for term in ["information technology", "software", "cloud", "SaaS", "data"]):
        score += 12
    if months_to_end is not None:
        if 0 <= months_to_end <= 18:
            score += 25
        elif months_to_end <= 36:
            score += 18
        elif months_to_end > 36:
            score += 6
    if record_type in {"master_agreement", "cooperative_contract"}:
        score += 8
    return max(0, min(score, 100))


def recompete_signal(months_to_end: int | None) -> str:
    if months_to_end is None:
        return "Unknown end date"
    if months_to_end < 0:
        return "Expired/past award"
    if months_to_end <= 18:
        return "Expiring soon"
    if months_to_end <= 36:
        return "Recompete watch"
    return "Longer-term contract"


def trim_empty_tail(values: list[str]) -> list[str]:
    result = list(values)
    while result and not clean_text(result[-1]):
        result.pop()
    return result


def strip_html(value: Any, limit: int = 1000) -> str:
    return clean_text(TAG_RE.sub(" ", str(value or "")), limit)


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


def contract_sort_key(row: dict[str, str]) -> tuple[int, int, str, str]:
    return (
        int_or_zero(row.get("relevance_score")),
        1 if row.get("vendor_query") else 0,
        row.get("end_date", ""),
        row.get("title", ""),
    )


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
