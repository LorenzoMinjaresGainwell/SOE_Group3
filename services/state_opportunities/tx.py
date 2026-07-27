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

ESBD_LIST_URL = "https://www.txsmartbuy.gov/app/extensions/CPA/CPAMain/1.0.0/services/ESBD.Service.ss"
ESBD_SOURCE_URL = "https://www.txsmartbuy.gov/esbd"
ESBD_SOURCE_NAME = "TXSmartBuy ESBD Solicitations"
USER_AGENT = "soe-group3-tx-esbd/0.1"


def fetch_opportunities(
    *,
    keywords: list[str],
    days_back: int,
    max_records: int,
    progress: Callable[[str], None] | None = None,
) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    seen: set[str] = set()
    limit = max(1, max_records)

    for term in unique_terms(keywords):
        rows = search_esbd(term, max_records=limit)
        emit(progress, f"TX ESBD: keyword={term}: {len(rows)} posted/addendum rows")
        for row in rows:
            record = normalize_opportunity(row, keywords=keywords)
            matched = {item.lower() for item in record["matched_keywords"].split(";") if item}
            if false_keyword_hit(term, record) or term.lower() not in matched:
                continue
            if not useful_keyword_match(record["matched_keywords"].split(";"), record["raw_json"]):
                continue
            if not is_open_or_recent(record["posted_date"], record["due_date"], days_back):
                continue
            if record["id"] in seen:
                continue
            seen.add(record["id"])
            records.append(record)
            if len(records) >= limit:
                break
        if len(records) >= limit:
            break
        time.sleep(0.25)

    return sorted(records, key=record_sort_key, reverse=True)[:limit]


def search_esbd(keyword: str, *, max_records: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    page = 1
    while len(rows) < max_records:
        payload = {"urlRoot": "esbd", "keyword": keyword, "status": "1", "page": page}
        data = http_json(ESBD_LIST_URL, payload=payload)
        page_rows = [row for row in data.get("lines") or [] if isinstance(row, dict)]
        if not page_rows:
            break
        rows.extend(page_rows[: max_records - len(rows)])
        records_per_page = int_or_zero(data.get("recordsPerPage")) or len(page_rows)
        total = int_or_zero(data.get("totalRecordsFound"))
        if len(rows) >= max_records or page * records_per_page >= total:
            break
        page += 1
        time.sleep(0.25)
    return rows


def normalize_opportunity(row: dict[str, Any], *, keywords: list[str]) -> dict[str, str]:
    solicitation_id = clean_text(row.get("solicitationId") or row.get("internalid") or "", 120)
    internal_id = clean_text(row.get("internalid") or solicitation_id, 80)
    title = clean_text(row.get("title") or solicitation_id, 500)
    agency = clean_text(row.get("agencyName") or row.get("agencyNumber") or "", 180)
    status = clean_text(row.get("statusName") or row.get("status") or "", 80)
    nigp_codes = clean_text(row.get("nigpCodes") or "", 1200)
    search_text = " ".join([solicitation_id, title, agency, status, nigp_codes])
    matched = keyword_hits(search_text, keywords)
    document_url = clean_text(row.get("repostURL") or row.get("url") or "", 500)
    if not document_url:
        document_url = ESBD_SOURCE_URL + "/" + urllib.parse.quote(solicitation_id, safe="")

    raw = dict(row)
    raw["source_note"] = "Official TXSmartBuy ESBD SuiteCommerce JSON service; status=1 also returns addendum-posted rows."

    return {
        "id": f"tx-esbd-{slug_id(solicitation_id or internal_id)}",
        "state": "TX",
        "source": ESBD_SOURCE_NAME,
        "source_record_id": solicitation_id or internal_id,
        "title": title,
        "agency": agency,
        "document_type": "ESBD Solicitation",
        "posted_date": iso_date(row.get("postingDate") or row.get("created")),
        "due_date": iso_date(row.get("responseDue")),
        "status": status,
        "amount": "",
        "document_url": document_url,
        "source_url": ESBD_SOURCE_URL,
        "matched_keywords": ";".join(matched),
        "relevance_score": str(relevance_score(matched, status, search_text)),
        "raw_json": json.dumps(raw, ensure_ascii=False, sort_keys=True),
        "last_checked_at": now_iso(),
    }


def http_json(url: str, payload: dict[str, Any], timeout: int = 60) -> Any:
    data = json.dumps(payload).encode("utf-8")
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Content-Type": "application/json",
        "Referer": ESBD_SOURCE_URL,
        "X-Requested-With": "XMLHttpRequest",
    }
    request = urllib.request.Request(url, data=data, headers=headers)
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                parsed = json.loads(response.read().decode("utf-8"))
                if isinstance(parsed, dict) and parsed.get("errorStatusCode"):
                    raise RuntimeError(f"TX ESBD error {parsed.get('errorStatusCode')}: {parsed.get('errorMessage')}")
                return parsed
        except urllib.error.HTTPError as exc:
            body = exc.read(600).decode("utf-8", "replace")
            raise RuntimeError(f"HTTP {exc.code} from {url}: {body}") from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, RuntimeError) as exc:
            last_error = exc
            time.sleep(1 + attempt)
    raise RuntimeError(f"TX ESBD request failed: {last_error}")


