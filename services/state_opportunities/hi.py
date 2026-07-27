from __future__ import annotations

import datetime as dt
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar
from typing import Any, Callable

from services.state_normalization import clean_id, clean_text, compact_raw_json, iso_date, keyword_hits, parse_date, stable_id, term_matches

BASE_URL = "https://hands.ehawaii.gov/hands/"
SEARCH_PAGE_URL = urllib.parse.urljoin(BASE_URL, "opportunities")
API_BASE_URL = urllib.parse.urljoin(BASE_URL, "api/")
CONFIG_URL = urllib.parse.urljoin(API_BASE_URL, "config")
SEARCH_URL = urllib.parse.urljoin(API_BASE_URL, "bidding-opportunities")
DETAIL_URL = urllib.parse.urljoin(API_BASE_URL, "opportunity")
USER_AGENT = "Mozilla/5.0 soe-group3-hi-hands-opportunities/0.1"
SOURCE_NAME = "Hawaii Awards and Notices Data System"
SOURCE_NOTE = (
    "Official HANDS Angular public JSON API: POST /hands/api/bidding-opportunities for current notices "
    "and GET /hands/api/opportunity?id=<id> for HANDS-hosted notice details."
)
PAGE_SIZE = 100
MAX_SCAN_ROWS = 5000


class HawaiiHandsClient:
    def __init__(self) -> None:
        self.cookie_jar = CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cookie_jar))
        self.initialized = False

    def prime(self) -> None:
        if self.initialized:
            return
        self._request_text(BASE_URL, accept="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8")
        self._request_json(CONFIG_URL, method="GET")
        self.initialized = True

    def search_open(self, *, page: int, size: int) -> dict[str, Any]:
        self.prime()
        url = SEARCH_URL + "?" + urllib.parse.urlencode({"size": str(size), "page": str(page), "sort": "publish_date_dt,desc"})
        return self._request_json(url, method="POST", payload=search_payload(), referer=SEARCH_PAGE_URL)

    def opportunity_detail(self, row: dict[str, Any]) -> dict[str, Any] | None:
        opportunity_id = clean_id(row.get("id"))
        if not opportunity_id or clean_text(row.get("system")).upper() != "HANDS":
            return None
        url = DETAIL_URL + "?" + urllib.parse.urlencode({"id": opportunity_id})
        try:
            payload = self._request_json(url, method="GET", referer=detail_page_url(row))
        except Exception:
            return None
        data = payload.get("data") if isinstance(payload, dict) else {}
        detail = data.get("opportunity") if isinstance(data, dict) else None
        return detail if isinstance(detail, dict) else None

    def _request_json(
        self,
        url: str,
        *,
        method: str,
        payload: dict[str, Any] | None = None,
        referer: str = BASE_URL,
        timeout: int = 60,
    ) -> dict[str, Any]:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "Referer": referer,
        }
        if data is not None:
            headers["Content-Type"] = "application/json"
            headers["Origin"] = "https://hands.ehawaii.gov"
        last_error = ""
        for attempt in range(3):
            request = urllib.request.Request(url, data=data, headers=headers, method=method)
            try:
                with self.opener.open(request, timeout=timeout) as response:
                    text = response.read().decode("utf-8", "replace")
                    parsed = json.loads(text)
                    if not isinstance(parsed, dict):
                        raise RuntimeError("HANDS JSON response root was not an object")
                    return parsed
            except urllib.error.HTTPError as exc:
                body = exc.read(500).decode("utf-8", "replace")
                last_error = f"HTTP {exc.code}: {body[:200]}"
            except (OSError, TimeoutError, json.JSONDecodeError, RuntimeError) as exc:
                last_error = str(exc)
            time.sleep(1 + attempt)
        raise RuntimeError(f"HI HANDS request failed for {url}: {last_error}")

    def _request_text(self, url: str, *, accept: str, timeout: int = 60) -> str:
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept})
        with self.opener.open(request, timeout=timeout) as response:
            return response.read().decode("utf-8", "replace")


def fetch_opportunities(
    *,
    keywords: list[str],
    days_back: int,
    max_records: int,
    progress: Callable[[str], None] | None = None,
) -> list[dict[str, str]]:
    client = HawaiiHandsClient()
    rows = fetch_open_rows(client, max_records=max_records, progress=progress)
    emit(progress, f"HI HANDS bidding opportunities: {len(rows)} public rows")

    records: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in rows:
        detail = client.opportunity_detail(row) if should_fetch_detail(row, keywords=keywords) else None
        record = normalize_opportunity(row, detail=detail, keywords=keywords)
        if not record.get("source_record_id") or record["id"] in seen:
            continue
        if record["status"].lower() in {"cancelled", "canceled"}:
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


