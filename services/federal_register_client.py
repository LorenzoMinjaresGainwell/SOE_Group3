from __future__ import annotations

import csv
import datetime as dt
import html
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from services.search_taxonomy import load_search_taxonomy, matching_terms

FEDERAL_REGISTER_DOCUMENTS_URL = "https://www.federalregister.gov/api/v1/documents.json"
USER_AGENT = "soe-group3-federal-register/0.1"
SOURCE_NAME = "Federal Register"

FEDERAL_REGISTER_FIELDS = [
    "title",
    "type",
    "abstract",
    "document_number",
    "html_url",
    "pdf_url",
    "publication_date",
    "agencies",
    "docket_ids",
    "comments_close_on",
    "regulations_dot_gov_url",
    "action",
    "dates",
    "citation",
    "significant",
]

FEDERAL_REGISTER_CSV_FIELDS = [
    "id",
    "source",
    "source_record_id",
    "title",
    "document_type",
    "agency",
    "publication_date",
    "comment_close_date",
    "docket_ids",
    "abstract",
    "url",
    "matched_keywords",
    "relevance_score",
    "last_checked_at",
    "raw_json",
]

# Compatibility export; agency slugs and API field vocabulary remain source-specific below.
DEFAULT_KEYWORDS = load_search_taxonomy().business_terms

DEFAULT_AGENCY_SLUGS = [
    "centers-for-medicare-medicaid-services",
    "health-and-human-services-department",
]

KEYWORD_WEIGHTS = {
    "medicaid": 18,
    "medicare": 16,
    "cms": 14,
    "rural health transformation": 20,
    "rural health": 16,
    "interoperability": 14,
    "prior authorization": 16,
    "managed care": 12,
    "waiver": 10,
    "1115": 14,
    "eligibility": 10,
    "enrollment": 10,
    "quality measures": 10,
}

RELATED_API_NOTES = [
    {
        "source": "Federal Register Documents API",
        "url": FEDERAL_REGISTER_DOCUMENTS_URL,
        "api_key_required": "No",
        "useful_fields": "document_number, title, type, agencies, publication_date, comments_close_on, docket_ids, regulations_dot_gov_url, abstract",
        "notes": "Use agency slugs plus conditions[term] and publication_date filters for CMS/HHS policy monitoring.",
    },
    {
        "source": "Federal Register Agencies API",
        "url": "https://www.federalregister.gov/api/v1/agencies",
        "api_key_required": "No",
        "useful_fields": "id, slug, name, parent_id, child_slugs, recent_articles_url",
        "notes": "Useful for validating HHS/CMS agency slugs before document searches.",
    },
    {
        "source": "Regulations.gov API v4",
        "url": "https://api.regulations.gov/v4/documents",
        "api_key_required": "Yes",
        "useful_fields": "filter[docketId], filter[agencyId], filter[documentType], filter[commentEndDate], include=attachments",
        "notes": "Federal Register docket_ids can be used as Regulations.gov docket filters. Implement later when an api.data.gov key is available.",
    },
]


@dataclass
class FederalRegisterConfig:
    keywords: list[str]
    start_date: dt.date
    end_date: dt.date
    max_records: int = 100
    agency_slugs: list[str] | None = None


def fetch_federal_register_updates(
    config: FederalRegisterConfig,
    progress: Callable[[str], None] | None = None,
) -> list[dict[str, str]]:
    rows_by_key: dict[str, dict[str, str]] = {}
    agency_slugs = config.agency_slugs or DEFAULT_AGENCY_SLUGS

    for keyword in config.keywords:
        if not keyword.strip():
            continue
        for agency_slug in agency_slugs:
            emit(progress, f"Federal Register: {agency_slug} term={keyword}")
            page = 1
            while True:
                data = http_json(build_documents_url(config, keyword, agency_slug, page))
                results = data.get("results") or []
                if not results:
                    break

                for item in results:
                    row = normalize_document(item, config.keywords)
                    key = row.get("source_record_id", "")
                    if not key:
                        continue
                    old = rows_by_key.get(key)
                    if old is None or int(row["relevance_score"]) > int(old["relevance_score"]):
                        rows_by_key[key] = row

                if not data.get("next_page_url") or page >= int(data.get("total_pages") or page):
                    break
                page += 1

    rows = sorted(
        rows_by_key.values(),
        key=lambda row: (int_or_zero(row.get("relevance_score")), row.get("publication_date", "")),
        reverse=True,
    )
    return rows[: config.max_records]


def build_documents_url(config: FederalRegisterConfig, keyword: str, agency_slug: str, page: int) -> str:
    per_page = min(max(config.max_records, 1), 100)
    params: list[tuple[str, Any]] = [
        ("conditions[term]", keyword),
        ("conditions[agencies][]", agency_slug),
        ("conditions[publication_date][gte]", config.start_date.isoformat()),
        ("conditions[publication_date][lte]", config.end_date.isoformat()),
        ("order", "newest"),
        ("page", page),
        ("per_page", per_page),
    ]
    params.extend(("fields[]", field) for field in FEDERAL_REGISTER_FIELDS)
    return FEDERAL_REGISTER_DOCUMENTS_URL + "?" + urllib.parse.urlencode(params)


