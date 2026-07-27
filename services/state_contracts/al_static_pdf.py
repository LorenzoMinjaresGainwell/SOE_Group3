from __future__ import annotations

import datetime as dt
import hashlib
import re
import shutil
import subprocess
import urllib.parse
from pathlib import Path
from typing import Any, Callable

from services.state_http import fetch_url
from services.state_contracts.keyword_context import useful_keyword_match as keyword_context_match
from services.state_normalization import clean_text, compact_raw_json, iso_date, keyword_hits, months_until, stable_id, term_matches

CONTRACT_NEWS_URL = "https://procurement.alabama.gov/contract-news/"
SUPPLIER_RESOURCES_URL = "https://procurement.alabama.gov/supplier-resources/"
PROCUREMENT_HOME_URL = "https://procurement.alabama.gov/"
VEHICLE_CONTRACT_PDF_URL = "https://procurement.alabama.gov/media/5tlmssv4/statewide-vehicle-and-accessory-contract-list.pdf"
ALABAMA_BUYS_REJECTED_URLS = [
    "https://alabamabuys.gov/page.aspx/en/ctr/contract_browse_public",
    "https://alabamabuys.gov/page.aspx/en/rfp/request_browse_public",
    "https://alabamabuys.gov/page.aspx/en/sup/supplier_browse_public",
]
SOURCE_NAME = "Alabama Procurement Static Statewide Contract PDFs"
SOURCE_NOTE = (
    "Official procurement.alabama.gov pages were probed for static PDF/XLSX/CSV contract artifacts. "
    "The Statewide Vehicle and Accessory Contract List PDF exposes master-agreement ids, suppliers, "
    "categories/items, line/base-pricing fields, and end dates. AlabamaBuys/Ivalua public contract, "
    "supplier, and solicitation routes were rejected because they redirect to /page.aspx/en/bas/browser_check."
)
USER_AGENT = "Mozilla/5.0 soe-group3-al-static-contract-pdfs/0.1"
REPO_ROOT = Path(__file__).resolve().parents[2]
CACHE_DIR = REPO_ROOT / "data" / "raw" / "state_contracts" / "al"

SECTION_HEADERS = {
    "Sedans",
    "Crossovers",
    "Small Trucks",
    "Sport Utility Vehicle (SUV)",
    "Full Size Trucks Passenger Fleet",
    "Vans",
    "After Market Truck Accessories",
}
HEADER_PREFIXES = (
    "Contracts can be accessed",
    "Statewide Contracts",
    "State of Alabama Vehicle",
    "Accessory Agreements",
)
DATE_RE = re.compile(r"\b\d{1,2}/\d{1,2}/\d{4}\b")
VEHICLE_ROW_RE = re.compile(r"^MA\s+(\d{9,15})\s+(.+?)\s*\((V[A-Z0-9]+)\)\s*(.*?)\s+(\d{1,2}/\d{1,2}/\d{4})\s*$")
PRICE_RE = re.compile(r"\$[\d,]+(?:\.\d{2})?")
LINE_RE = re.compile(r"\bLine\s*([A-Za-z0-9-]+)\b", re.IGNORECASE)
PSEUDO_VENDOR_TERMS = {"", "tbd", "n/a", "na"}


def fetch_contracts(
    *,
    vendor_terms: list[str],
    keywords: list[str],
    max_per_vendor: int,
    progress: Callable[[str], None] | None = None,
) -> list[dict[str, str]]:
    rows, scanned = fetch_vehicle_contract_rows(progress=progress)
    emit(progress, f"AL static statewide contract PDFs: scanned {scanned} vehicle/accessory PDF rows; {len(rows)} rows have contract id + vendor + end date")

    terms = unique_terms(vendor_terms)
    records: list[dict[str, str]] = []
    seen: set[str] = set()
    query_counts: dict[str, int] = {}
    limit = max(1, max_per_vendor)

    for row in rows:
        record = normalize_vehicle_row(row, vendor_terms=terms, keywords=keywords)
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

    emit(progress, f"AL static statewide contract PDFs: normalized {len(records)} vendor/default-keyword records")
    return sorted(records, key=contract_sort_key, reverse=True)


def fetch_vehicle_contract_rows(*, progress: Callable[[str], None] | None = None) -> tuple[list[dict[str, str]], int]:
    pdf_path = cached_pdf(VEHICLE_CONTRACT_PDF_URL, progress=progress)
    text = extracted_pdf_text(pdf_path, progress=progress)
    if not text:
        return [], 0
    return parse_vehicle_contract_rows(text, document_url=VEHICLE_CONTRACT_PDF_URL)