def fetch_open_rows(
    client: HawaiiHandsClient,
    *,
    max_records: int,
    progress: Callable[[str], None] | None = None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    page = 0
    total: int | None = None
    max_to_scan = min(MAX_SCAN_ROWS, max(PAGE_SIZE * 2, max(1, max_records) * 50))

    while len(rows) < max_to_scan:
        page_size = min(PAGE_SIZE, max_to_scan - len(rows))
        payload = client.search_open(page=page, size=page_size)
        search_result = search_result_payload(payload)
        batch = valid_rows(search_result.get("content"))
        if total is None:
            total = int_or_zero(search_result.get("totalElements") or payload.get("data", {}).get("total"))
            if total:
                max_to_scan = min(max_to_scan, total)
        emit(progress, f"HI HANDS page {page + 1}: {len(batch)} rows")
        if not batch:
            break
        rows.extend(batch[: max_to_scan - len(rows)])
        if search_result.get("last") is True:
            break
        if total is not None and len(rows) >= total:
            break
        page += 1
        time.sleep(0.15)

    return rows[:max_to_scan]


def search_result_payload(payload: dict[str, Any]) -> dict[str, Any]:
    data = payload.get("data") if isinstance(payload, dict) else {}
    search_result = data.get("searchResult") if isinstance(data, dict) else {}
    if not isinstance(search_result, dict):
        raise RuntimeError("HI HANDS search response missing data.searchResult")
    return search_result


def search_payload() -> dict[str, Any]:
    return {
        "query": "",
        "showClosed": False,
        "showCancelled": False,
        "omitPagination": False,
        "categories": [],
        "procurementCategory": "",
        "department": "",
        "islands": [],
        "statuses": ["POSTED"],
        "publishDate": "",
        "offerDueDate": "",
        "jurisdiction": "",
    }


def normalize_opportunity(row: dict[str, Any], *, detail: dict[str, Any] | None, keywords: list[str]) -> dict[str, str]:
    detail = detail or {}
    opportunity_id = clean_id(detail.get("id") or row.get("id"))
    source_record_id = clean_text(detail.get("solicitationNumber") or row.get("solicitionNo") or opportunity_id, 180)
    title = clean_text(detail.get("service") or row.get("title") or source_record_id, 500)
    agency = clean_text(detail.get("department") or row.get("department"), 180)
    status = status_text(row, detail)
    posted_date = iso_date(detail.get("publishedDate") or row.get("publishDate"))
    due_date = iso_date(detail.get("dueDate") or row.get("dueDate"))
    document_url = preferred_document_url(row, detail)
    search_text = opportunity_search_text(row, detail)
    matched = keyword_hits(search_text, keywords)
    raw = {
        "source_key": "hi_hands",
        "source_note": SOURCE_NOTE,
        "detail_url": detail_page_url(row),
        "source_payload": row,
    }
    if detail:
        raw["detail"] = detail

    return {
        "id": stable_id("HI", source_record_id or opportunity_id, prefix="hi-hands-opportunity"),
        "state": "HI",
        "source": SOURCE_NAME,
        "source_record_id": source_record_id,
        "title": title,
        "agency": agency,
        "document_type": document_type(row, detail, source_record_id, title),
        "posted_date": posted_date,
        "due_date": due_date,
        "status": status,
        "amount": "",
        "document_url": document_url,
        "source_url": SEARCH_PAGE_URL,
        "matched_keywords": ";".join(matched),
        "relevance_score": str(relevance_score(matched, status, search_text, due_date)),
        "raw_json": compact_raw_json(raw, limit=10000),
        "last_checked_at": now_iso(),
    }


def should_fetch_detail(row: dict[str, Any], *, keywords: list[str]) -> bool:
    if clean_text(row.get("system")).upper() != "HANDS":
        return False
    text = row_search_text(row)
    if keyword_hits(text, keywords):
        return True
    return any(term_matches(text, term) for term in health_agency_terms())


def preferred_document_url(row: dict[str, Any], detail: dict[str, Any]) -> str:
    details_url = clean_text(row.get("detailsUrl"), 500)
    if details_url:
        return details_url
    for attachment in detail.get("attachments") or []:
        if not isinstance(attachment, dict):
            continue
        url = clean_text(attachment.get("detailsURL"), 500)
        if url:
            return url
    return detail_page_url(row)


def detail_page_url(row: dict[str, Any]) -> str:
    details_url = clean_text(row.get("detailsUrl"), 500)
    if details_url:
        return details_url
    opportunity_id = clean_id(row.get("id"))
    if not opportunity_id:
        return SEARCH_PAGE_URL
    return urllib.parse.urljoin(BASE_URL, f"opportunities/opportunity-details/{urllib.parse.quote(opportunity_id)}")


def status_text(row: dict[str, Any], detail: dict[str, Any]) -> str:
    if clean_text(detail.get("cancellationReason")):
        return "Cancelled"
    if detail.get("closed") is True or row.get("closed") is True:
        return "Closed"
    return clean_text(detail.get("status") or row.get("status") or "Open", 80)


def document_type(row: dict[str, Any], detail: dict[str, Any], source_record_id: str, title: str) -> str:
    category = clean_text(detail.get("category") or row.get("category"), 120)
    text = " ".join([source_record_id, title, category, clean_text(detail.get("description"), 1000)]).upper()
    if code_matches(text, "RFI") or "REQUEST FOR INFORMATION" in text:
        return "Hawaii Request for Information"
    if code_matches(text, "RFP") or "REQUEST FOR PROPOS" in text:
        return "Hawaii Request for Proposal"
    if code_matches(text, "RFQ") or "REQUEST FOR QUOTE" in text:
        return "Hawaii Request for Quote"
    if code_matches(text, "IFB") or "INVITATION FOR BID" in text:
        return "Hawaii Invitation for Bids"
    if "SOLE SOURCE" in text:
        return "Hawaii Sole Source Notice"
    if category:
        return f"Hawaii {category} Opportunity"
    return "Hawaii Bidding Opportunity"


def opportunity_search_text(row: dict[str, Any], detail: dict[str, Any]) -> str:
    parts = [row_search_text(row)]
    parts.extend(
        [
            detail.get("solicitationNumber"),
            detail.get("service"),
            detail.get("description"),
            detail.get("comments"),
            detail.get("department"),
            detail.get("division"),
            detail.get("branch"),
            detail.get("buyerName"),
            detail.get("buyerEmail"),
            detail.get("category"),
            detail.get("procurementCategory"),
            detail.get("jurisdiction"),
            detail.get("island"),
        ]
    )
    for attachment in detail.get("attachments") or []:
        if isinstance(attachment, dict):
            parts.extend([attachment.get("name"), attachment.get("file")])
    for commodity in detail.get("commodityCodes") or []:
        if isinstance(commodity, dict):
            parts.extend([commodity.get("code"), commodity.get("description")])
    return expand_related_terms(" ".join(clean_text(part, 4000) for part in parts if part))


def row_search_text(row: dict[str, Any]) -> str:
    parts = [
        row.get("solicitionNo"),
        row.get("title"),
        row.get("department"),
        row.get("division"),
        row.get("category"),
        row.get("jurisdiction"),
        row.get("island"),
        row.get("status"),
        row.get("system"),
    ]
    return expand_related_terms(" ".join(clean_text(part, 1000) for part in parts if part))


def expand_related_terms(text: str) -> str:
    expanded = text
    if any(term_matches(text, term) for term in health_agency_terms()):
        expanded += " Medicaid Medicare CMS MMIS managed care eligibility claims provider health human services behavioral health"
    if any(term_matches(text, term) for term in ["Med-QUEST", "MQD"]):
        expanded += " Medicaid managed care eligibility claims provider enrollment"
    return expanded


def health_agency_terms() -> list[str]:
    return [
        "Department of Health",
        "Department of Human Services",
        "Med-QUEST",
        "Behavioral Health",
        "Developmental Disabilities",
        "Hawaii Health Systems",
        "Department of Corrections and Rehabilitation",
        "Office of Wellness and Resilience",
    ]


def useful_keyword_match(matches: list[str], text: str) -> bool:
    generic_terms = {"claims", "eligibility", "enrollment"}
    matched_terms = {match.lower() for match in matches if match}
    if not matched_terms or not matched_terms <= generic_terms:
        return True
    context_terms = [
        "medicaid",
        "medicare",
        "med-quest",
        "department of human services",
        "department of health",
        "health care",
        "healthcare",
        "provider",
        "managed care",
        "mmis",
    ]
    return any(term_matches(text, term) for term in context_terms)


def is_open_or_recent(posted_date: str, due_date: str, days_back: int) -> bool:
    today = dt.date.today()
    due = parse_date(due_date)
    if due and due >= today:
        return True
    posted = parse_date(posted_date)
    if posted and posted >= today - dt.timedelta(days=max(0, days_back)):
        return True
    return False


def relevance_score(matches: list[str], status: str, text: str, due_date: str) -> int:
    score = len([match for match in matches if match]) * 10
    high_value = ["Medicaid", "MMIS", "Med-QUEST", "managed care", "eligibility", "claims", "provider data", "CMS"]
    score += 20 * len(keyword_hits(text, high_value))
    if "open" in status.lower() or "posted" in status.lower() or "released" in status.lower():
        score += 5
    due = parse_date(due_date)
    if due and due >= dt.date.today():
        score += 3
    return min(score, 100)


def record_sort_key(row: dict[str, str]) -> tuple[int, str, str]:
    return (int_or_zero(row.get("relevance_score")), row.get("due_date", ""), row.get("posted_date", ""))


def valid_rows(value: Any) -> list[dict[str, Any]]:
    return [row for row in value or [] if isinstance(row, dict)]


def code_matches(text: str, code: str) -> bool:
    return re.search(rf"(?<![A-Z0-9]){re.escape(code)}(?![A-Z0-9])", text, re.IGNORECASE) is not None


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
