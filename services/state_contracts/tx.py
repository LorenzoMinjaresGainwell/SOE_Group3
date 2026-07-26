from __future__ import annotations

import datetime as dt
import html
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

from . import tx_dir

TXSMARTBUY_LIST_URL = "https://www.txsmartbuy.gov/app/extensions/CPA/CPAMain/1.0.0/services/BrowseContracts.Service.ss"
TXSMARTBUY_DETAIL_URL = "https://www.txsmartbuy.gov/app/extensions/CPA/CPAMain/1.0.0/services/BrowseContracts.Details.Service.ss"
TXSMARTBUY_SOURCE_URL = "https://www.txsmartbuy.gov/browsecontracts"
TXSMARTBUY_SOURCE_NAME = "TXSmartBuy Statewide Contracts"
USER_AGENT = "soe-group3-tx-contracts/0.1"


def fetch_contracts(
    *,
    vendor_terms: list[str],
    keywords: list[str],
    max_per_vendor: int,
    progress: Callable[[str], None] | None = None,
) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    seen_record_ids: set[str] = set()
    detail_cache: dict[str, dict[str, Any]] = {}
    max_per_search = max(1, min(max_per_vendor, 100))

    for term in unique_terms(vendor_terms):
        rows = search_contract_rows({"urlRoot": "dealers", "contractor": term}, max_records=max_per_search)
        emit(progress, f"TXSmartBuy: contractor={term}: {len(rows)} matching contract/dealer rows")
        for row in rows:
            record = build_record(row, query=term, keywords=keywords, detail_cache=detail_cache, row_kind="dealer")
            if record["id"] in seen_record_ids:
                continue
            seen_record_ids.add(record["id"])
            records.append(record)

    for term in unique_terms(keywords):
        rows = search_contract_rows({"urlRoot": "browsecontracts", "keyword": term}, max_records=max_per_search)
        emit(progress, f"TXSmartBuy: keyword={term}: {len(rows)} matching contract rows")
        for row in rows:
            record = build_record(row, query=term, keywords=keywords, detail_cache=detail_cache, row_kind="contract")
            matched = {item.lower() for item in record["matched_keywords"].split(";") if item}
            if false_keyword_hit(term, record) or term.lower() not in matched or record["id"] in seen_record_ids:
                continue
            seen_record_ids.add(record["id"])
            records.append(record)

    try:
        dir_records = tx_dir.fetch_contracts(
            vendor_terms=vendor_terms,
            keywords=keywords,
            max_per_vendor=max_per_vendor,
            progress=progress,
        )
        emit(progress, f"TX DIR: {len(dir_records)} normalized cooperative contract records")
        for record in dir_records:
            if record["id"] in seen_record_ids:
                continue
            seen_record_ids.add(record["id"])
            records.append(record)
    except Exception as exc:
        emit(progress, f"TX DIR: skipped after TXSmartBuy due to {exc}")

    return sorted(records, key=lambda row: (int(row["relevance_score"]), row["end_date"], row["title"]), reverse=True)


