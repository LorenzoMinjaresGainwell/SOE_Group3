from __future__ import annotations

import csv
import datetime as dt
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

REGULATIONS_BASE_URL = "https://api.regulations.gov/v4"
USER_AGENT = "soe-group3-regulations/0.1"
SOURCE_NAME = "Regulations.gov"
MIN_PAGE_SIZE = 5
SEARCH_PAGE_SIZE = 10
SEARCH_MATCHES_PER_TOKEN = 1
ATTACHMENT_DOCUMENT_SAMPLE = 1
REQUEST_DELAY_SECONDS = 1.0

REGULATIONS_CSV_FIELDS = [
    "id",
    "source",
    "federal_register_source_record_id",
    "federal_register_docket_id",
    "regulations_docket_id",
    "regulations_document_ids",
    "title",
    "docket_title",
    "docket_type",
    "agency_id",
    "docket_status",
    "document_types",
    "posted_date",
    "comment_start_date",
    "comment_end_date",
    "open_for_comment",
    "within_comment_period",
    "document_count",
    "comment_count",
    "attachment_count",
    "attachment_count_scope",
    "matched_via",
    "documents_url",
    "docket_url",
    "federal_register_url",
    "last_checked_at",
    "raw_json",
]

DOCKET_TOKEN_RE = re.compile(r"\b[A-Z][A-Z0-9]+-[A-Z0-9]+(?:-[A-Z0-9]+)*\b", re.IGNORECASE)
REGULATIONS_DOCKET_ID_RE = re.compile(r"^[A-Z][A-Z0-9]*(?:-[A-Z][A-Z0-9]*)*-\d{4}-\d{3,}$", re.IGNORECASE)


@dataclass(frozen=True)
class DocketRef:
    docket_id: str
    federal_register_source_record_id: str = ""
    federal_register_title: str = ""
    federal_register_url: str = ""


@dataclass
class RegulationsConfig:
    api_key: str
    docket_refs: list[DocketRef]
    max_records: int = 100
    attachment_document_sample: int = ATTACHMENT_DOCUMENT_SAMPLE


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def regulations_api_key_from_env(env_file: Path) -> str:
    load_env_file(env_file)
    return os.environ.get("REGULATIONS_API_KEY", "") or os.environ.get("REGULATIONS_GOV_API_KEY", "")


def load_docket_refs_from_federal_register(path: Path) -> list[DocketRef]:
    if not path.exists():
        return []

    refs: list[DocketRef] = []
    seen: set[tuple[str, str]] = set()
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            source_record_id = str(row.get("source_record_id") or "").strip()
            for docket_id in extract_docket_tokens(row.get("docket_ids")):
                key = (source_record_id, docket_id)
                if key in seen:
                    continue
                seen.add(key)
                refs.append(
                    DocketRef(
                        docket_id=docket_id,
                        federal_register_source_record_id=source_record_id,
                        federal_register_title=str(row.get("title") or ""),
                        federal_register_url=str(row.get("url") or ""),
                    )
                )
    return refs


def docket_refs_from_values(values: list[str]) -> list[DocketRef]:
    refs: list[DocketRef] = []
    seen: set[str] = set()
    for value in values:
        for docket_id in extract_docket_tokens(value):
            if docket_id in seen:
                continue
            seen.add(docket_id)
            refs.append(DocketRef(docket_id=docket_id))
    return refs


def extract_docket_tokens(value: Any) -> list[str]:
    text = str(value or "")
    tokens: list[str] = []
    for match in DOCKET_TOKEN_RE.findall(text):
        token = match.strip().upper()
        if token and token not in tokens:
            tokens.append(token)
    return tokens


def looks_like_regulations_docket_id(value: str) -> bool:
    return bool(REGULATIONS_DOCKET_ID_RE.match(value.strip()))