def keyword_hits(text: str, keywords: list[str]) -> list[str]:
    return sorted({keyword for keyword in keywords if keyword and term_matches(text, keyword)}, key=str.lower)


def term_matches(text: str, term: str) -> bool:
    parts = [re.escape(part) for part in re.split(r"\s+", term.strip()) if part]
    if not parts:
        return False
    pattern = r"(?<![A-Za-z0-9])" + r"\s+".join(parts) + r"(?![A-Za-z0-9])"
    return re.search(pattern, text, re.IGNORECASE) is not None


def false_keyword_hit(term: str, record: dict[str, str]) -> bool:
    text = " ".join([record.get("title", ""), record.get("raw_json", "")])
    return term.lower() == "mmis" and term_matches(text, "commissary") and not term_matches(text, "MMIS")


def useful_keyword_match(matches: list[str], text: str) -> bool:
    generic_terms = {"claims", "eligibility", "enrollment"}
    matched_terms = {match.lower() for match in matches if match}
    if not matched_terms or not matched_terms <= generic_terms:
        return True
    context_terms = [
        "health & human services",
        "health and human services",
        "department of state health services",
        "medicaid",
        "medicare",
        "medical",
        "health",
        "hospital",
        "behavioral",
        "managed care",
        "provider",
        "chip",
        "mmis",
    ]
    return any(term_matches(text, term) for term in context_terms)


def relevance_score(matches: list[str], status: str, text: str) -> int:
    score = min(50, len(matches) * 10)
    if term_matches(text, "Medicaid") or term_matches(text, "MMIS") or term_matches(text, "Health & Human Services"):
        score += 25
    if any(term_matches(text, term) for term in ["eligibility", "claims", "managed care", "interoperability", "FHIR", "prior authorization"]):
        score += 15
    if term_matches(text, "rural health"):
        score += 25
    if any(term_matches(text, term) for term in ["RFP", "RFI", "RFQ", "software", "data hosting", "analytics"]):
        score += 12
    if status.lower() in {"posted", "addendum posted", "open", "upcoming"}:
        score += 10
    return min(score, 100)


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


def parse_date(value: Any) -> dt.date | None:
    if not value:
        return None
    text = clean_text(value, 80)
    numeric = re.search(r"\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b", text)
    if numeric:
        text = numeric.group(0)
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%m-%d-%Y", "%m-%d-%y"):
        try:
            return dt.datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    return None


def iso_date(value: Any) -> str:
    parsed = parse_date(value)
    return parsed.isoformat() if parsed else ""


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


def clean_text(value: Any, limit: int) -> str:
    text = html.unescape(re.sub(r"<[^>]+>", " ", str(value or "")))
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "..."


def slug_id(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip()).strip("-").lower()
    return slug or "unknown"


def record_sort_key(row: dict[str, str]) -> tuple[int, str, str]:
    return (int_or_zero(row.get("relevance_score")), row.get("due_date", ""), row.get("posted_date", ""))


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
