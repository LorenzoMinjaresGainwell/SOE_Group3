from __future__ import annotations

import csv
import datetime as dt
import hashlib
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

from services.sam_cache import DEFAULT_SAM_LEDGER_PATH, DEFAULT_SAM_RAW_CACHE_DIR, RawSAMCache
from services.sam_quota import SAMLiveCallBlocked, SAMQuotaError, SAMQuotaGuard, policy_from_settings

SAM_ENTITY_URL = "https://api.sam.gov/entity-information/v4/entities"
SAM_ENTITY_SOURCE_KEY = "sam_entity_information"
USER_AGENT = "soe-group3-sam-entity/0.1"

VENDOR_ENTITY_FIELDS = [
    "vendor_key",
    "search_name",
    "matched_name",
    "legal_business_name",
    "dba_name",
    "uei_sam",
    "cage_code",
    "entity_status",
    "registration_expiration_date",
    "physical_city",
    "physical_state",
    "physical_zip",
    "sam_entity_url",
    "match_confidence",
    "match_reason",
    "source_key",
    "source_record_id",
    "last_checked_at",
    "raw_json",
]

VENDOR_ALIAS_FIELDS = [
    "vendor_key",
    "vendor_name",
    "search_name",
    "alias_type",
    "last_checked_at",
]

LEGAL_SUFFIXES = {
    "CO",
    "COMPANY",
    "CORP",
    "CORPORATION",
    "INC",
    "INCORPORATED",
    "LLC",
    "LLP",
    "LP",
    "LTD",
    "LIMITED",
    "SERVICES",
    "SERVICE",
    "THE",
}


class SamEntityError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None, blocked: bool = False) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.blocked = blocked


@dataclass(frozen=True)
class VendorQuery:
    vendor_key: str
    vendor_name: str
    search_name: str
    alias_type: str = "alias"


@dataclass
class SamEntityConfig:
    api_key: str = ""
    max_results: int = 5
    include_sections: str = "entityRegistration,coreData"
    timeout: int = 45
    retry_count: int = 2
    sam_quota_mode: str = "cache-only"
    sam_live_budget: int = 0
    sam_cache_dir: Path = DEFAULT_SAM_RAW_CACHE_DIR
    sam_ledger_path: Path = DEFAULT_SAM_LEDGER_PATH
    allow_alias_live_searches: bool = False
    allow_live_uei_refresh: bool = False
    approved_live_vendor_keys: tuple[str, ...] = ("gainwell_technologies", "maximus", "deloitte")


@dataclass
class ResolveResult:
    rows: list[dict[str, str]]
    errors: list[str]
    queries_run: int
    refreshed: int


