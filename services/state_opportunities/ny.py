from __future__ import annotations

import datetime as dt
import re
import time
import urllib.parse
from typing import Any, Callable

from services.state_http import fetch_url
from services.state_normalization import clean_text, compact_raw_json, iso_date, keyword_hits, parse_date, stable_id, term_matches

BASE_URL = "https://www.nyscr.ny.gov/"
SEARCH_URL = urllib.parse.urljoin(BASE_URL, "Ads/Search")
PDF_URL = urllib.parse.urljoin(BASE_URL, "Ads/GenerateSearchPdf")
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
SOURCE_NOTE = "Official NYSCR public /Ads/Search listing. Per-row detail action requires login; public search PDF is captured when available."
PAGE_SIZE = 25
MAX_PAGES_PER_TERM = 6
TAG_RE = re.compile(r"(?is)<[^>]+>")
AD_ID_RE = re.compile(r"data-ad-id=[\"']([^\"']+)[\"']", re.IGNORECASE)
TITLE_RE = re.compile(r"title=[\"']Full Title:\s*([^\"']+)[\"']", re.IGNORECASE | re.DOTALL)
LABEL_RE = re.compile(
    r"(?is)<div[^>]*class=[\"'][^\"']*w-exact-8[^\"']*[\"'][^>]*>\s*(.*?)</div>\s*"
    r"<div[^>]*class=[\"'][^\"']*px-2[^\"']*[\"'][^>]*>\s*(.*?)</div>"
)
TOTAL_RE = re.compile(r"All\s+Open\s+Opportunities:</span>\s*([\d,]+)", re.IGNORECASE)