def search_contract_rows(payload_base: dict[str, Any], *, max_records: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    page = 1
    while len(rows) < max_records:
        payload = {**payload_base, "page": page}
        data = http_json(TXSMARTBUY_LIST_URL, payload=payload)
        page_rows = [row for row in data.get("lines") or [] if isinstance(row, dict)]
        if not page_rows:
            break
        rows.extend(page_rows[: max_records - len(rows)])
        records_per_page = int_or_zero(data.get("recordsPerPage")) or len(page_rows)
        total = int_or_zero(data.get("totalRecordsFound"))
        if len(rows) >= max_records or page * records_per_page >= total:
            break
        page += 1
    return rows


def build_record(
    row: dict[str, Any],
    *,
    query: str,
    keywords: list[str],
    detail_cache: dict[str, dict[str, Any]],
    row_kind: str,
) -> dict[str, str]:
    contract_id = str(row.get("contractInternalid") or row.get("internalid") or "").strip()
    detail = get_contract_detail(contract_id, detail_cache) if contract_id else {}
    merged = {**detail, **row}

    line_id = str(row.get("internalid") or contract_id)
    contract_number = clean_text(merged.get("contract") or merged.get("name") or "", 80)
    title = clean_text(strip_html(merged.get("description") or ""), 500)
    vendor_name = vendor_from_row(row, detail, row_kind=row_kind)
    keyword_text = " ".join(
        [
            contract_number,
            title,
            strip_html(merged.get("contractNotes")),
            strip_html(merged.get("contractManagement")),
            strip_html(merged.get("nigpCodes")),
        ]
    )
    matched = keyword_hits(keyword_text, keywords)
    end_date = iso_date(merged.get("endDate"))
    months = months_until(end_date)
    recompete = recompete_signal(months)
    score = relevance_score(matched, recompete, title, vendor_name)
    source_record_id = f"{row_kind}-{line_id}"

    return {
        "id": f"txsmartbuy-{source_record_id}",
        "state": "TX",
        "source": TXSMARTBUY_SOURCE_NAME,
        "source_record_id": source_record_id,
        "vendor_name": vendor_name,
        "vendor_query": query,
        "agency": "Texas Comptroller of Public Accounts",
        "contract_number": contract_number,
        "title": title,
        "amount": "0",
        "execution_date": "",
        "start_date": iso_date(merged.get("startDate")),
        "end_date": end_date,
        "months_to_end": "" if months is None else str(months),
        "recompete_signal": recompete,
        "document_type": clean_text(merged.get("contractType") or "Statewide Contract", 120),
        "document_url": f"{TXSMARTBUY_SOURCE_URL}/{contract_id}" if contract_id else TXSMARTBUY_SOURCE_URL,
        "source_url": TXSMARTBUY_SOURCE_URL,
        "matched_keywords": ";".join(matched),
        "relevance_score": str(score),
        "raw_json": json.dumps({"row": row, "detail": detail}, ensure_ascii=False, sort_keys=True),
        "last_checked_at": now_iso(),
    }


def get_contract_detail(contract_id: str, cache: dict[str, dict[str, Any]]) -> dict[str, Any]:
    if contract_id not in cache:
        url = TXSMARTBUY_DETAIL_URL + "?" + urllib.parse.urlencode({"internalid": contract_id})
        data = http_json(url)
        cache[contract_id] = data if isinstance(data, dict) and not data.get("errorStatusCode") else {}
    return cache[contract_id]


def vendor_from_row(row: dict[str, Any], detail: dict[str, Any], *, row_kind: str) -> str:
    if row_kind == "dealer":
        return clean_text(row.get("name") or "", 180)
    contractors = detail.get("contractors") if isinstance(detail, dict) else []
    names = [clean_text(item.get("contractor"), 120) for item in contractors if isinstance(item, dict) and item.get("contractor")]
    if names:
        joined = "; ".join(names[:3])
        return joined if len(names) <= 3 else joined + f"; +{len(names) - 3} more"
    return "Multiple award vendors"


def http_json(url: str, payload: dict[str, Any] | None = None, timeout: int = 45) -> Any:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Referer": TXSMARTBUY_SOURCE_URL,
        "X-Requested-With": "XMLHttpRequest",
    }
    if payload is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers)
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                parsed = json.loads(response.read().decode("utf-8"))
                if isinstance(parsed, dict) and parsed.get("errorStatusCode"):
                    raise RuntimeError(f"TXSmartBuy error {parsed.get('errorStatusCode')}: {parsed.get('errorMessage')}")
                return parsed
        except urllib.error.HTTPError as exc:
            body = exc.read(600).decode("utf-8", "replace")
            raise RuntimeError(f"HTTP {exc.code} from {url}: {body}") from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, RuntimeError) as exc:
            last_error = exc
            time.sleep(1 + attempt)
    raise RuntimeError(f"TXSmartBuy request failed: {last_error}")


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


def keyword_hits(text: str, keywords: list[str]) -> list[str]:
    return sorted({keyword for keyword in keywords if keyword and term_matches(text, keyword)}, key=str.lower)


def term_matches(text: str, term: str) -> bool:
    parts = [re.escape(part) for part in re.split(r"\s+", term.strip()) if part]
    if not parts:
        return False
    pattern = r"(?<![A-Za-z0-9])" + r"\s+".join(parts) + r"(?![A-Za-z0-9])"
    return re.search(pattern, text, re.IGNORECASE) is not None


def false_keyword_hit(term: str, record: dict[str, str]) -> bool:
    text = " ".join([record.get("title", ""), record.get("contract_number", ""), record.get("raw_json", "")])
    return term.lower() == "mmis" and term_matches(text, "commissary") and not term_matches(text, "MMIS")


def relevance_score(keywords: list[str], recompete: str, title: str, vendor_name: str) -> int:
    score = min(45, len(keywords) * 8)
    text = " ".join([title, vendor_name])
    if term_matches(text, "Medicaid") or term_matches(text, "MMIS"):
        score += 25
    if any(term_matches(text, term) for term in ["information technology", "technology", "software"]):
        score += 12
    if recompete == "Expiring soon":
        score += 25
    elif recompete == "Recompete watch":
        score += 18
    elif recompete == "Open-ended/placeholder end date":
        score += 8
    return min(score, 100)


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


def months_until(value: Any) -> int | None:
    parsed = parse_date(value)
    if not parsed:
        return None
    today = dt.date.today()
    return (parsed.year - today.year) * 12 + (parsed.month - today.month)


def parse_date(value: Any) -> dt.date | None:
    if not value:
        return None
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return dt.datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    return None


def iso_date(value: Any) -> str:
    parsed = parse_date(value)
    return parsed.isoformat() if parsed else ""


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def strip_html(value: Any) -> str:
    text = re.sub(r"<[^>]+>", " ", str(value or ""))
    return html.unescape(text)


def clean_text(value: Any, limit: int) -> str:
    text = re.sub(r"\s+", " ", strip_html(value)).strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "..."


def int_or_zero(value: Any) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


def emit(progress: Callable[[str], None] | None, message: str) -> None:
    if progress:
        progress(message)