def normalize_document(item: dict[str, Any], keywords: list[str]) -> dict[str, str]:
    source_record_id = str(item.get("document_number") or item.get("html_url") or "")
    agencies = agency_names(item.get("agencies"))
    abstract = clean_text(item.get("abstract") or item.get("excerpts") or item.get("action") or "", 1200)
    title = clean_text(item.get("title") or "Untitled Federal Register document", 400)
    document_type = clean_text(item.get("type") or "Document", 80)
    text_for_match = " ".join(
        [
            title,
            document_type,
            agencies,
            abstract,
            clean_text(item.get("action") or "", 500),
            clean_text(item.get("dates") or "", 500),
            ";".join(str(value) for value in item.get("docket_ids") or []),
        ]
    )
    matched = keyword_hits(text_for_match, keywords)
    docket_ids = ";".join(str(value) for value in item.get("docket_ids") or [] if str(value).strip())

    return {
        "id": stable_id(source_record_id),
        "source": SOURCE_NAME,
        "source_record_id": source_record_id,
        "title": title,
        "document_type": document_type,
        "agency": clean_text(agencies, 240),
        "publication_date": iso_date(item.get("publication_date")),
        "comment_close_date": iso_date(item.get("comments_close_on")),
        "docket_ids": docket_ids,
        "abstract": abstract,
        "url": str(item.get("html_url") or item.get("pdf_url") or ""),
        "matched_keywords": ";".join(matched),
        "relevance_score": str(relevance_score(matched, document_type, agencies, docket_ids, item.get("comments_close_on"))),
        "last_checked_at": now_iso(),
        "raw_json": json.dumps(item, ensure_ascii=True, sort_keys=True, separators=(",", ":")),
    }


def upsert_federal_register_updates(path: Path, new_rows: list[dict[str, str]]) -> tuple[int, int, int]:
    existing_rows = read_csv(path)
    by_record_id = {row.get("source_record_id", ""): row for row in existing_rows if row.get("source_record_id")}
    added = 0
    updated = 0

    for new_row in new_rows:
        key = new_row.get("source_record_id", "")
        if not key:
            continue
        old_row = by_record_id.get(key)
        if old_row is None:
            by_record_id[key] = {field: new_row.get(field, "") for field in FEDERAL_REGISTER_CSV_FIELDS}
            added += 1
            continue

        merged = dict(old_row)
        changed = False
        for field in FEDERAL_REGISTER_CSV_FIELDS:
            value = new_row.get(field, "")
            if merged.get(field, "") != value:
                merged[field] = value
                changed = True
        if changed:
            updated += 1
        by_record_id[key] = merged

    rows = sorted(
        by_record_id.values(),
        key=lambda row: (int_or_zero(row.get("relevance_score")), row.get("publication_date", "")),
        reverse=True,
    )
    write_csv(path, FEDERAL_REGISTER_CSV_FIELDS, rows)
    return added, updated, len(rows)


def http_json(url: str, timeout: int = 45) -> Any:
    request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": USER_AGENT})
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read(800).decode("utf-8", "replace")
            raise RuntimeError(f"HTTP {exc.code} from {sanitize_url(url)}: {body}") from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = exc
            time.sleep(1 + attempt)
    raise RuntimeError(f"request failed: {last_error}")


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def date_window(days_back: int, start_date: dt.date | None = None, end_date: dt.date | None = None) -> tuple[dt.date, dt.date]:
    end = end_date or dt.date.today()
    start = start_date or (end - dt.timedelta(days=days_back))
    if start > end:
        raise ValueError("start-date must be on or before end-date")
    return start, end


def agency_names(agencies: Any) -> str:
    names: list[str] = []
    for agency in agencies or []:
        if not isinstance(agency, dict):
            continue
        name = str(agency.get("name") or agency.get("raw_name") or "").strip()
        if name and name not in names:
            names.append(name)
    return "; ".join(names)


def keyword_hits(text: str, keywords: list[str]) -> list[str]:
    return matching_terms(text, keywords)


def relevance_score(keywords: list[str], document_type: str, agency: str, docket_ids: str, comment_close_date: Any) -> int:
    score = 5
    lower_type = document_type.lower()
    lower_agency = agency.lower()

    for keyword in keywords:
        score += KEYWORD_WEIGHTS.get(keyword.lower(), 4)
    if "centers for medicare" in lower_agency:
        score += 12
    elif "health and human services" in lower_agency:
        score += 6
    if any(term in lower_type for term in ("proposed rule", "rule", "notice")):
        score += 8
    if docket_ids:
        score += 6
    close_date = parse_date(comment_close_date)
    if close_date:
        if close_date >= dt.date.today():
            score += 12
        else:
            score += 3
    return max(0, min(score, 100))


def clean_text(value: Any, limit: int) -> str:
    text = html.unescape(re.sub(r"<[^>]+>", " ", str(value or "")))
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "..."


def parse_date(value: Any) -> dt.date | None:
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z"):
        try:
            return dt.datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    try:
        return dt.datetime.fromisoformat(text).date()
    except ValueError:
        return None


def iso_date(value: Any) -> str:
    parsed = parse_date(value)
    return parsed.isoformat() if parsed else ""


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def stable_id(source_record_id: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_-]+", "-", source_record_id.strip())[:120].strip("-")
    return f"federal-register-{cleaned or 'record'}"


def int_or_zero(value: Any) -> int:
    try:
        return int(float(value or 0))
    except ValueError:
        return 0


def sanitize_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    redacted = [(key, "REDACTED") if key.lower() in {"api_key", "apikey"} else (key, value) for key, value in query]
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urllib.parse.urlencode(redacted), parsed.fragment))


def emit(progress: Callable[[str], None] | None, message: str) -> None:
    if progress:
        progress(message)
