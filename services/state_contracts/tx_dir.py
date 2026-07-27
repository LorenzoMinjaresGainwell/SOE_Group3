from __future__ import annotations

import datetime as dt
import hashlib
import html as html_lib
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[2]
DIR_CONTRACTS_URL = "https://dir.texas.gov/contracts"
DIR_SEARCH_URL = "https://dir.texas.gov/search-contracts-vendors/contract/Active"
DIR_SOURCE_NAME = "Texas DIR Cooperative Contracts"
DIR_AGENCY = "Texas Department of Information Resources"
USER_AGENT = "soe-group3-tx-dir-contracts/0.1"
DEFAULT_CACHE_DIR = ROOT / "data" / "raw" / "state_contracts" / "tx_dir"
DEFAULT_THROTTLE_SECONDS = 8.0
DEFAULT_CACHE_TTL_HOURS = 168.0
DEFAULT_MAX_SEARCH_TERMS = 1
DEFAULT_MAX_RESULTS_PER_SEARCH = 3
CARD_MARKER = '<div class="card card--special">'
_LIVE_STATUSES = {"live_ok", "http_error", "live_error"}
_LAST_LIVE_CALL_EPOCH = 0.0


@dataclass(frozen=True)
class SearchCard:
    contract_number: str
    vendor_name: str
    overview: str
    detail_url: str


@dataclass(frozen=True)
class DIRCacheEntry:
    timestamp: str
    url: str
    cache_hit: bool
    status: str
    cache_key: str
    elapsed_ms: int = 0
    note: str = ""


class DIRDiskCache:
    """Small on-disk cache so DIR probing never loops live requests."""

    def __init__(self, root: Path = DEFAULT_CACHE_DIR) -> None:
        self.root = root
        self.requests_dir = root / "requests"
        self.ledger_path = root / "call_ledger.ndjson"

    def get(self, url: str, *, ttl_hours: float | None = None) -> dict[str, Any] | None:
        path = self.cache_path(cache_key(url))
        if not path.exists():
            return None
        with path.open(encoding="utf-8") as handle:
            payload = json.load(handle)
        if cache_expired(payload.get("stored_at"), ttl_hours=ttl_hours):
            return None
        return payload

    def put(self, url: str, *, status: int | str, body: str, headers: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = {
            "cache_key": cache_key(url),
            "stored_at": now_iso(),
            "url": url,
            "response_status": str(status),
            "response_headers": dict(headers or {}),
            "response_text": body,
        }
        self.requests_dir.mkdir(parents=True, exist_ok=True)
        path = self.cache_path(payload["cache_key"])
        tmp_path = path.with_suffix(".tmp")
        with tmp_path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=True, sort_keys=True)
            handle.write("\n")
        tmp_path.replace(path)
        return payload

    def log(self, url: str, *, cache_hit: bool, status: str, elapsed_ms: int = 0, note: str = "") -> None:
        entry = DIRCacheEntry(
            timestamp=now_iso(),
            url=url,
            cache_hit=cache_hit,
            status=status,
            cache_key=cache_key(url),
            elapsed_ms=elapsed_ms,
            note=note,
        )
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        with self.ledger_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(asdict(entry), ensure_ascii=True, sort_keys=True, separators=(",", ":")))
            handle.write("\n")

    def cache_path(self, key: str) -> Path:
        return self.requests_dir / f"{key}.json"


def fetch_contracts(
    *,
    vendor_terms: list[str],
    keywords: list[str],
    max_per_vendor: int,
    progress: Callable[[str], None] | None = None,
) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    detail_cache: dict[str, dict[str, Any]] = {}
    search_terms = selected_vendor_terms(vendor_terms)
    max_results = max(1, min(max_per_vendor, int_from_env("TX_DIR_MAX_RESULTS_PER_SEARCH", DEFAULT_MAX_RESULTS_PER_SEARCH)))

    for term in search_terms:
        cards = search_contract_cards(term, max_results=max_results)
        emit(progress, f"TX DIR: query={term}: {len(cards)} contract rows from first page")
        for card in cards:
            detail = detail_cache.get(card.detail_url)
            if detail is None:
                detail = fetch_contract_detail(card.detail_url, card=card)
                detail_cache[card.detail_url] = detail
            record = build_record(detail, vendor_query=term, keywords=keywords)
            if not record or record["id"] in seen_ids:
                continue
            seen_ids.add(record["id"])
            records.append(record)

    return sorted(records, key=contract_sort_key, reverse=True)