def cached_pdf(url: str, *, progress: Callable[[str], None] | None = None) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = CACHE_DIR / cache_filename(url)
    if path.exists() and path.stat().st_size > 0:
        emit(progress, f"AL static statewide contract PDFs: cache hit {path}")
        return path

    result = fetch_url(
        url,
        headers={"Accept": "application/pdf,*/*", "Referer": CONTRACT_NEWS_URL},
        timeout=60,
        byte_limit=10_000_000,
        user_agent=USER_AGENT,
    )
    result.raise_for_status()
    path.write_bytes(result.body)
    emit(progress, f"AL static statewide contract PDFs: downloaded {len(result.body)} bytes to {path}")
    return path


def extracted_pdf_text(pdf_path: Path, *, progress: Callable[[str], None] | None = None) -> str:
    text_path = pdf_path.with_suffix(pdf_path.suffix + ".txt")
    if text_path.exists() and text_path.stat().st_size > 0 and text_path.stat().st_mtime >= pdf_path.stat().st_mtime:
        return text_path.read_text(encoding="utf-8", errors="replace")

    tool = shutil.which("pdftotext")
    if not tool:
        emit(progress, "AL static statewide contract PDFs: pdftotext not installed; skipping PDF extraction")
        return ""

    proc = subprocess.run([tool, "-layout", str(pdf_path), str(text_path)], capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        emit(progress, f"AL static statewide contract PDFs: pdftotext failed: {clean_text(proc.stderr, 300)}")
        return ""
    return text_path.read_text(encoding="utf-8", errors="replace")


def parse_vehicle_contract_rows(text: str, *, document_url: str) -> tuple[list[dict[str, str]], int]:
    rows: list[dict[str, str]] = []
    scanned = 0
    category = "Statewide Vehicle and Accessory Agreements"
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        line = normalized_line(lines[index])
        index += 1
        if not line:
            continue
        if line in SECTION_HEADERS:
            category = line
            continue
        if is_header_line(line):
            continue
        if not line.startswith("MA "):
            continue

        scanned += 1
        if not DATE_RE.search(line):
            next_index = next_non_empty_index(lines, index)
            if next_index is not None:
                next_line = normalized_line(lines[next_index])
                if next_line.startswith("(") or DATE_RE.search(next_line):
                    line = f"{line} {next_line}"
                    index = next_index + 1

        match = VEHICLE_ROW_RE.match(line)
        if not match:
            continue
        contract_digits, vendor_name, vendor_id, details, end_date_raw = match.groups()
        parsed = parse_vehicle_details(details)
        contract_number = f"MA {contract_digits}"
        item = parsed["item"] or category
        rows.append(
            {
                "contract_number": contract_number,
                "vendor_name": clean_text(vendor_name, 180),
                "vendor_id": clean_text(vendor_id, 80),
                "category": clean_text(category, 180),
                "item": clean_text(item, 220),
                "configuration": parsed["configuration"],
                "line_number": parsed["line_number"],
                "base_price": parsed["base_price"],
                "end_date_raw": clean_text(end_date_raw, 80),
                "document_url": document_url,
                "raw_line": line,
            }
        )
    return rows, scanned


def parse_vehicle_details(details: str) -> dict[str, str]:
    text = clean_text(details, 500)
    price_match = PRICE_RE.search(text)
    base_price = price_match.group(0) if price_match else ""
    before_price = text[: price_match.start()].strip() if price_match else text
    after_price = text[price_match.end() :].strip() if price_match else ""

    line_number = ""
    line_match = LINE_RE.search(after_price) or LINE_RE.search(before_price)
    if line_match:
        line_number = f"Line {line_match.group(1)}"
        after_price = LINE_RE.sub("", after_price).strip()
        before_price = LINE_RE.sub("", before_price).strip()

    return {
        "item": clean_text(before_price, 220),
        "configuration": clean_text(after_price, 220),
        "line_number": line_number,
        "base_price": base_price,
    }


def normalize_vehicle_row(row: dict[str, str], *, vendor_terms: list[str], keywords: list[str]) -> dict[str, str]:
    contract_number = clean_text(row.get("contract_number"), 120)
    vendor_name = normalized_vendor(row.get("vendor_name", ""))
    category = clean_text(row.get("category"), 180)
    item = clean_text(row.get("item") or category, 220)
    end_date = iso_date(row.get("end_date_raw"))
    if not contract_number or not vendor_name or not category or not item or not end_date:
        return {}

    line_number = clean_text(row.get("line_number"), 80)
    configuration = clean_text(row.get("configuration"), 220)
    search_text = " ".join([contract_number, vendor_name, category, item, configuration, line_number, "vehicle fleet automotive cars trucks accessories"])
    vendor_hits = keyword_hits(vendor_name, vendor_terms)
    matched = keyword_hits(search_text, keywords)
    if not vendor_hits and not useful_keyword_match(matched, search_text):
        return {}

    query = vendor_hits[0] if vendor_hits else matched[0]
    source_record_id = source_record_id_for(row)
    months = months_until(end_date)
    raw = {
        "source_key": "al_static_statewide_contract_pdfs",
        "source_note": SOURCE_NOTE,
        "discovery_pages": [PROCUREMENT_HOME_URL, CONTRACT_NEWS_URL, SUPPLIER_RESOURCES_URL],
        "rejected_alabama_buys_urls": ALABAMA_BUYS_REJECTED_URLS,
        "row": row,
    }
    return {
        "id": stable_id("AL", source_record_id, vendor_name, prefix="al-static-pdf-contract"),
        "state": "AL",
        "source": SOURCE_NAME,
        "source_record_id": source_record_id,
        "parent_id": contract_number,
        "contract_record_type": "dealer_line",
        "vendor_name": vendor_name,
        "vendor_query": query,
        "agency": "Alabama Department of Finance Division of Procurement",
        "contract_number": contract_number,
        "title": title_for(row),
        "amount": "0",
        "execution_date": "",
        "start_date": "",
        "end_date": end_date,
        "months_to_end": "" if months is None else str(months),
        "recompete_signal": recompete_signal(months),
        "document_type": "Alabama Statewide Vehicle and Accessory Contract PDF",
        "document_url": clean_text(row.get("document_url"), 700) or VEHICLE_CONTRACT_PDF_URL,
        "source_url": CONTRACT_NEWS_URL,
        "matched_keywords": ";".join(matched),
        "relevance_score": str(relevance_score(vendor_hits, matched, months, search_text)),
        "raw_json": compact_raw_json(raw),
        "last_checked_at": now_iso(),
    }


def title_for(row: dict[str, str]) -> str:
    parts = ["Statewide Vehicle and Accessory Agreements", row.get("category", ""), row.get("item", ""), row.get("configuration", ""), row.get("line_number", "")]
    return clean_text(" - ".join(part for part in parts if clean_text(part)), 500)


def source_record_id_for(row: dict[str, str]) -> str:
    parts = [row.get("contract_number", ""), row.get("line_number", ""), row.get("item", ""), row.get("vendor_id", "")]
    return clean_text(" - ".join(part for part in parts if clean_text(part)), 240)


def normalized_line(value: str) -> str:
    return clean_text(str(value or "").replace("\f", " "), 1200)


def next_non_empty_index(lines: list[str], start: int) -> int | None:
    for index in range(start, min(len(lines), start + 4)):
        if normalized_line(lines[index]):
            return index
    return None


def is_header_line(line: str) -> bool:
    return line.startswith(HEADER_PREFIXES) or "MA Base" in line or line == "Number End Date"


def normalized_vendor(value: str) -> str:
    vendor = clean_text(value, 180)
    return "" if vendor.lower() in PSEUDO_VENDOR_TERMS else vendor


def useful_keyword_match(matches: list[str], text: str) -> bool:
    return keyword_context_match(matches, text)


def relevance_score(vendor_hits: list[str], matches: list[str], months_to_end: int | None, text: str) -> int:
    score = min(45, len(matches) * 8)
    if vendor_hits:
        score += 35
    if any(term_matches(text, term) for term in ["Medicaid", "MMIS", "Medicare", "managed care", "provider data"]):
        score += 25
    if any(term_matches(text, term) for term in ["vehicle", "fleet", "automotive", "accessories"]):
        score += 8
    if months_to_end is not None:
        if 0 <= months_to_end <= 18:
            score += 25
        elif months_to_end <= 36:
            score += 18
        elif months_to_end > 36:
            score += 6
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


def cache_filename(url: str) -> str:
    path = urllib.parse.urlparse(url).path
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(path).name) or "document.pdf"
    return f"{hashlib.sha1(url.encode('utf-8')).hexdigest()[:12]}-{name}"


def emit(progress: Callable[[str], None] | None, message: str) -> None:
    if progress:
        progress(message)