def fetch_opportunities(
    *,
    keywords: list[str],
    days_back: int,
    max_records: int,
    progress: Callable[[str], None] | None = None,
) -> list[dict[str, str]]:
    terms = prioritized_search_terms(keywords)
    candidates: dict[str, dict[str, Any]] = {}
    candidate_cap = max(80, max_records * 4)
    pages_seen = 0

    for term in terms:
        pages = fetch_search_pages(term=term, max_pages=MAX_PAGES_PER_TERM)
        pages_seen += len(pages)
        for page in pages:
            for row in parse_search_rows(page["html"], source_url=page["source_url"], pdf_url=page["pdf_url"], search_term=term):
                merge_candidate(candidates, row)
                if len(candidates) >= candidate_cap:
                    break
            if len(candidates) >= candidate_cap:
                break
        if len(candidates) >= candidate_cap:
            break

    if not candidates:
        pages = fetch_search_pages(term="", max_pages=max(2, min(MAX_PAGES_PER_TERM, (max_records // PAGE_SIZE) + 2)))
        pages_seen += len(pages)
        for page in pages:
            for row in parse_search_rows(page["html"], source_url=page["source_url"], pdf_url=page["pdf_url"], search_term=""):
                merge_candidate(candidates, row)

    emit(progress, f"NY NYSCR search: {len(candidates)} public rows across {pages_seen} pages")

    records: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in candidates.values():
        record = normalize_search_row(row, keywords=keywords)
        if not record.get("source_record_id") or record["id"] in seen:
            continue
        if record["status"].lower() == "cancelled":
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


def fetch_search_pages(*, term: str, max_pages: int) -> list[dict[str, str]]:
    pages: list[dict[str, str]] = []
    total: int | None = None
    for page_index in range(max(1, max_pages)):
        skip = page_index * PAGE_SIZE
        source_url = search_url(term=term, skip=skip)
        html, final_url = http_text(source_url)
        rows = count_rows(html)
        if rows == 0 and page_index > 0:
            break
        if total is None:
            total = parse_total(html)
        pages.append({"html": html, "source_url": final_url or source_url, "pdf_url": pdf_url(term=term)})
        if rows < PAGE_SIZE:
            break
        if total is not None and skip + rows >= total:
            break
    return pages


def parse_search_rows(html: str, *, source_url: str, pdf_url: str, search_term: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for chunk in re.split(r"(?=<div class=[\"']opp-list-item\b)", html):
        if "opp-list-item" not in chunk:
            continue
        ad_id = first_group(AD_ID_RE, chunk)
        fields = parse_label_fields(chunk)
        source_record_id = clean_text(fields.get("cr") or ad_id, 180)
        title = clean_text(first_group(TITLE_RE, chunk) or source_record_id, 500)
        if not source_record_id or not title:
            continue
        raw = {
            "source_key": "ny_contract_reporter",
            "source_note": SOURCE_NOTE,
            "ad_id": ad_id,
            "fields": fields,
            "search_terms": [search_term] if search_term else [],
            "detail_access": "login_required",
        }
        rows.append(
            {
                "source_record_id": source_record_id,
                "title": title,
                "agency": clean_text(fields.get("agency"), 180),
                "division": clean_text(fields.get("division"), 180),
                "posted_date": iso_date(fields.get("issue_date")),
                "due_date": iso_date(fields.get("due_date")),
                "location": clean_text(fields.get("location"), 240),
                "category": clean_text(fields.get("category"), 500),
                "ad_type": clean_text(fields.get("ad_type"), 120),
                "note": clean_text(fields.get("note"), 500),
                "source_url": source_url,
                "document_url": pdf_url,
                "search_terms": [search_term] if search_term else [],
                "raw": raw,
            }
        )
    return rows


def normalize_search_row(row: dict[str, Any], *, keywords: list[str]) -> dict[str, str]:
    source_record_id = clean_text(row.get("source_record_id"), 180)
    title = clean_text(row.get("title") or source_record_id, 500)
    agency = clean_text(row.get("agency"), 180)
    posted_date = clean_text(row.get("posted_date"), 20)
    due_date = clean_text(row.get("due_date"), 20)
    status = status_from_row(row)
    search_text = " ".join(
        [
            source_record_id,
            title,
            agency,
            clean_text(row.get("division"), 180),
            clean_text(row.get("category"), 500),
            clean_text(row.get("ad_type"), 120),
            clean_text(row.get("location"), 240),
            clean_text(row.get("note"), 500),
        ]
    )
    matched = merge_matches(keyword_hits(search_text, keywords), row.get("search_terms") or [], keywords)
    raw = dict(row.get("raw") or {})
    raw.update(
        {
            "normalized_status": status,
            "source_url": clean_text(row.get("source_url"), 500),
            "document_url": clean_text(row.get("document_url"), 500),
        }
    )

    return {
        "id": stable_id("NY", source_record_id, prefix="ny-nyscr-ad"),
        "state": "NY",
        "source": "New York State Contract Reporter Open Opportunities",
        "source_record_id": source_record_id,
        "title": title,
        "agency": agency,
        "document_type": document_type(clean_text(row.get("ad_type"), 120), title, clean_text(row.get("category"), 500)),
        "posted_date": posted_date,
        "due_date": due_date,
        "status": status,
        "amount": "",
        "document_url": clean_text(row.get("document_url"), 500),
        "source_url": clean_text(row.get("source_url") or SEARCH_URL, 500),
        "matched_keywords": ";".join(matched),
        "relevance_score": str(relevance_score(matched, status, search_text, due_date)),
        "raw_json": compact_raw_json(raw),
        "last_checked_at": now_iso(),
    }


def parse_label_fields(block: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for match in LABEL_RE.finditer(block):
        key = normalize_label(strip_html(match.group(1), 120))
        value = strip_html(match.group(2), 1000)
        if key and value and key not in fields:
            fields[key] = value
    return fields


def normalize_label(value: str) -> str:
    text = clean_text(value, 120).strip(":").lower()
    text = text.replace("#", "")
    return re.sub(r"[^a-z0-9]+", "_", text).strip("_")


def first_group(pattern: re.Pattern[str], text: str) -> str:
    match = pattern.search(text)
    return clean_text(match.group(1), 1000) if match else ""


def strip_html(value: str, limit: int) -> str:
    return clean_text(TAG_RE.sub(" ", value), limit)


def parse_total(html: str) -> int | None:
    match = TOTAL_RE.search(html)
    if not match:
        return None
    try:
        return int(match.group(1).replace(",", ""))
    except ValueError:
        return None


def count_rows(html: str) -> int:
    return len(re.findall(r"class=[\"']opp-list-item\b", html, re.IGNORECASE))


def merge_candidate(candidates: dict[str, dict[str, Any]], row: dict[str, Any]) -> None:
    key = clean_text(row.get("source_record_id"), 180)
    if not key:
        return
    existing = candidates.get(key)
    if existing is None:
        candidates[key] = row
        return
    terms = list(existing.get("search_terms") or [])
    for term in row.get("search_terms") or []:
        if term and term not in terms:
            terms.append(term)
    existing["search_terms"] = terms
    raw = dict(existing.get("raw") or {})
    raw["search_terms"] = terms
    existing["raw"] = raw


def search_url(*, term: str, skip: int) -> str:
    params: dict[str, str] = {"Status": "Open", "DateFilter": "All"}
    if term:
        params["Keyword"] = term
    if skip > 0:
        params["Skip"] = str(skip)
    return SEARCH_URL + "?" + urllib.parse.urlencode(params)


def pdf_url(*, term: str) -> str:
    params: dict[str, str] = {"Status": "Open", "DateFilter": "All"}
    if term:
        params["Keyword"] = term
    return PDF_URL + "?" + urllib.parse.urlencode(params)


def http_text(url: str) -> tuple[str, str]:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": BASE_URL,
    }
    last_error = ""
    for attempt in range(3):
        result = fetch_url(url, headers=headers, timeout=60, byte_limit=1_500_000, user_agent=USER_AGENT)
        if result.ok:
            return result.body_text(), result.final_url
        last_error = result.error or f"HTTP {result.status_code}"
        time.sleep(1 + attempt)
    raise RuntimeError(f"NY NYSCR request failed for {url}: {last_error}")


def prioritized_search_terms(keywords: list[str]) -> list[str]:
    terms = dedupe([clean_text(keyword, 80) for keyword in keywords if clean_text(keyword, 80)])
    priority = [
        "medicaid",
        "mmis",
        "managed care",
        "provider data",
        "eligibility",
        "claims",
        "enrollment",
        "interoperability",
        "prior authorization",
        "fhir",
        "cms",
        "medicare",
        "quality measures",
        "behavioral health",
        "rural health",
        "telehealth",
    ]

    def key(term: str) -> tuple[int, int, str]:
        lower = term.lower()
        if lower in priority:
            return (0, priority.index(lower), lower)
        if " " in lower:
            return (1, 0, lower)
        return (2, 0, lower)

    return sorted(terms, key=key)


def merge_matches(direct_hits: list[str], search_terms: list[str], keywords: list[str]) -> list[str]:
    matches = list(direct_hits)
    keyword_lookup = {keyword.lower(): keyword for keyword in keywords}
    for term in search_terms:
        canonical = keyword_lookup.get(clean_text(term).lower(), clean_text(term))
        if canonical and canonical not in matches:
            matches.append(canonical)
    return sorted(matches, key=str.lower)


def document_type(ad_type: str, title: str, category: str) -> str:
    text = " ".join([ad_type, title, category]).upper()
    if code_matches(text, "RFI") or "REQUEST FOR INFORMATION" in text:
        return "NYSCR Request for Information"
    if code_matches(text, "RFP") or "REQUEST FOR PROPOSAL" in text:
        return "NYSCR Request for Proposal"
    if code_matches(text, "RFQ") or "REQUEST FOR QUOTE" in text:
        return "NYSCR Request for Quote"
    if code_matches(text, "IFB") or "INVITATION FOR BID" in text:
        return "NYSCR Invitation for Bid"
    return f"NYSCR {ad_type} Opportunity" if ad_type else "NYSCR Contracting Opportunity"


def status_from_row(row: dict[str, Any]) -> str:
    text = " ".join([clean_text(row.get("title")), clean_text(row.get("note"))]).lower()
    if "cancelled" in text or "canceled" in text:
        return "Cancelled"
    due = parse_date(row.get("due_date"))
    if due and due < dt.date.today():
        return "Closed"
    return "Open"


def useful_keyword_match(matches: list[str], text: str) -> bool:
    generic_terms = {"claims", "eligibility", "enrollment", "workforce"}
    matched_terms = {match.lower() for match in matches if match}
    if not matched_terms or not matched_terms <= generic_terms:
        return True
    context_terms = [
        "health",
        "healthcare",
        "health care",
        "medicaid",
        "medicare",
        "medical",
        "hospital",
        "behavioral",
        "managed care",
        "provider",
        "chip",
        "mmis",
        "department of health",
        "office of mental health",
        "office for people with developmental disabilities",
    ]
    return any(term_matches(text, term) for term in context_terms)


def relevance_score(matches: list[str], status: str, text: str, due_date: str) -> int:
    score = min(50, len([match for match in matches if match]) * 10)
    if any(term_matches(text, term) for term in ["Medicaid", "MMIS", "Department of Health", "Office of Mental Health", "Health Care", "Healthcare"]):
        score += 25
    if any(term_matches(text, term) for term in ["eligibility", "claims", "enrollment", "managed care", "interoperability", "FHIR", "prior authorization", "provider data"]):
        score += 15
    if term_matches(text, "rural health"):
        score += 25
    if any(term_matches(text, term) for term in ["RFP", "RFI", "RFQ", "software", "data", "cloud", "platform", "services"]):
        score += 12
    if status.lower() == "open":
        score += 10
    due = parse_date(due_date)
    if due and due >= dt.date.today():
        score += 8
    return min(score, 100)


def is_open_or_recent(posted_date: str, due_date: str, days_back: int) -> bool:
    due = parse_date(due_date)
    if due and due >= dt.date.today():
        return True
    if days_back <= 0:
        return True
    posted = parse_date(posted_date)
    return not posted or (dt.date.today() - posted).days <= days_back


def record_sort_key(row: dict[str, str]) -> tuple[int, str, str]:
    return (int_or_zero(row.get("relevance_score")), row.get("due_date", ""), row.get("posted_date", ""))


def code_matches(text: str, code: str) -> bool:
    return re.search(rf"(?<![A-Z0-9]){re.escape(code)}(?![A-Z0-9])", text, re.IGNORECASE) is not None


def dedupe(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


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