def load_search_parameters(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


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


def build_vendor_queries(
    params: dict[str, Any],
    vendors_filter: list[str] | None = None,
    vendor_groups: list[str] | None = None,
) -> list[VendorQuery]:
    groups: dict[str, dict[str, Any]] = {}

    for item in params.get("vendors") or []:
        if isinstance(item, str):
            add_vendor_alias(groups, item, item, "canonical")
            continue
        if not isinstance(item, dict):
            continue
        name = clean_text(item.get("name"), 180)
        if not name:
            continue
        add_vendor_alias(groups, name, name, "canonical")
        for alias in item.get("aliases") or []:
            add_vendor_alias(groups, name, clean_text(alias, 180), "alias")

    state_groups = ((params.get("state_contracts") or {}).get("vendor_terms_by_group") or {})
    if isinstance(state_groups, dict):
        for group_name, aliases in state_groups.items():
            name = clean_text(group_name, 180)
            if not name:
                continue
            add_vendor_alias(groups, name, name, "canonical")
            for alias in aliases or []:
                add_vendor_alias(groups, name, clean_text(alias, 180), "state_contract_alias")

    selectors = [normalize_selector(value) for value in [*(vendors_filter or []), *(vendor_groups or [])] if value]
    selected_keys: set[str]
    if selectors:
        selected_keys = {
            key
            for key, group in groups.items()
            if key in selectors or normalize_selector(str(group.get("vendor_name") or "")) in selectors
        }
        for selector, original in zip(selectors, [*(vendors_filter or []), *(vendor_groups or [])]):
            if selector not in selected_keys and selector not in groups:
                add_vendor_alias(groups, original, original, "direct")
                selected_keys.add(vendor_key(original))
    else:
        selected_keys = set(groups)

    queries: list[VendorQuery] = []
    for key in sorted(selected_keys):
        group = groups.get(key)
        if not group:
            continue
        vendor_name = str(group["vendor_name"])
        aliases = group.get("aliases") or []
        alias_types = group.get("alias_types") or {}
        for alias in aliases:
            queries.append(VendorQuery(key, vendor_name, alias, alias_types.get(alias, "alias")))
    return queries


def add_vendor_alias(groups: dict[str, dict[str, Any]], vendor_name: str, alias: str, alias_type: str) -> None:
    vendor_name = clean_text(vendor_name, 180)
    alias = clean_text(alias, 180)
    if not vendor_name or not alias:
        return
    key = vendor_key(vendor_name)
    group = groups.setdefault(key, {"vendor_name": vendor_name, "aliases": [], "alias_types": {}})
    group["vendor_name"] = vendor_name
    if alias not in group["aliases"]:
        group["aliases"].append(alias)
    group["alias_types"].setdefault(alias, alias_type)


def resolve_vendor_entities(
    config: SamEntityConfig,
    queries: list[VendorQuery],
    existing_rows: list[dict[str, str]] | None = None,
    refresh_existing: bool = True,
    progress: Callable[[str], None] | None = None,
) -> ResolveResult:
    rows_by_key: dict[str, dict[str, str]] = {}
    errors: list[str] = []
    queries_run = 0
    refreshed = 0
    checked_at = now_iso()
    if sam_live_enabled(config):
        if not config.allow_live_uei_refresh:
            refresh_existing = False
        if not config.allow_alias_live_searches:
            queries = [query for query in queries if query.alias_type in {"canonical", "direct"}]
        if config.approved_live_vendor_keys:
            approved = set(config.approved_live_vendor_keys)
            queries = [query for query in queries if query.vendor_key in approved]

    if refresh_existing:
        seen_ueis: set[str] = set()
        for existing in existing_rows or []:
            uei = clean_text(existing.get("uei_sam"), 40)
            if not uei or uei in seen_ueis:
                continue
            seen_ueis.add(uei)
            query = VendorQuery(
                vendor_key=existing.get("vendor_key") or vendor_key(existing.get("matched_name") or uei),
                vendor_name=existing.get("vendor_key") or existing.get("matched_name") or uei,
                search_name=uei,
                alias_type="uei_refresh",
            )
            try:
                emit(progress, f"SAM entity refresh: vendor={query.vendor_key} uei={uei}")
                item = fetch_entity_by_uei(config, uei)
                queries_run += 1
                if item:
                    row = normalize_entity(item, query, checked_at)
                    merge_entity_row(rows_by_key, row)
                    refreshed += 1
            except SamEntityError as exc:
                errors.append(f"uei refresh {uei}: {exc}")
                if exc.status_code == 429 or exc.blocked:
                    return ResolveResult(sorted_entity_rows(rows_by_key), errors, queries_run, refreshed)

    for query in queries:
        try:
            emit(progress, f"SAM entity search: vendor={query.vendor_key} search={query.search_name}")
            items = fetch_entities_by_name(config, query.search_name)
            queries_run += 1
        except SamEntityError as exc:
            errors.append(f"search {query.vendor_key}/{query.search_name}: {exc}")
            if exc.status_code == 429 or exc.blocked:
                break
            continue

        for item in items:
            row = normalize_entity(item, query, checked_at)
            merge_entity_row(rows_by_key, row)

    return ResolveResult(sorted_entity_rows(rows_by_key), errors, queries_run, refreshed)


def fetch_entities_by_name(config: SamEntityConfig, search_name: str) -> list[dict[str, Any]]:
    params = {
        "api_key": config.api_key,
        "legalBusinessName": search_name,
        "includeSections": config.include_sections,
        "size": str(max(1, min(config.max_results, 100))),
    }
    data = http_json(SAM_ENTITY_URL, params, config)
    rows = data.get("entityData") if isinstance(data, dict) else []
    return [row for row in rows or [] if isinstance(row, dict)][: config.max_results]


def fetch_entity_by_uei(config: SamEntityConfig, uei_sam: str) -> dict[str, Any] | None:
    params = {
        "api_key": config.api_key,
        "ueiSAM": uei_sam,
        "includeSections": config.include_sections,
        "size": "1",
    }
    data = http_json(SAM_ENTITY_URL, params, config)
    rows = data.get("entityData") if isinstance(data, dict) else []
    for row in rows or []:
        if isinstance(row, dict):
            return row
    return None


def normalize_entity(item: dict[str, Any], query: VendorQuery, checked_at: str) -> dict[str, str]:
    registration = item.get("entityRegistration") if isinstance(item.get("entityRegistration"), dict) else {}
    core_data = item.get("coreData") if isinstance(item.get("coreData"), dict) else {}
    physical = core_data.get("physicalAddress") if isinstance(core_data.get("physicalAddress"), dict) else {}

    legal_name = clean_text(registration.get("legalBusinessName"), 240)
    dba_name = clean_text(registration.get("dbaName"), 240)
    matched_name = legal_name or dba_name or query.search_name
    uei_sam = clean_text(registration.get("ueiSAM"), 40)
    cage_code = clean_text(registration.get("cageCode"), 20)
    entity_status = clean_text(registration.get("registrationStatus") or registration.get("samRegistered"), 80)
    confidence, reason = match_quality(query.search_name, legal_name, dba_name, entity_status, uei_sam, cage_code)

    return {
        "vendor_key": query.vendor_key,
        "search_name": query.search_name,
        "matched_name": matched_name,
        "legal_business_name": legal_name,
        "dba_name": dba_name,
        "uei_sam": uei_sam,
        "cage_code": cage_code,
        "entity_status": entity_status,
        "registration_expiration_date": iso_date(registration.get("registrationExpirationDate")),
        "physical_city": clean_text(first_value(physical, ["city", "cityName"]), 80),
        "physical_state": clean_text(first_value(physical, ["stateOrProvinceCode", "state", "province"]), 40),
        "physical_zip": clean_text(first_value(physical, ["zipCode", "zip", "postalCode", "zip4"]), 20),
        "sam_entity_url": sam_entity_url(uei_sam),
        "match_confidence": f"{confidence:.2f}",
        "match_reason": reason,
        "source_key": SAM_ENTITY_SOURCE_KEY,
        "source_record_id": uei_sam or cage_code or stable_hash(matched_name + query.search_name),
        "last_checked_at": checked_at,
        "raw_json": json.dumps(item, ensure_ascii=True, sort_keys=True, separators=(",", ":")),
    }


def match_quality(search_name: str, legal_name: str, dba_name: str, status: str, uei_sam: str, cage_code: str) -> tuple[float, str]:
    search_norm = normalize_name(search_name)
    legal_norm = normalize_name(legal_name)
    dba_norm = normalize_name(dba_name)
    search_core = normalize_name(search_name, drop_suffixes=True)
    legal_core = normalize_name(legal_name, drop_suffixes=True)
    dba_core = normalize_name(dba_name, drop_suffixes=True)
    has_identifier = bool(uei_sam or cage_code)
    is_active = active_status(status)

    if search_norm and search_norm == legal_norm:
        confidence = 0.96
        reason = "exact legal name"
    elif search_norm and search_norm == dba_norm:
        confidence = 0.93
        reason = "exact dba name"
    elif search_core and search_core == legal_core:
        confidence = 0.90
        reason = "legal name matches after suffix normalization"
    elif search_core and search_core == dba_core:
        confidence = 0.87
        reason = "dba name matches after suffix normalization"
    elif has_identifier and containment_match(search_core, legal_core, dba_core):
        confidence = 0.76
        reason = "name containment with UEI/CAGE evidence"
    elif has_identifier and max(token_overlap(search_core, legal_core), token_overlap(search_core, dba_core)) >= 0.75:
        confidence = 0.64
        reason = "token overlap with UEI/CAGE evidence"
    else:
        confidence = 0.38
        reason = "weak name evidence; verify manually"

    reason_parts = [reason]
    if has_identifier:
        confidence += 0.02
        reason_parts.append("UEI/CAGE present")
    else:
        confidence = min(confidence, 0.70)
        reason_parts.append("no UEI/CAGE returned")
    if is_active:
        confidence += 0.02
        reason_parts.append("active registration")
    elif status:
        confidence -= 0.08
        reason_parts.append(f"status={status}")

    return max(0.0, min(confidence, 0.99)), "; ".join(reason_parts)


def upsert_vendor_entities(path: Path, new_rows: list[dict[str, str]]) -> tuple[int, int, int]:
    existing_rows = read_csv(path)
    by_key = {entity_row_key(row): {field: row.get(field, "") for field in VENDOR_ENTITY_FIELDS} for row in existing_rows if entity_row_key(row)}
    added = 0
    updated = 0

    for row in new_rows:
        key = entity_row_key(row)
        if not key:
            continue
        old = by_key.get(key)
        if old is None:
            by_key[key] = {field: row.get(field, "") for field in VENDOR_ENTITY_FIELDS}
            added += 1
            continue

        merged = merge_rows(old, row)
        if any(old.get(field, "") != merged.get(field, "") for field in VENDOR_ENTITY_FIELDS):
            updated += 1
        by_key[key] = merged

    rows = sorted_entity_list(by_key.values())
    write_csv(path, VENDOR_ENTITY_FIELDS, rows)
    return added, updated, len(rows)


def write_vendor_aliases(path: Path, queries: list[VendorQuery]) -> int:
    checked_at = now_iso()
    rows = [
        {
            "vendor_key": query.vendor_key,
            "vendor_name": query.vendor_name,
            "search_name": query.search_name,
            "alias_type": query.alias_type,
            "last_checked_at": checked_at,
        }
        for query in queries
    ]
    write_csv(path, VENDOR_ALIAS_FIELDS, rows)
    return len(rows)


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def http_json(url: str, params: dict[str, Any], config: SamEntityConfig) -> Any:
    if sam_live_enabled(config) and not config.api_key:
        raise SamEntityError("SAM_API_KEY not configured for live SAM mode", blocked=True)

    guard = sam_quota_guard(config)
    cached = guard.cache.get("GET", url, params)
    if cached is not None:
        data = cached_response_json(cached)
        guard.log_cache_hit("GET", url, params, record_count=sam_entity_record_count(data), caller="sam_entities")
        return data

    full_url = url + "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(full_url, headers={"Accept": "application/json", "User-Agent": USER_AGENT})
    last_error: Exception | None = None

    for attempt in range(config.retry_count + 1):
        try:
            guard.require_live_call("GET", url, params, caller="sam_entities")
        except (SAMLiveCallBlocked, SAMQuotaError) as exc:
            raise SamEntityError(str(exc), blocked=True) from exc
        try:
            with urllib.request.urlopen(request, timeout=config.timeout) as response:
                response_text = response.read().decode("utf-8")
                data = json.loads(response_text)
                guard.cache.put("GET", url, params, None, response.status, response_text, dict(response.headers.items()))
                guard.log_live_result("GET", url, params, status="live_ok", record_count=sam_entity_record_count(data), caller="sam_entities")
                return data
        except urllib.error.HTTPError as exc:
            body = redact_secret(exc.read(800).decode("utf-8", "replace"), config.api_key)
            if exc.code == 429:
                guard.log_live_result("GET", url, params, status="rate_limited", caller="sam_entities", note="SAM_API_KEY 429")
                raise SamEntityError("SAM_API_KEY 429", status_code=429) from exc
            guard.log_live_result("GET", url, params, status="http_error", caller="sam_entities", note=f"HTTP {exc.code}")
            message = f"HTTP {exc.code} from {sanitize_url(full_url)}: {body}"
            raise SamEntityError(message, status_code=exc.code) from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = exc
            status = "timeout" if isinstance(exc, TimeoutError) else "live_error"
            message = redact_secret(str(exc), config.api_key)
            guard.log_live_result("GET", url, params, status=status, caller="sam_entities", note=message)
            if attempt < config.retry_count:
                time.sleep(1 + attempt)
    message = redact_secret(str(last_error), config.api_key)
    raise SamEntityError(f"request failed: {message}")


def sam_live_enabled(config: SamEntityConfig) -> bool:
    return str(config.sam_quota_mode).strip().lower() == "live" and config.sam_live_budget > 0


def sam_quota_guard(config: SamEntityConfig) -> SAMQuotaGuard:
    policy = policy_from_settings(config.sam_quota_mode, config.sam_live_budget, config.sam_ledger_path)
    cache = RawSAMCache(root=config.sam_cache_dir, ledger_path=config.sam_ledger_path)
    return SAMQuotaGuard(policy=policy, cache=cache)


def cached_response_json(cached: dict[str, Any]) -> Any:
    return json.loads(str(cached.get("response_text") or "{}"))


def sam_entity_record_count(data: Any) -> int:
    rows = data.get("entityData") if isinstance(data, dict) else []
    return len(rows) if isinstance(rows, list) else 0


def merge_entity_row(rows_by_key: dict[str, dict[str, str]], row: dict[str, str]) -> None:
    key = entity_row_key(row)
    if not key:
        return
    old = rows_by_key.get(key)
    rows_by_key[key] = row if old is None else merge_rows(old, row)


def merge_rows(old: dict[str, str], new: dict[str, str]) -> dict[str, str]:
    old_conf = float_or_zero(old.get("match_confidence"))
    new_conf = float_or_zero(new.get("match_confidence"))
    merged = dict(new if new_conf >= old_conf else old)
    merged["search_name"] = join_unique(old.get("search_name", ""), new.get("search_name", ""))
    merged["match_reason"] = join_unique(old.get("match_reason", ""), new.get("match_reason", ""))
    for field in VENDOR_ENTITY_FIELDS:
        if not merged.get(field):
            merged[field] = new.get(field) or old.get(field, "")
    return merged


def entity_row_key(row: dict[str, str]) -> str:
    vendor = row.get("vendor_key") or vendor_key(row.get("matched_name", ""))
    uei = row.get("uei_sam", "").strip()
    cage = row.get("cage_code", "").strip()
    if uei:
        return f"{vendor}|uei:{uei}"
    if cage:
        return f"{vendor}|cage:{cage}|name:{normalize_name(row.get('legal_business_name', ''), drop_suffixes=True)}"
    return f"{vendor}|record:{row.get('source_record_id', '')}"


def sorted_entity_rows(rows_by_key: dict[str, dict[str, str]]) -> list[dict[str, str]]:
    return sorted_entity_list(rows_by_key.values())


def sorted_entity_list(rows: Any) -> list[dict[str, str]]:
    return sorted(
        rows,
        key=lambda row: (
            row.get("vendor_key", ""),
            -float_or_zero(row.get("match_confidence")),
            row.get("legal_business_name", ""),
            row.get("uei_sam", ""),
        ),
    )


def counts_by_vendor(rows: list[dict[str, str]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        key = row.get("vendor_key") or "unknown"
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


def alias_rows_by_vendor(queries: list[VendorQuery]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for query in queries:
        counts[query.vendor_key] = counts.get(query.vendor_key, 0) + 1
    return dict(sorted(counts.items()))


def vendor_key(value: Any) -> str:
    text = re.sub(r"[^a-z0-9]+", "_", str(value or "").lower()).strip("_")
    return text or "unknown"


def normalize_selector(value: str) -> str:
    return vendor_key(value)


def normalize_name(value: str, drop_suffixes: bool = False) -> str:
    words = re.findall(r"[A-Z0-9]+", str(value or "").upper())
    if drop_suffixes:
        words = [word for word in words if word not in LEGAL_SUFFIXES]
    return " ".join(words)


def containment_match(search_core: str, legal_core: str, dba_core: str) -> bool:
    if not search_core:
        return False
    return any(search_core in candidate or candidate in search_core for candidate in [legal_core, dba_core] if candidate)


def token_overlap(left: str, right: str) -> float:
    left_tokens = set(left.split())
    right_tokens = set(right.split())
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / len(left_tokens | right_tokens)


def active_status(value: str) -> bool:
    lower = str(value or "").lower()
    return "active" in lower and "inactive" not in lower


def iso_date(value: Any) -> str:
    parsed = parse_date(value)
    return parsed.isoformat() if parsed else ""


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


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def clean_text(value: Any, limit: int = 500) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "..."


def first_value(data: dict[str, Any], keys: list[str]) -> Any:
    for key in keys:
        value = data.get(key)
        if value not in (None, ""):
            return value
    return ""


def sam_entity_url(uei_sam: str) -> str:
    return f"https://sam.gov/entity/{urllib.parse.quote(uei_sam)}/coreData?status=active" if uei_sam else ""


def stable_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]


def join_unique(*values: str) -> str:
    items: list[str] = []
    for value in values:
        for item in str(value or "").split(";"):
            item = item.strip()
            if item and item not in items:
                items.append(item)
    return ";".join(items)


def float_or_zero(value: Any) -> float:
    try:
        return float(value or 0)
    except ValueError:
        return 0.0


def sanitize_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    redacted = [(key, "REDACTED") if key.lower() in {"api_key", "apikey"} else (key, value) for key, value in query]
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urllib.parse.urlencode(redacted), parsed.fragment))


def redact_secret(text: str, secret: str) -> str:
    if not secret:
        return text
    return text.replace(secret, "REDACTED")


def emit(progress: Callable[[str], None] | None, message: str) -> None:
    if progress:
        progress(message)
