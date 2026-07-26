from __future__ import annotations

import datetime as dt
import html as html_lib
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from http.cookiejar import CookieJar
from typing import Any, Callable

from services.state_normalization import clean_text, compact_raw_json, iso_date, keyword_hits, parse_date, stable_id, term_matches

BASE_URL = "https://evp.nc.gov/"
SOLICITATIONS_URL = urllib.parse.urljoin(BASE_URL, "solicitations/")
TOKEN_URL = urllib.parse.urljoin(BASE_URL, "_layout/tokenhtml")
DETAIL_URL = urllib.parse.urljoin(BASE_URL, "solicitations/details/")
USER_AGENT = "soe-group3-nc-evp-opportunities/0.1"
SOURCE_NOTE = (
    "Official NC eVP Microsoft Power Pages public entity-grid endpoint: solicitations page exposes "
    "data-get-url=/_services/entity-grid-data.json/... and secure view config; token endpoint is public."
)
OPEN_PENDING_META_FILTER = "3=0&3=1"
PAGE_SIZE = 10
GRID_TAG_RE = re.compile(r'<div\b(?=[^>]*\bentity-grid\b)(?=[^>]*\bdata-get-url=)[^>]*>', re.IGNORECASE)
ATTR_RE = re.compile(r"([\w:-]+)\s*=\s*(?:\"([^\"]*)\"|'([^']*)')")


@dataclass(frozen=True)
class EntityGridConfig:
    get_url: str
    base64_config: str
    sort_expression: str
    entity_name: str
    entity_id: str