def selected_vendor_terms(vendor_terms: list[str]) -> list[str]:
    max_terms = max(0, int_from_env("TX_DIR_MAX_SEARCH_TERMS", DEFAULT_MAX_SEARCH_TERMS))
    if max_terms <= 0:
        return []
    unique = unique_terms(vendor_terms)
    preferred = sorted([term for term in unique if "gainwell" in term.lower()], key=len)
    remaining = [term for term in unique if term not in preferred]
    return (preferred + remaining)[:max_terms]


def search_contract_cards(query: str, *, max_results: int) -> list[SearchCard]:
    url = DIR_SEARCH_URL + "?" + urllib.parse.urlencode({"query": query, "sort_by": "search_api_relevance"})
    body = http_text(url, referer=DIR_CONTRACTS_URL)
    return parse_search_cards(body)[:max_results]


def fetch_contract_detail(detail_url: str, *, card: SearchCard) -> dict[str, Any]:
    url = absolute_dir_url(detail_url)
    body = http_text(url, referer=DIR_SEARCH_URL)
    return parse_contract_detail(body, url=url, card=card)


def http_text(url: str, *, referer: str, timeout: int = 45, cache: DIRDiskCache | None = None) -> str:
    active_cache = cache or DIRDiskCache()
    ttl_hours = float_from_env("TX_DIR_CACHE_TTL_HOURS", DEFAULT_CACHE_TTL_HOURS)
    cached = active_cache.get(url, ttl_hours=ttl_hours)
    if cached is not None:
        active_cache.log(url, cache_hit=True, status="cache_hit")
        return str(cached.get("response_text") or "")

    throttle_live_call(active_cache)
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Referer": referer,
    }
    request = urllib.request.Request(url, headers=headers)
    start = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", "replace")
            elapsed_ms = int((time.monotonic() - start) * 1000)
            active_cache.put(url, status=getattr(response, "status", response.getcode()), body=body, headers=dict(response.headers.items()))
            active_cache.log(url, cache_hit=False, status="live_ok", elapsed_ms=elapsed_ms)
            mark_live_call()
            return body
    except urllib.error.HTTPError as exc:
        body = exc.read(1000).decode("utf-8", "replace")
        elapsed_ms = int((time.monotonic() - start) * 1000)
        active_cache.log(url, cache_hit=False, status="http_error", elapsed_ms=elapsed_ms, note=f"HTTP {exc.code}: {body[:300]}")
        mark_live_call()
        raise RuntimeError(f"TX DIR HTTP {exc.code} from {url}: {body[:300]}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        active_cache.log(url, cache_hit=False, status="live_error", elapsed_ms=elapsed_ms, note=str(exc)[:300])
        mark_live_call()
        raise RuntimeError(f"TX DIR request failed for {url}: {exc}") from exc


def parse_search_cards(body: str) -> list[SearchCard]:
    cards: list[SearchCard] = []
    for chunk in body.split(CARD_MARKER)[1:]:
        link = re.search(r'<h3 class="card-heading">\s*<a\s+href="([^"]+)"[^>]*>(.*?)</a>', chunk, re.IGNORECASE | re.DOTALL)
        if not link:
            continue
        overview = first_match(
            r'views-field-contract-overview.*?<span[^>]*class="field-content card-content"[^>]*>(.*?)</span>',
            chunk,
        )
        vendor = first_match(r'<span class="card-subtitle">(.*?)</span>', chunk)
        contract_number = clean_text(strip_tags(link.group(2)), 80)
        detail_url = absolute_dir_url(html_lib.unescape(link.group(1)))
        if contract_number and detail_url:
            cards.append(
                SearchCard(
                    contract_number=contract_number,
                    vendor_name=clean_text(strip_tags(vendor), 180),
                    overview=clean_text(strip_tags(overview), 900),
                    detail_url=detail_url,
                )
            )
    return cards


def parse_contract_detail(body: str, *, url: str, card: SearchCard) -> dict[str, Any]:
    meta = meta_values(body)
    contract_number = first_meta(meta, "contract") or card.contract_number or title_from_page(body)
    vendor_name = first_meta(meta, "vendor") or vendor_from_page(body) or card.vendor_name
    overview = first_meta(meta, "contract_overview") or contract_overview_from_page(body) or card.overview
    product_services = meta.get("product_service_category", [])
    commodity_codes = meta.get("commodity_code", [])
    start_date = date_label(body, "Contract Start Date")
    term_date = date_label(body, "Contract Term Date")
    expiration_date = date_label(body, "Contract Expiration Date")
    return {
        "contract_number": clean_text(contract_number, 80),
        "vendor_name": clean_text(vendor_name, 180),
        "overview": clean_text(overview, 1500),
        "rfo_number": clean_text(first_meta(meta, "rfo_number"), 80),
        "status": clean_text(status_from_page(body) or first_meta(meta, "expired_contract"), 80),
        "vendor_id": clean_text(value_after_strong(body, "Vendor ID"), 80),
        "start_date": start_date,
        "term_date": term_date,
        "expiration_date": expiration_date,
        "product_services": [clean_text(item, 240) for item in product_services if clean_text(item, 240)],
        "commodity_codes": [clean_text(item, 240) for item in commodity_codes if clean_text(item, 240)],
        "brand": clean_text(first_meta(meta, "brand"), 120),
        "document_url": url,
    }


def build_record(detail: dict[str, Any], *, vendor_query: str, keywords: list[str]) -> dict[str, str]:
    contract_number = clean_text(detail.get("contract_number"), 80)
    vendor_name = clean_text(detail.get("vendor_name"), 180)
    overview = clean_text(detail.get("overview"), 500)
    start_date = iso_date(detail.get("start_date"))
    end_date = iso_date(detail.get("expiration_date") or detail.get("term_date"))
    document_url = clean_text(detail.get("document_url"), 300)
    categories = [str(item) for item in detail.get("product_services") or []]
    commodity_codes = [str(item) for item in detail.get("commodity_codes") or []]
    if not all([contract_number, vendor_name, overview, start_date, end_date, document_url]) or not (categories or commodity_codes):
        return {}

    match_text = " ".join([vendor_name, vendor_query, contract_number, overview, " ".join(categories), " ".join(commodity_codes)])
    matched = keyword_hits(match_text, keywords)
    months = months_until(end_date)
    recompete = recompete_signal(months)
    raw = {**detail, "selected_vendor_query": vendor_query}

    return {
        "id": f"tx-dir-{slug_id(contract_number)}",
        "state": "TX",
        "source": DIR_SOURCE_NAME,
        "source_record_id": contract_number,
        "parent_id": contract_number,
        "contract_record_type": "parent_contract",
        "vendor_name": vendor_name,
        "vendor_query": vendor_query,
        "agency": DIR_AGENCY,
        "contract_number": contract_number,
        "title": overview,
        "amount": "0",
        "execution_date": "",
        "start_date": start_date,
        "end_date": end_date,
        "months_to_end": "" if months is None else str(months),
        "recompete_signal": recompete,
        "document_type": "DIR Cooperative Contract",
        "document_url": document_url,
        "source_url": DIR_CONTRACTS_URL,
        "matched_keywords": ";".join(matched),
        "relevance_score": str(relevance_score(matched, recompete, overview, vendor_name, vendor_query)),
        "raw_json": compact_raw_json(raw),
        "last_checked_at": now_iso(),
    }


def meta_values(body: str) -> dict[str, list[str]]:
    values: dict[str, list[str]] = {}
    for tag in re.findall(r"<meta\s+[^>]*>", body, flags=re.IGNORECASE | re.DOTALL):
        attrs = tag_attrs(tag)
        name = attrs.get("name", "").strip().lower()
        content = attrs.get("content", "")
        if name:
            values.setdefault(name, []).append(clean_text(content, 3000))
    return values


def tag_attrs(tag: str) -> dict[str, str]:
    attrs: dict[str, str] = {}
    for match in re.finditer(r"([A-Za-z_:][-A-Za-z0-9_:.]*)\s*=\s*([\"'])(.*?)\2", tag, flags=re.DOTALL):
        attrs[match.group(1).lower()] = html_lib.unescape(match.group(3))
    return attrs


def first_meta(meta: dict[str, list[str]], name: str) -> str:
    values = meta.get(name.lower(), [])
    return values[0] if values else ""


def title_from_page(body: str) -> str:
    return clean_text(first_match(r'<h1[^>]*>\s*<span[^>]*>(.*?)</span>', body), 80)


def vendor_from_page(body: str) -> str:
    return clean_text(first_match(r'field--name-field-vendor-ref[^>]*>\s*<a[^>]*>(.*?)</a>', body), 180)


def status_from_page(body: str) -> str:
    return clean_text(value_after_strong(body, "Contract Status"), 80)


def contract_overview_from_page(body: str) -> str:
    section = first_match(r'<section class="contract-overview".*?<h2[^>]*>Contract Overview</h2>(.*?)</section>', body)
    return clean_text(strip_tags(section), 1500)


def date_label(body: str, label: str) -> str:
    return clean_text(first_match(rf'<strong>\s*{re.escape(label)}:\s*</strong>\s*<span>(.*?)</span>', body), 40)


def value_after_strong(body: str, label: str) -> str:
    return clean_text(first_match(rf'<strong>\s*{re.escape(label)}:\s*</strong>\s*<span>(.*?)</span>', body), 120)


def first_match(pattern: str, text: str) -> str:
    match = re.search(pattern, text, flags=re.IGNORECASE | re.DOTALL)
    return match.group(1) if match else ""


def strip_tags(value: Any) -> str:
    text = re.sub(r"<[^>]+>", " ", str(value or ""))
    return html_lib.unescape(text)


def absolute_dir_url(url: str) -> str:
    return urllib.parse.urljoin("https://dir.texas.gov/", str(url or ""))


def cache_key(url: str) -> str:
    return hashlib.sha256(json.dumps({"method": "GET", "url": url}, sort_keys=True).encode("utf-8")).hexdigest()


def cache_expired(stored_at: Any, *, ttl_hours: float | None) -> bool:
    ttl = DEFAULT_CACHE_TTL_HOURS if ttl_hours is None else ttl_hours
    if ttl <= 0:
        return False
    parsed = parse_datetime(stored_at)
    if not parsed:
        return True
    age = dt.datetime.now(dt.timezone.utc) - parsed
    return age.total_seconds() > ttl * 3600


def throttle_live_call(cache: DIRDiskCache) -> None:
    interval = max(0.0, float_from_env("TX_DIR_THROTTLE_SECONDS", DEFAULT_THROTTLE_SECONDS))
    if interval <= 0:
        return
    last_epoch = max(_LAST_LIVE_CALL_EPOCH, last_live_call_epoch(cache.ledger_path))
    if last_epoch <= 0:
        return
    wait = interval - (time.time() - last_epoch)
    if wait > 0:
        time.sleep(wait)


def mark_live_call() -> None:
    global _LAST_LIVE_CALL_EPOCH
    _LAST_LIVE_CALL_EPOCH = time.time()


def last_live_call_epoch(path: Path) -> float:
    if not path.exists():
        return 0.0
    latest = 0.0
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("cache_hit") or str(row.get("status") or "") not in _LIVE_STATUSES:
                continue
            parsed = parse_datetime(row.get("timestamp"))
            if parsed:
                latest = max(latest, parsed.timestamp())
    return latest


def contract_sort_key(row: dict[str, str]) -> tuple[int, str, str]:
    return (int_or_zero(row.get("relevance_score")), row.get("end_date", ""), row.get("title", ""))


def relevance_score(keywords: list[str], recompete: str, title: str, vendor_name: str, vendor_query: str) -> int:
    score = 30 if term_matches(vendor_name, vendor_query) else 18
    score += min(40, len(keywords) * 8)
    text = " ".join([title, vendor_name])
    if any(term_matches(text, term) for term in ["Medicaid", "MMIS"]):
        score += 25
    if any(term_matches(text, term) for term in ["information technology", "technology", "software", "data"]):
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
    text = clean_text(value, 80)
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return dt.datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    return None


def iso_date(value: Any) -> str:
    parsed = parse_date(value)
    return parsed.isoformat() if parsed else ""


def parse_datetime(value: Any) -> dt.datetime | None:
    if not value:
        return None
    text = str(value).replace("Z", "+00:00")
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


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


def term_matches(text: Any, term: str) -> bool:
    parts = [re.escape(part) for part in re.split(r"\s+", clean_text(term, 120)) if part]
    if not parts:
        return False
    pattern = r"(?<![A-Za-z0-9])" + r"\s+".join(parts) + r"(?![A-Za-z0-9])"
    return re.search(pattern, clean_text(text, 5000), re.IGNORECASE) is not None


def compact_raw_json(value: Any, *, limit: int = 5000) -> str:
    text = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), default=str)
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def slug_id(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(value or "").lower()).strip("-") or "contract"


def clean_text(value: Any, limit: int) -> str:
    text = re.sub(r"\s+", " ", strip_tags(value)).strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "..."


def int_from_env(name: str, default: int) -> int:
    try:
        return int(str(os.environ.get(name, default)).strip())
    except ValueError:
        return default


def float_from_env(name: str, default: float) -> float:
    try:
        return float(str(os.environ.get(name, default)).strip())
    except ValueError:
        return default


def int_or_zero(value: Any) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


def emit(progress: Callable[[str], None] | None, message: str) -> None:
    if progress:
        progress(message)
