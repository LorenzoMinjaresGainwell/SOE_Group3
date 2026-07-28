from __future__ import annotations

import datetime as dt
import io
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from typing import Any, Callable

from services.state_contracts.tabular import normalized_end_date_fields, record_sort_key
from services.state_normalization import clean_text, compact_raw_json, keyword_hits, stable_id

PAGE_URL = "https://oa.mo.gov/purchasing/contracts"
XLSX_URL = "https://oa.mo.gov/sites/default/files/agcycontracts.xlsx"
SOURCE_NAME = "Missouri OA Agency Contracts"
USER_AGENT = "soe-group3-mo-contracts/0.1"
NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
MAX_WORKBOOK_BYTES = 10_000_000
MAX_EXPANDED_ENTRY_BYTES = 10_000_000


def fetch_contracts(*, vendor_terms: list[str], keywords: list[str], max_per_vendor: int, progress: Callable[[str], None] | None = None) -> list[dict[str, str]]:
    request = urllib.request.Request(XLSX_URL, headers={"User-Agent": USER_AGENT, "Referer": PAGE_URL})
    with urllib.request.urlopen(request, timeout=45) as response:
        rows = parse_xlsx_rows(response.read(MAX_WORKBOOK_BYTES + 1))
    records: list[dict[str, str]] = []
    counts: dict[str, int] = {}
    seen: set[str] = set()
    for row in rows:
        record = normalize_row(row, vendor_terms=vendor_terms, keywords=keywords)
        if not record:
            continue
        query = record["vendor_query"]
        if counts.get(query, 0) >= max(1, max_per_vendor) or record["id"] in seen:
            continue
        seen.add(record["id"]); counts[query] = counts.get(query, 0) + 1; records.append(record)
    emit(progress, f"MO OA agency contracts: scanned {len(rows)} awarded rows, normalized {len(records)} current records")
    return sorted(records, key=record_sort_key, reverse=True)


def read_xlsx_entry(archive: zipfile.ZipFile, name: str) -> bytes:
    info = archive.getinfo(name)
    if info.file_size > MAX_EXPANDED_ENTRY_BYTES:
        raise RuntimeError(f"MO agency contract workbook entry {name} exceeded expanded byte limit")
    with archive.open(info) as handle:
        data = handle.read(MAX_EXPANDED_ENTRY_BYTES + 1)
    if len(data) > MAX_EXPANDED_ENTRY_BYTES:
        raise RuntimeError(f"MO agency contract workbook entry {name} exceeded expanded byte limit")
    return data


def parse_xlsx_rows(data: bytes) -> list[dict[str, str]]:
    if len(data) > MAX_WORKBOOK_BYTES:
        raise RuntimeError("MO agency contract workbook exceeded byte limit")
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        shared: list[str] = []
        if "xl/sharedStrings.xml" in archive.namelist():
            root = ET.fromstring(read_xlsx_entry(archive, "xl/sharedStrings.xml"))
            shared = ["".join(node.text or "" for node in item.iter(NS + "t")) for item in root.findall(NS + "si")]
        root = ET.fromstring(read_xlsx_entry(archive, "xl/worksheets/sheet1.xml"))
    table: list[list[str]] = []
    for row in root.iter(NS + "row"):
        cells: dict[int, str] = {}
        for cell in row.findall(NS + "c"):
            ref = cell.get("r", "A1")
            index = column_index(ref)
            value_node = cell.find(NS + "v")
            value = "" if value_node is None else value_node.text or ""
            if cell.get("t") == "s" and value:
                value = shared[int(value)]
            cells[index] = clean_text(value, 1000)
        table.append([cells.get(i, "") for i in range(max(cells, default=-1) + 1)])
    if not table:
        return []
    headers = table[0]
    return [dict(zip(headers, row + [""] * (len(headers) - len(row)))) for row in table[1:] if any(row)]


def column_index(reference: str) -> int:
    result = 0
    for char in reference:
        if not char.isalpha(): break
        result = result * 26 + ord(char.upper()) - 64
    return result - 1


def excel_date(value: Any) -> str:
    try:
        return (dt.date(1899, 12, 30) + dt.timedelta(days=int(float(value)))).isoformat()
    except (TypeError, ValueError, OverflowError):
        return ""


def normalize_row(row: dict[str, str], *, vendor_terms: list[str], keywords: list[str]) -> dict[str, str] | None:
    number = clean_text(row.get("Contract Number"), 120); vendor = clean_text(row.get("Contractor Name"), 180)
    title = clean_text(row.get("Contract Title"), 500); agency = clean_text(row.get("Agency"), 180)
    end_date, months, end_signal, expired = normalized_end_date_fields(excel_date(row.get("Contract Expiration Date")))
    if not number or not vendor or not title or end_signal == "Unknown end date" or expired:
        return None
    vendor_hits = keyword_hits(vendor, vendor_terms); matched = keyword_hits(" ".join([number, vendor, title, agency]), keywords)
    if not vendor_hits and not matched: return None
    date_score = 25 if months is not None and months <= 18 else 18 if months is not None and months <= 36 else 8 if months is not None else 0
    score = min(100, (35 if vendor_hits else 0) + len(matched) * 8 + date_score)
    raw = {"source_key": "mo_oa_agency_contracts", "source_note": "Official OA awarded agency-contract workbook linked from the contracts page; expired rows rejected.", "row": row}
    return {"id": stable_id("MO", number, vendor, agency, prefix="mo-oa-contract"), "state": "MO", "source": SOURCE_NAME, "source_record_id": f"{number}-{agency}", "parent_id": number,
        "contract_record_type": "parent_contract", "vendor_name": vendor, "vendor_query": vendor_hits[0] if vendor_hits else "", "agency": agency, "contract_number": number, "title": title,
        "amount": "", "execution_date": "", "start_date": excel_date(row.get("Contract Effective Date")), "end_date": end_date, "months_to_end": "" if months is None else str(months),
        "recompete_signal": end_signal, "document_type": "Missouri Agency Contract",
        "document_url": PAGE_URL, "source_url": XLSX_URL, "matched_keywords": ";".join(matched), "relevance_score": str(score), "raw_json": compact_raw_json(raw, limit=7000), "last_checked_at": now_iso()}


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def emit(progress: Callable[[str], None] | None, message: str) -> None:
    if progress: progress(message)
