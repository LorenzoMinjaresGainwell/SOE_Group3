from __future__ import annotations

import datetime as dt
import html
import json
import re
import urllib.parse
import zipfile
import xml.etree.ElementTree as ET
from io import BytesIO
from typing import Any, Callable

from services.state_http import fetch_url
from services.state_contracts.keyword_context import useful_keyword_match as keyword_context_match
from services.state_normalization import clean_text, compact_raw_json, iso_date, keyword_hits, months_until, stable_id, term_matches

CONTRACT_SEARCH_PAGE_URL = "https://www.dms.myflorida.com/contract_search"
CONTRACT_SEARCH_API_URL = "https://dms-media.ccplatform.net/api/search_contracts"
DMS_BASE_URL = "https://www.dms.myflorida.com"
SOURCE_NAME = "Florida DMS State Contracts and Agreements"
USER_AGENT = "soe-group3-fl-dms-contracts/0.1"


def fetch_contracts(
    *,
    vendor_terms: list[str],
    keywords: list[str],
    max_per_vendor: int,
    progress: Callable[[str], None] | None = None,
) -> list[dict[str, str]]:
    rows = fetch_current_contract_rows()
    emit(progress, f"FL DMS contract search: scanned {len(rows)} current contract/agreement rows")

    records: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in rows:
        for record in normalize_search_row(row, vendor_terms=vendor_terms, keywords=keywords):
            if record["id"] in seen:
                continue
            seen.add(record["id"])
            records.append(record)

    limit = max(1, max_per_vendor) * max(1, len(unique_terms(vendor_terms)))
    return sorted(records, key=contract_sort_key, reverse=True)[:limit]


def fetch_current_contract_rows() -> list[dict[str, Any]]:
    data = fetch_search_json({"q": "", "page": 1})
    rows = data.get("resultsObjects") if isinstance(data, dict) else []
    return [row for row in rows or [] if isinstance(row, dict)]


def fetch_search_json(params: dict[str, Any]) -> dict[str, Any]:
    url = CONTRACT_SEARCH_API_URL + "?" + urllib.parse.urlencode(params)
    result = fetch_url(
        url,
        headers={"Accept": "application/json,*/*", "Origin": DMS_BASE_URL, "Referer": CONTRACT_SEARCH_PAGE_URL},
        timeout=60,
        byte_limit=1_000_000,
        user_agent=USER_AGENT,
    )
    result.raise_for_status()
    data = json.loads(result.body_text())
    return data if isinstance(data, dict) else {}


def normalize_search_row(row: dict[str, Any], *, vendor_terms: list[str], keywords: list[str]) -> list[dict[str, str]]:
    contract_number = clean_text(row.get("number"), 120)
    title = clean_text(row.get("name") or contract_number, 500)
    end_date = iso_date(row.get("endDate"))
    detail_url = nested_text(row, "link", "url")
    if not contract_number or not title or not end_date or not detail_url:
        return []

    detail = fetch_detail(detail_url)
    detail_text = detail_search_text(detail)
    contractors = contractor_rows_for_detail(detail)
    contractor_names = [clean_text(item.get("Contractor Name") or item.get("contractor_name"), 180) for item in contractors]
    contractor_names = unique_terms([name for name in contractor_names if name])

    base_text = " ".join(
        [
            contract_number,
            title,
            clean_text(row.get("category"), 200),
            clean_text(row.get("type"), 120),
            clean_text(row.get("admin"), 120),
            detail_text,
            " ".join(contractor_names[:100]),
        ]
    )
    matched = keyword_hits(base_text, keywords)
    vendor_matches = matching_contractors(contractor_names, vendor_terms)
    if not vendor_matches and (not contractor_names or not useful_keyword_match(matched, base_text)):
        return []

    records: list[dict[str, str]] = []
    if vendor_matches:
        for vendor_name, hits in vendor_matches:
            records.append(build_record(row, detail, vendor_name=vendor_name, vendor_hits=hits, matched=matched, contractor_names=contractor_names))
        return records

    records.append(
        build_record(
            row,
            detail,
            vendor_name=summarize_contractors(contractor_names),
            vendor_hits=[],
            matched=matched,
            contractor_names=contractor_names,
        )
    )
    return records