class NcEvpClient:
    def __init__(self) -> None:
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))
        self.config: EntityGridConfig | None = None
        self.token = ""

    def search_rows(self, *, search: str | None, max_pages: int = 1, page_size: int = PAGE_SIZE) -> list[dict[str, Any]]:
        config = self.load_config()
        token = self.request_token()
        rows: list[dict[str, Any]] = []
        secure_config = config.base64_config
        paging_cookie = ""
        for page in range(1, max(1, max_pages) + 1):
            payload = {
                "base64SecureConfiguration": secure_config,
                "sortExpression": config.sort_expression,
                "search": search or None,
                "page": page,
                "pageSize": page_size,
                "pagingCookie": paging_cookie,
                "filter": None,
                "metaFilter": OPEN_PENDING_META_FILTER,
                "nlSearchFilter": None,
                "timezoneOffset": 0,
                "customParameters": [],
                "entityName": None,
                "entityId": None,
            }
            data = self.post_json(config.get_url, payload, token=token)
            rows.extend(valid_records(data.get("Records")))
            secure_config = clean_text(data.get("ViewConfiguration")) or secure_config
            paging_cookie = clean_text(data.get("NextPagePagingCookie"))
            if not data.get("MoreRecords") or not paging_cookie:
                break
            time.sleep(0.15)
        return rows

    def load_config(self) -> EntityGridConfig:
        if self.config is not None:
            return self.config
        page = self.request_text(SOLICITATIONS_URL, accept="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8")
        tag_match = GRID_TAG_RE.search(page)
        if not tag_match:
            raise RuntimeError("NC eVP solicitations entity-grid config not found")
        attrs = parse_attrs(tag_match.group(0))
        layouts = decode_layouts(attrs.get("data-view-layouts", ""))
        selected_view = attrs.get("data-selected-view", "")
        layout = select_layout(layouts, selected_view)
        configuration = layout.get("Configuration") or {}
        get_url = urllib.parse.urljoin(SOLICITATIONS_URL, attrs.get("data-get-url", ""))
        if not get_url or not layout.get("Base64SecureConfiguration"):
            raise RuntimeError("NC eVP solicitations grid is missing endpoint or secure config")
        self.config = EntityGridConfig(
            get_url=get_url,
            base64_config=clean_text(layout.get("Base64SecureConfiguration")),
            sort_expression=clean_text(layout.get("SortExpression") or "evp_posteddate DESC"),
            entity_name=clean_text(configuration.get("EntityName") or "evp_solicitation"),
            entity_id=clean_text(configuration.get("ViewId") or selected_view),
        )
        return self.config

    def request_token(self) -> str:
        if self.token:
            return self.token
        token_html = self.request_text(TOKEN_URL, accept="text/html, */*; q=0.01", extra_headers={"X-Requested-With": "XMLHttpRequest"})
        for input_tag in re.findall(r"<input\b[^>]*>", token_html, re.IGNORECASE):
            attrs = parse_attrs(input_tag)
            if attrs.get("name") == "__RequestVerificationToken" and attrs.get("value"):
                self.token = attrs["value"]
                return self.token
        raise RuntimeError("NC eVP anti-forgery token not found")

    def post_json(self, url: str, payload: dict[str, Any], *, token: str) -> dict[str, Any]:
        body = json.dumps(payload).encode("utf-8")
        headers = {
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Content-Type": "application/json; charset=utf-8",
            "Origin": BASE_URL.rstrip("/"),
            "Referer": SOLICITATIONS_URL,
            "X-Requested-With": "XMLHttpRequest",
            "__RequestVerificationToken": token,
        }
        text = self.request_text(url, data=body, accept=headers.pop("Accept"), extra_headers=headers)
        data = json.loads(text)
        if not isinstance(data, dict):
            raise RuntimeError("NC eVP grid endpoint returned non-object JSON")
        return data

    def request_text(
        self,
        url: str,
        *,
        data: bytes | None = None,
        accept: str = "*/*",
        extra_headers: dict[str, str] | None = None,
        timeout: int = 60,
    ) -> str:
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": accept,
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": SOLICITATIONS_URL,
        }
        headers.update(extra_headers or {})
        request = urllib.request.Request(url, data=data, headers=headers)
        try:
            with self.opener.open(request, timeout=timeout) as response:
                return response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            body = exc.read(800).decode("utf-8", "replace")
            raise RuntimeError(f"HTTP {exc.code} from NC eVP {url}: {body}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise RuntimeError(f"NC eVP request failed for {url}: {exc}") from exc


def fetch_opportunities(
    *,
    keywords: list[str],
    days_back: int,
    max_records: int,
    progress: Callable[[str], None] | None = None,
) -> list[dict[str, str]]:
    client = NcEvpClient()
    rows = fetch_candidate_rows(client, keywords=keywords, max_records=max_records, progress=progress)
    emit(progress, f"NC eVP public solicitation rows: {len(rows)} unique rows")

    records: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in rows:
        record = normalize_solicitation_row(row, keywords=keywords)
        if not record.get("source_record_id") or record["id"] in seen:
            continue
        if record["status"].lower() in {"canceled", "cancelled"}:
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


def fetch_candidate_rows(
    client: NcEvpClient,
    *,
    keywords: list[str],
    max_records: int,
    progress: Callable[[str], None] | None = None,
) -> list[dict[str, Any]]:
    rows_by_id: dict[str, dict[str, Any]] = {}
    terms = dedupe([clean_text(term, 80) for term in keywords])
    if terms:
        for term in terms:
            batch = client.search_rows(search=term, max_pages=1, page_size=PAGE_SIZE)
            if batch:
                emit(progress, f"NC eVP search {term!r}: {len(batch)} rows")
            for row in batch:
                row_id = clean_text(row.get("Id"), 80)
                if row_id:
                    rows_by_id.setdefault(row_id, row)
            if len(rows_by_id) >= max(25, max_records * 3):
                break
            time.sleep(0.15)
    else:
        pages = max(1, min(5, (max(1, max_records) + PAGE_SIZE - 1) // PAGE_SIZE))
        for row in client.search_rows(search=None, max_pages=pages, page_size=PAGE_SIZE):
            row_id = clean_text(row.get("Id"), 80)
            if row_id:
                rows_by_id.setdefault(row_id, row)
    return list(rows_by_id.values())


def normalize_solicitation_row(row: dict[str, Any], *, keywords: list[str]) -> dict[str, str]:
    attrs = record_attributes(row)
    record_guid = clean_text(row.get("Id"), 80)
    source_record_id = attr_display(attrs, "evp_solicitationnbr") or record_guid
    title = attr_display(attrs, "evp_name") or source_record_id
    description = attr_display(attrs, "evp_description")
    agency = attr_display(attrs, "owningbusinessunit")
    status = attr_display(attrs, "statuscode") or clean_text(row.get("StatusCode"), 80)
    posted_date = iso_date(attr_value(attrs, "evp_posteddate") or attr_display(attrs, "evp_posteddate"))
    due_date = iso_date(attr_value(attrs, "evp_opendate") or attr_display(attrs, "evp_opendate"))
    detail_url = detail_page_url(record_guid)
    search_text = expand_related_terms(" ".join([source_record_id, title, description, agency, status]))
    matched = keyword_hits(search_text, keywords)
    raw = {
        "source_key": "nc_evp",
        "source_note": SOURCE_NOTE,
        "record_id": record_guid,
        "entity_name": clean_text(row.get("EntityName"), 80),
        "detail_url": detail_url,
        "attributes": raw_attribute_subset(attrs),
    }

    return {
        "id": stable_id("NC", record_guid or source_record_id, prefix="nc-evp-solicitation"),
        "state": "NC",
        "source": "North Carolina eVP Public Solicitations",
        "source_record_id": source_record_id,
        "title": clean_text(title, 500),
        "agency": clean_text(agency, 180),
        "document_type": document_type(source_record_id, title, description),
        "posted_date": posted_date,
        "due_date": due_date,
        "status": clean_text(status, 80),
        "amount": "",
        "document_url": detail_url,
        "source_url": SOLICITATIONS_URL,
        "matched_keywords": ";".join(matched),
        "relevance_score": str(relevance_score(matched, status, search_text, due_date)),
        "raw_json": compact_raw_json(raw),
        "last_checked_at": now_iso(),
    }


def parse_attrs(tag: str) -> dict[str, str]:
    return {name: html_lib.unescape(double or single or "") for name, double, single in ATTR_RE.findall(tag)}


def decode_layouts(value: str) -> list[dict[str, Any]]:
    if not value:
        return []
    decoded = html_lib.unescape(value)
    decoded = urllib.parse.unquote(decoded)
    try:
        layouts = json.loads(base64_decode(decoded))
    except (ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("NC eVP entity-grid layout config could not be decoded") from exc
    return [layout for layout in layouts if isinstance(layout, dict)] if isinstance(layouts, list) else []


def base64_decode(value: str) -> str:
    import base64

    return base64.b64decode(value).decode("utf-8")


def select_layout(layouts: list[dict[str, Any]], selected_view: str) -> dict[str, Any]:
    for layout in layouts:
        configuration = layout.get("Configuration") or {}
        if selected_view and selected_view in {clean_text(layout.get("Id")), clean_text(configuration.get("ViewId"))}:
            return layout
    if layouts:
        return layouts[0]
    raise RuntimeError("NC eVP entity-grid layout list is empty")


def valid_records(value: Any) -> list[dict[str, Any]]:
    return [row for row in value or [] if isinstance(row, dict)]


def record_attributes(row: dict[str, Any]) -> dict[str, dict[str, Any]]:
    attrs: dict[str, dict[str, Any]] = {}
    for item in row.get("Attributes") or []:
        if isinstance(item, dict) and item.get("Name"):
            attrs[clean_text(item.get("Name"))] = item
    return attrs


def attr_display(attrs: dict[str, dict[str, Any]], name: str) -> str:
    return clean_text((attrs.get(name) or {}).get("DisplayValue"), 1000)


def attr_value(attrs: dict[str, dict[str, Any]], name: str) -> Any:
    value = (attrs.get(name) or {}).get("Value")
    if isinstance(value, dict):
        return value.get("Name") or value.get("Value") or value.get("Id") or ""
    return value


def raw_attribute_subset(attrs: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    keys = [
        "evp_solicitationid",
        "evp_solicitationnbr",
        "evp_name",
        "evp_description",
        "owningbusinessunit",
        "statuscode",
        "evp_posteddate",
        "evp_opendate",
    ]
    subset: dict[str, dict[str, Any]] = {}
    for key in keys:
        item = attrs.get(key)
        if not item:
            continue
        subset[key] = {
            "display": clean_text(item.get("DisplayValue"), 1500),
            "value": compact_value(item.get("Value")),
        }
    return subset


def compact_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: value.get(key) for key in ("Id", "LogicalName", "Name", "Value") if value.get(key) not in (None, "")}
    return value


def detail_page_url(record_guid: str) -> str:
    if not record_guid:
        return SOLICITATIONS_URL
    return DETAIL_URL + "?" + urllib.parse.urlencode({"id": record_guid})


def document_type(source_record_id: str, title: str, description: str) -> str:
    text = " ".join([source_record_id, title, description]).upper()
    if code_matches(text, "RFI"):
        return "NC eVP Request for Information"
    if code_matches(text, "RFP"):
        return "NC eVP Request for Proposal"
    if code_matches(text, "RFQ"):
        return "NC eVP Request for Quote"
    if code_matches(text, "IFB"):
        return "NC eVP Invitation for Bid"
    if "SOURCING EVENT" in text:
        return "NC eVP Sourcing Event"
    return "NC eVP Solicitation"


def expand_related_terms(text: str) -> str:
    expanded = text
    upper = text.upper()
    if "DIVISION OF HEALTH BENEFITS" in upper or code_matches(upper, "DHB"):
        expanded += " Medicaid managed care"
    if "NC FAST" in upper:
        expanded += " Medicaid eligibility enrollment"
    return expanded


def useful_keyword_match(matches: list[str], text: str) -> bool:
    generic_terms = {"claims", "eligibility", "enrollment"}
    matched_terms = {match.lower() for match in matches if match}
    if not matched_terms or not matched_terms <= generic_terms:
        return True
    context_terms = [
        "department of health and human services",
        "division of health benefits",
        "healthcare",
        "health care",
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
        "dhhs",
        "dhb",
        "nc fast",
    ]
    return any(term_matches(text, term) for term in context_terms)


def relevance_score(matches: list[str], status: str, text: str, due_date: str) -> int:
    score = min(50, len([match for match in matches if match]) * 10)
    if any(term_matches(text, term) for term in ["Medicaid", "MMIS", "Department of Health and Human Services", "Division of Health Benefits", "NC FAST", "DHHS", "DHB"]):
        score += 25
    if any(term_matches(text, term) for term in ["eligibility", "claims", "enrollment", "managed care", "interoperability", "FHIR", "prior authorization", "provider data"]):
        score += 15
    if term_matches(text, "rural health"):
        score += 25
    if any(term_matches(text, term) for term in ["RFP", "RFI", "RFQ", "IFB", "software", "data", "cloud", "platform", "services"]):
        score += 12
    if status.lower() in {"open", "pending selection", "posted", "upcoming"}:
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
        if not value or value in seen:
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