def fetch_regulations_updates(
    config: RegulationsConfig,
    progress: Callable[[str], None] | None = None,
) -> list[dict[str, str]]:
    if not config.api_key:
        raise ValueError("REGULATIONS_API_KEY is required")

    rows_by_id: dict[str, dict[str, str]] = {}
    resolution_cache: dict[str, list[tuple[str, str]]] = {}
    bundle_cache: dict[str, dict[str, Any]] = {}

    for ref in config.docket_refs:
        if len(rows_by_id) >= config.max_records:
            break
        emit(progress, f"Regulations.gov: resolve {ref.docket_id}")
        try:
            matches = resolution_cache.get(ref.docket_id)
            if matches is None:
                matches = resolve_regulations_dockets(config.api_key, ref.docket_id)
                resolution_cache[ref.docket_id] = matches

            if not matches:
                row = not_found_row(ref)
                rows_by_id[row["id"]] = row
                continue

            for regulations_docket_id, matched_via in matches:
                if len(rows_by_id) >= config.max_records:
                    break
                bundle = bundle_cache.get(regulations_docket_id)
                if bundle is None:
                    bundle = fetch_docket_bundle(config.api_key, regulations_docket_id, config.attachment_document_sample)
                    bundle_cache[regulations_docket_id] = bundle
                row = normalize_bundle(ref, regulations_docket_id, matched_via, bundle)
                rows_by_id[row["id"]] = row
        except Exception as exc:
            if "HTTP 429" in str(exc) or "OVER_RATE_LIMIT" in str(exc):
                emit(progress, "Regulations.gov: rate limit reached; stopping early")
                break
            row = error_row(ref, str(exc))
            rows_by_id[row["id"]] = row

    return sorted(rows_by_id.values(), key=sort_key, reverse=True)[: config.max_records]


def resolve_regulations_dockets(api_key: str, docket_id: str) -> list[tuple[str, str]]:
    direct = fetch_documents(api_key, "filter[docketId]", docket_id, MIN_PAGE_SIZE)
    direct_ids = actual_docket_ids(direct.get("data") or [])
    if direct_ids:
        return [(actual_id, "filter[docketId]") for actual_id in direct_ids]

    if looks_like_regulations_docket_id(docket_id):
        docket_detail = fetch_docket(api_key, docket_id)
        if docket_detail:
            return [(docket_id, "dockets/{id}")]

    search = fetch_documents(api_key, "filter[searchTerm]", docket_id, SEARCH_PAGE_SIZE)
    matched_ids: list[str] = []
    for item in search.get("data") or []:
        attrs = item.get("attributes") or {}
        actual_id = str(attrs.get("docketId") or "").strip()
        if not actual_id:
            continue
        if docket_id.lower() in candidate_text(item).lower() and actual_id not in matched_ids:
            matched_ids.append(actual_id)
            if len(matched_ids) >= SEARCH_MATCHES_PER_TOKEN:
                break
    if not matched_ids:
        data = search.get("data") or []
        if len(data) == 1:
            actual_id = str((data[0].get("attributes") or {}).get("docketId") or "").strip()
            if actual_id:
                matched_ids.append(actual_id)
    return [(actual_id, "filter[searchTerm]") for actual_id in matched_ids]


def fetch_docket_bundle(api_key: str, docket_id: str, attachment_sample: int) -> dict[str, Any]:
    documents = fetch_documents(api_key, "filter[docketId]", docket_id, MIN_PAGE_SIZE)
    sample_docs = documents.get("data") or []
    docket = fetch_docket(api_key, docket_id)
    comments = fetch_comments_meta(api_key, docket_id)
    attachment_counts = fetch_attachment_counts(api_key, sample_docs[: max(0, attachment_sample)])
    return {
        "docket": docket,
        "documents": sample_docs,
        "documents_meta": documents.get("meta") or {},
        "comments_meta": comments,
        "attachment_counts": attachment_counts,
    }


def fetch_documents(api_key: str, filter_name: str, value: str, page_size: int) -> dict[str, Any]:
    return regulations_get(
        api_key,
        "/documents",
        {
            filter_name: value,
            "page[size]": str(max(page_size, MIN_PAGE_SIZE)),
        },
    )