def fetch_detail(url: str) -> dict[str, Any]:
    result = fetch_url(
        url,
        headers={"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8", "Referer": CONTRACT_SEARCH_PAGE_URL},
        timeout=60,
        byte_limit=1_000_000,
        user_agent=USER_AGENT,
    )
    if not result.ok:
        return {"detail_error": result.metadata(), "detail_url": url}
    match = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', result.body_text(), re.DOTALL)
    if not match:
        return {"detail_url": url, "detail_error": "__NEXT_DATA__ not found"}
    data = json.loads(html.unescape(match.group(1)))
    page_props = ((data.get("props") or {}).get("pageProps") or {}) if isinstance(data, dict) else {}
    return {
        "detail_url": url,
        "pageData": page_props.get("pageData") or {},
        "childrenInfos": page_props.get("childrenInfos") or [],
    }


def detail_search_text(detail: dict[str, Any]) -> str:
    page_data = detail.get("pageData") if isinstance(detail.get("pageData"), dict) else {}
    parts = [
        clean_text(page_data.get("name"), 500),
        clean_text(page_data.get("number"), 120),
        rich_text(page_data.get("description")),
        rich_text(page_data.get("additionalInformation")),
        rich_text(page_data.get("benefits")),
        clean_text(page_data.get("newCategory"), 200),
    ]
    for child in detail.get("childrenInfos") or []:
        content = ((child.get("props") or {}).get("content") or {}) if isinstance(child, dict) else {}
        fields = content.get("fields") if isinstance(content, dict) else {}
        parts.extend([clean_text(content.get("name"), 300), clean_text((fields or {}).get("name"), 200), clean_text((fields or {}).get("type"), 120)])
    return " ".join(part for part in parts if part)