def fetch_docket(api_key: str, docket_id: str) -> dict[str, Any] | None:
    try:
        return regulations_get(api_key, f"/dockets/{urllib.parse.quote(docket_id)}", {}, not_found_ok=True)
    except RuntimeError as exc:
        if "HTTP 400" in str(exc) and "Invalid ID" in str(exc):
            return None
        raise


def fetch_comments_meta(api_key: str, docket_id: str) -> dict[str, Any]:
    data = regulations_get(
        api_key,
        "/comments",
        {
            "filter[docketId]": docket_id,
            "page[size]": str(MIN_PAGE_SIZE),
        },
    )
    return data.get("meta") or {}


def fetch_attachment_counts(api_key: str, documents: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for document in documents:
        document_id = str(document.get("id") or "").strip()
        if not document_id:
            continue
        data = regulations_get(api_key, f"/documents/{urllib.parse.quote(document_id)}/attachments", {}, not_found_ok=True)
        counts[document_id] = len((data or {}).get("data") or [])
    return counts


def normalize_bundle(ref: DocketRef, regulations_docket_id: str, matched_via: str, bundle: dict[str, Any]) -> dict[str, str]:
    documents = bundle.get("documents") or []
    docket = bundle.get("docket") or {}
    doc_attrs = [doc.get("attributes") or {} for doc in documents]
    docket_attrs = ((docket.get("data") or {}).get("attributes") or {}) if isinstance(docket, dict) else {}
    document_ids = [str(doc.get("id") or "") for doc in documents if doc.get("id")]
    document_count = meta_total(bundle.get("documents_meta"), len(documents))
    comment_count = meta_total(bundle.get("comments_meta"), 0)
    attachment_counts = bundle.get("attachment_counts") or {}
    attachment_count = sum(int(value or 0) for value in attachment_counts.values())
    comment_start = min_date(attr.get("commentStartDate") for attr in doc_attrs)
    comment_end = max_date(attr.get("commentEndDate") for attr in doc_attrs)
    posted = max_date(attr.get("postedDate") for attr in doc_attrs)
    open_for_comment = any_bool(attr.get("openForComment") for attr in doc_attrs)
    within_comment_period = any_bool(attr.get("withinCommentPeriod") for attr in doc_attrs)
    title = first_text(attr.get("title") for attr in doc_attrs) or str(docket_attrs.get("title") or ref.federal_register_title or "")
    docket_title = str(docket_attrs.get("title") or "")
    raw_json = {
        "federal_register": {
            "source_record_id": ref.federal_register_source_record_id,
            "docket_id": ref.docket_id,
            "title": ref.federal_register_title,
            "url": ref.federal_register_url,
        },
        "docket": slim_docket(docket),
        "documents": [slim_document(doc) for doc in documents],
        "documents_meta": bundle.get("documents_meta") or {},
        "comments_meta": bundle.get("comments_meta") or {},
        "attachment_counts": attachment_counts,
    }

    return {
        "id": stable_id("regulations", ref.federal_register_source_record_id or "direct", ref.docket_id, regulations_docket_id),
        "source": SOURCE_NAME,
        "federal_register_source_record_id": ref.federal_register_source_record_id,
        "federal_register_docket_id": ref.docket_id,
        "regulations_docket_id": regulations_docket_id,
        "regulations_document_ids": ";".join(document_ids),
        "title": clean_text(title, 400),
        "docket_title": clean_text(docket_title, 400),
        "docket_type": clean_text(docket_attrs.get("docketType") or "", 80),
        "agency_id": first_text([docket_attrs.get("agencyId"), *(attr.get("agencyId") for attr in doc_attrs)]),
        "docket_status": docket_status(open_for_comment, within_comment_period, comment_end, document_count),
        "document_types": ";".join(unique_text(attr.get("documentType") for attr in doc_attrs)),
        "posted_date": posted,
        "comment_start_date": comment_start,
        "comment_end_date": comment_end,
        "open_for_comment": bool_text(open_for_comment),
        "within_comment_period": bool_text(within_comment_period),
        "document_count": str(document_count),
        "comment_count": str(comment_count),
        "attachment_count": str(attachment_count),
        "attachment_count_scope": attachment_scope(document_count, len(attachment_counts)),
        "matched_via": matched_via,
        "documents_url": ";".join(f"https://www.regulations.gov/document/{doc_id}" for doc_id in document_ids),
        "docket_url": f"https://www.regulations.gov/docket/{regulations_docket_id}",
        "federal_register_url": ref.federal_register_url,
        "last_checked_at": now_iso(),
        "raw_json": json.dumps(raw_json, ensure_ascii=True, sort_keys=True, separators=(",", ":")),
    }


def not_found_row(ref: DocketRef) -> dict[str, str]:
    return empty_row(ref, "not_found", "", "", {"message": "No matching Regulations.gov docket found."})


def error_row(ref: DocketRef, message: str) -> dict[str, str]:
    return empty_row(ref, "error", "", "", {"error": message})


def empty_row(ref: DocketRef, status: str, regulations_docket_id: str, matched_via: str, raw: dict[str, Any]) -> dict[str, str]:
    return {
        "id": stable_id("regulations", ref.federal_register_source_record_id or "direct", ref.docket_id, "unresolved"),
        "source": SOURCE_NAME,
        "federal_register_source_record_id": ref.federal_register_source_record_id,
        "federal_register_docket_id": ref.docket_id,
        "regulations_docket_id": regulations_docket_id,
        "regulations_document_ids": "",
        "title": clean_text(ref.federal_register_title, 400),
        "docket_title": "",
        "docket_type": "",
        "agency_id": "",
        "docket_status": status,
        "document_types": "",
        "posted_date": "",
        "comment_start_date": "",
        "comment_end_date": "",
        "open_for_comment": "",
        "within_comment_period": "",
        "document_count": "0",
        "comment_count": "",
        "attachment_count": "",
        "attachment_count_scope": "",
        "matched_via": matched_via,
        "documents_url": "",
        "docket_url": "",
        "federal_register_url": ref.federal_register_url,
        "last_checked_at": now_iso(),
        "raw_json": json.dumps(raw, ensure_ascii=True, sort_keys=True, separators=(",", ":")),
    }


def upsert_regulations_updates(path: Path, new_rows: list[dict[str, str]]) -> tuple[int, int, int]:
    existing_rows = read_csv(path)
    by_id = {row.get("id", ""): row for row in existing_rows if row.get("id")}
    added = 0
    updated = 0

    for new_row in new_rows:
        row_id = new_row.get("id", "")
        if not row_id:
            continue
        old_row = by_id.get(row_id)
        if old_row is None:
            by_id[row_id] = {field: new_row.get(field, "") for field in REGULATIONS_CSV_FIELDS}
            added += 1
            continue
        merged = dict(old_row)
        changed = False
        for field in REGULATIONS_CSV_FIELDS:
            value = new_row.get(field, "")
            if merged.get(field, "") != value:
                merged[field] = value
                changed = True
        if changed:
            updated += 1
        by_id[row_id] = merged

    rows = sorted(by_id.values(), key=sort_key, reverse=True)
    write_csv(path, REGULATIONS_CSV_FIELDS, rows)
    return added, updated, len(rows)


def regulations_get(
    api_key: str,
    path: str,
    params: dict[str, Any] | None = None,
    *,
    timeout: int = 45,
    not_found_ok: bool = False,
) -> Any:
    request_params = dict(params or {})
    request_params["api_key"] = api_key
    url = REGULATIONS_BASE_URL + path
    if request_params:
        url += "?" + urllib.parse.urlencode(request_params)
    request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": USER_AGENT})

    last_error: Exception | None = None
    for attempt in range(3):
        try:
            time.sleep(REQUEST_DELAY_SECONDS)
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 404 and not_found_ok:
                return None
            body = exc.read(800).decode("utf-8", "replace")
            if exc.code in {500, 502, 503, 504} and attempt < 2:
                last_error = RuntimeError(f"HTTP {exc.code} from {sanitize_url(url)}: {body}")
                time.sleep(5 * (attempt + 1))
                continue
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


def actual_docket_ids(documents: list[dict[str, Any]]) -> list[str]:
    ids: list[str] = []
    for item in documents:
        actual_id = str((item.get("attributes") or {}).get("docketId") or "").strip()
        if actual_id and actual_id not in ids:
            ids.append(actual_id)
    return ids


def candidate_text(item: dict[str, Any]) -> str:
    attrs = item.get("attributes") or {}
    return " ".join(
        clean_text(value, 1000)
        for value in [
            item.get("id"),
            attrs.get("docketId"),
            attrs.get("title"),
            attrs.get("subject"),
            attrs.get("docAbstract"),
            attrs.get("highlightedContent"),
        ]
    )


def slim_docket(docket: dict[str, Any] | None) -> dict[str, Any]:
    if not docket:
        return {}
    data = docket.get("data") or {}
    attrs = data.get("attributes") or {}
    keep = ["agencyId", "title", "docketType", "dkAbstract", "rin", "modifyDate", "effectiveDate", "keywords"]
    return {"id": data.get("id"), "type": data.get("type"), "attributes": {key: attrs.get(key) for key in keep}}


def slim_document(document: dict[str, Any]) -> dict[str, Any]:
    attrs = document.get("attributes") or {}
    keep = [
        "agencyId",
        "docketId",
        "documentType",
        "title",
        "postedDate",
        "commentStartDate",
        "commentEndDate",
        "openForComment",
        "withinCommentPeriod",
        "withdrawn",
        "frDocNum",
        "subtype",
    ]
    return {"id": document.get("id"), "type": document.get("type"), "attributes": {key: attrs.get(key) for key in keep}}


def meta_total(meta: Any, default: int) -> int:
    if isinstance(meta, dict):
        try:
            return int(meta.get("totalElements"))
        except (TypeError, ValueError):
            pass
    return default


def unique_text(values: Any) -> list[str]:
    output: list[str] = []
    for value in values:
        text = clean_text(value, 120)
        if text and text not in output:
            output.append(text)
    return output


def first_text(values: Any) -> str:
    for value in values:
        text = clean_text(value, 400)
        if text:
            return text
    return ""


def any_bool(values: Any) -> bool:
    return any(value is True or str(value).lower() == "true" for value in values)


def bool_text(value: bool | None) -> str:
    if value is None:
        return ""
    return "true" if value else "false"


def min_date(values: Any) -> str:
    parsed = sorted(date for date in (iso_date(value) for value in values) if date)
    return parsed[0] if parsed else ""


def max_date(values: Any) -> str:
    parsed = sorted(date for date in (iso_date(value) for value in values) if date)
    return parsed[-1] if parsed else ""


def docket_status(open_for_comment: bool, within_comment_period: bool, comment_end: str, document_count: int) -> str:
    if open_for_comment or within_comment_period:
        return "open_for_comment"
    parsed_end = parse_date(comment_end)
    if parsed_end:
        return "open_for_comment" if parsed_end >= dt.date.today() else "closed"
    if document_count > 0:
        return "metadata_only"
    return "unknown"


def attachment_scope(document_count: int, sampled_count: int) -> str:
    if sampled_count <= 0:
        return "none_sampled"
    if document_count > sampled_count:
        return f"first_{sampled_count}_of_{document_count}_documents"
    return "all_documents"


def sort_key(row: dict[str, str]) -> tuple[int, int, int, str]:
    status_rank = {"open_for_comment": 4, "metadata_only": 3, "closed": 2, "not_found": 1, "error": 0}.get(row.get("docket_status", ""), 0)
    return (
        status_rank,
        int_or_zero(row.get("comment_count")),
        int_or_zero(row.get("document_count")),
        row.get("comment_end_date") or row.get("posted_date") or "",
    )


def clean_text(value: Any, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "..."


def parse_date(value: Any) -> dt.date | None:
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z"):
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


def stable_id(*parts: str) -> str:
    raw = "-".join(part for part in parts if part)
    cleaned = re.sub(r"[^a-zA-Z0-9_-]+", "-", raw.strip())[:180].strip("-")
    return cleaned or "regulations-record"


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