def contractor_rows_for_detail(detail: dict[str, Any]) -> list[dict[str, str]]:
    attachment = contractor_attachment(detail)
    if not attachment:
        return []
    uri = clean_text(attachment.get("uri"), 500)
    if not uri:
        return []
    url = urllib.parse.urljoin(DMS_BASE_URL, uri)
    result = fetch_url(
        url,
        headers={"Accept": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,*/*", "Referer": clean_text(detail.get("detail_url"), 700)},
        timeout=60,
        byte_limit=5_000_000,
        user_agent=USER_AGENT,
    )
    if not result.ok or not result.body.startswith(b"PK"):
        return []
    return parse_xlsx_table(result.body)


def contractor_attachment(detail: dict[str, Any]) -> dict[str, Any]:
    for child in detail.get("childrenInfos") or []:
        content = ((child.get("props") or {}).get("content") or {}) if isinstance(child, dict) else {}
        fields = content.get("fields") if isinstance(content, dict) else {}
        name = clean_text((fields or {}).get("name") or content.get("name"), 120).lower()
        attachment = (fields or {}).get("attachment") if isinstance(fields, dict) else None
        if attachment and "contractor" in name:
            return attachment if isinstance(attachment, dict) else {}
    return {}


def parse_xlsx_table(content: bytes) -> list[dict[str, str]]:
    with zipfile.ZipFile(BytesIO(content)) as archive:
        strings = shared_strings(archive)
        sheet_name = first_sheet_name(archive)
        if not sheet_name:
            return []
        root = ET.fromstring(archive.read(sheet_name))

    rows = worksheet_rows(root, strings)
    header_index = next((index for index, row in enumerate(rows) if any(clean_text(cell).lower() == "contractor name" for cell in row)), -1)
    if header_index < 0:
        return []
    headers = [clean_header(cell) for cell in rows[header_index]]
    result: list[dict[str, str]] = []
    for row in rows[header_index + 1 :]:
        values = {headers[index]: clean_text(row[index], 500) for index in range(min(len(headers), len(row))) if headers[index]}
        if values.get("Contractor Name"):
            result.append(values)
    return result


def shared_strings(archive: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []
    ns = {"a": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
    strings: list[str] = []
    for item in root.findall("a:si", ns):
        strings.append("".join(node.text or "" for node in item.findall(".//a:t", ns)))
    return strings


def first_sheet_name(archive: zipfile.ZipFile) -> str:
    for name in archive.namelist():
        if name.startswith("xl/worksheets/sheet") and name.endswith(".xml"):
            return name
    return ""


def worksheet_rows(root: ET.Element, strings: list[str]) -> list[list[str]]:
    ns = {"a": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    rows: list[list[str]] = []
    for row in root.findall(".//a:row", ns):
        values: list[str] = []
        for cell in row.findall("a:c", ns):
            index = column_index(cell.attrib.get("r", ""))
            while len(values) <= index:
                values.append("")
            values[index] = cell_value(cell, strings, ns)
        rows.append(values)
    return rows


def cell_value(cell: ET.Element, strings: list[str], ns: dict[str, str]) -> str:
    if cell.attrib.get("t") == "inlineStr":
        return clean_text("".join(node.text or "" for node in cell.findall(".//a:t", ns)), 500)
    value = cell.find("a:v", ns)
    text = "" if value is None else value.text or ""
    if cell.attrib.get("t") == "s" and text.isdigit():
        return clean_text(strings[int(text)] if int(text) < len(strings) else "", 500)
    return clean_text(text, 500)


def column_index(cell_ref: str) -> int:
    letters = re.sub(r"[^A-Z]", "", cell_ref.upper())
    index = 0
    for letter in letters:
        index = index * 26 + (ord(letter) - ord("A") + 1)
    return max(index - 1, 0)


def clean_header(value: Any) -> str:
    text = clean_text(value, 80)
    aliases = {"Contractor name": "Contractor Name", "Contact name": "Contact Name"}
    return aliases.get(text, text)


def matching_contractors(contractor_names: list[str], vendor_terms: list[str]) -> list[tuple[str, list[str]]]:
    matches: list[tuple[str, list[str]]] = []
    for contractor in contractor_names:
        hits = keyword_hits(contractor, unique_terms(vendor_terms))
        if hits:
            matches.append((contractor, hits))
    return matches


def build_record(
    row: dict[str, Any],
    detail: dict[str, Any],
    *,
    vendor_name: str,
    vendor_hits: list[str],
    matched: list[str],
    contractor_names: list[str],
) -> dict[str, str]:
    contract_number = clean_text(row.get("number"), 120)
    title = clean_text(row.get("name") or contract_number, 500)
    detail_url = nested_text(row, "link", "url")
    start_date = iso_date(row.get("startDate"))
    end_date = iso_date(row.get("endDate"))
    months = months_until(end_date)
    recompete = recompete_signal(months)
    base_record_type = contract_record_type(detail_url)
    record_type = "dealer_line" if vendor_hits and len(contractor_names) > 1 else base_record_type
    source_record_id = contract_number if record_type != "dealer_line" else f"{contract_number}-{vendor_name}"
    raw = {
        "source_key": "fl_dms_contract_search",
        "source_note": "Official Florida DMS Contract Search API and contract detail pages; contractor attachment XLSX supplies awarded vendor names.",
        "search_row": row,
        "detail_url": detail_url,
        "detail_page": detail_summary(detail),
        "contractor_count": len(contractor_names),
        "matched_vendor": vendor_name if vendor_hits else "",
    }

    return {
        "id": stable_id("FL", source_record_id, title, prefix="fl-dms-contract"),
        "state": "FL",
        "source": SOURCE_NAME,
        "source_record_id": source_record_id,
        "parent_id": contract_number,
        "contract_record_type": record_type,
        "vendor_name": clean_text(vendor_name, 180),
        "vendor_query": ";".join(vendor_hits),
        "agency": "Florida Department of Management Services, State Purchasing",
        "contract_number": contract_number,
        "title": title,
        "amount": "0",
        "execution_date": "",
        "start_date": start_date,
        "end_date": end_date,
        "months_to_end": "" if months is None else str(months),
        "recompete_signal": recompete,
        "document_type": document_type(detail_url),
        "document_url": detail_url,
        "source_url": CONTRACT_SEARCH_PAGE_URL,
        "matched_keywords": ";".join(matched),
        "relevance_score": str(relevance_score(vendor_hits, matched, recompete, " ".join([title, vendor_name, detail_search_text(detail)]), record_type)),
        "raw_json": compact_raw_json(raw, limit=10000),
        "last_checked_at": now_iso(),
    }


def detail_summary(detail: dict[str, Any]) -> dict[str, Any]:
    page_data = detail.get("pageData") if isinstance(detail.get("pageData"), dict) else {}
    children = []
    for child in detail.get("childrenInfos") or []:
        content = ((child.get("props") or {}).get("content") or {}) if isinstance(child, dict) else {}
        fields = content.get("fields") if isinstance(content, dict) else {}
        attachment = (fields or {}).get("attachment") if isinstance(fields, dict) else None
        children.append(
            {
                "name": content.get("name"),
                "type": (fields or {}).get("type") if isinstance(fields, dict) else "",
                "attachment": attachment,
            }
        )
    return {
        "name": page_data.get("name"),
        "number": page_data.get("number"),
        "newCategory": page_data.get("newCategory"),
        "children": children,
    }


def useful_keyword_match(matches: list[str], text: str) -> bool:
    return keyword_context_match(matches, text)


def contract_record_type(detail_url: str) -> str:
    lower = detail_url.lower()
    if "/alternate_contract_source/" in lower:
        return "cooperative_contract"
    if "/state_term_contract/" in lower:
        return "master_agreement"
    if "/state_purchasing_agreements/" in lower:
        return "parent_contract"
    return "parent_contract"


def document_type(detail_url: str) -> str:
    lower = detail_url.lower()
    if "/alternate_contract_source/" in lower:
        return "Alternate Contract Source"
    if "/state_term_contract/" in lower:
        return "State Term Contract"
    if "/state_purchasing_agreements/" in lower:
        return "State Purchasing Agreement"
    return "Florida DMS Contract"


def relevance_score(vendor_hits: list[str], matches: list[str], recompete: str, text: str, record_type: str) -> int:
    score = min(45, len(matches) * 8)
    if vendor_hits:
        score += 35
    if any(term_matches(text, term) for term in ["Medicaid", "MMIS", "managed care", "provider data", "Medicare"]):
        score += 25
    if any(term_matches(text, term) for term in ["health", "medical", "claims", "eligibility", "CMS", "benefits"]):
        score += 14
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


def summarize_contractors(names: list[str]) -> str:
    if not names:
        return "Multiple award vendors"
    shown = names[:3]
    suffix = f"; +{len(names) - len(shown)} more" if len(names) > len(shown) else ""
    return "; ".join(shown) + suffix


def rich_text(value: Any) -> str:
    if isinstance(value, dict):
        return strip_html(value.get("html5") or value.get("html") or "")
    return strip_html(value)


def strip_html(value: Any) -> str:
    return clean_text(re.sub(r"(?is)<[^>]+>", " ", str(value or "")), 4000)


def nested_text(row: dict[str, Any], *keys: str) -> str:
    value: Any = row
    for key in keys:
        if not isinstance(value, dict):
            return ""
        value = value.get(key)
    return clean_text(value, 700)


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


def int_or_zero(value: Any) -> int:
    try:
        return int(float(clean_text(value).replace(",", "") or 0))
    except (TypeError, ValueError):
        return 0


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def emit(progress: Callable[[str], None] | None, message: str) -> None:
    if progress:
        progress(message)
