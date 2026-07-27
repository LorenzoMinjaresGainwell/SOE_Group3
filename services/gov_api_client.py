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
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from services.sam_cache import DEFAULT_SAM_LEDGER_PATH, DEFAULT_SAM_RAW_CACHE_DIR, RawSAMCache
from services.sam_quota import SAMLiveCallBlocked, SAMQuotaError, SAMQuotaGuard, policy_from_settings
from services.usaspending_client import (
    USASpendingConfig,
    VendorSearch,
    fetch_vendor_contracts,
    load_search_parameters,
    vendor_searches,
)

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
USER_AGENT = "soe-group3-gov-search/0.1"

OPPORTUNITY_FIELDS = [
    "id",
    "title",
    "state",
    "agency",
    "source",
    "source_url",
    "document_url",
    "document_type",
    "due_date",
    "posted_date",
    "last_checked_at",
    "last_updated_at",
    "budget_estimate",
    "eligibility",
    "eligibility_reason",
    "fit_score",
    "ai_recommendation",
    "status",
    "program_focus",
    "keywords_matched",
    "risks",
    "summary",
]

FEDERAL_OPPORTUNITY_FIELDS = [
    "opportunity_id",
    "source_key",
    "sam_notice_id",
    "solicitation_number",
    "title",
    "notice_type",
    "ptype",
    "notice_bucket",
    "record_type",
    "agency",
    "subagency",
    "office",
    "posted_date",
    "updated_date",
    "due_date",
    "archive_date",
    "naics",
    "psc",
    "set_aside",
    "place_of_performance_state",
    "program_focus",
    "topic_keys",
    "vendor_keys_mentioned",
    "lifecycle_status",
    "importance_score",
    "score_evidence_json",
    "document_url",
    "source_url",
    "summary",
    "raw_json",
    "last_checked_at",
]

FEDERAL_GRANT_FIELDS = [
    "grant_id",
    "opportunity_number",
    "opportunity_title",
    "agency",
    "agency_code",
    "posted_date",
    "close_date",
    "archive_date",
    "award_ceiling",
    "award_floor",
    "estimated_total_program_funding",
    "expected_awards",
    "eligibility",
    "funding_category",
    "assistance_listing_number",
    "program_focus",
    "topic_keys",
    "rht_flag",
    "importance_score",
    "predictive_value_usd",
    "score_evidence_json",
    "document_url",
    "raw_json",
    "last_checked_at",
]

SOURCE_FIELDS = [
    "id",
    "state",
    "name",
    "type",
    "url",
    "last_checked_at",
    "status",
    "opportunities_found",
]

SOURCE_RUN_FIELDS = [
    "source",
    "mode",
    "last_successful_run",
    "started_at",
    "finished_at",
    "status",
    "records_found",
    "records_added",
    "records_updated",
    "message",
]

DEFAULT_KEYWORDS = [
    "medicaid",
    "medicare",
    "CMS",
    "rural health",
    "rural health transformation",
    "RFP",
    "RFI",
    "solicitation",
    "sources sought",
    "grant",
    "waiver",
    "1115",
    "SPA",
    "managed care",
    "MMIS",
    "eligibility",
    "enrollment",
    "claims",
    "interoperability",
    "FHIR",
    "prior authorization",
    "provider data",
    "quality measures",
    "telehealth",
    "behavioral health",
    "workforce",
]

SOURCE_META = {
    "sam": {
        "id": "api-sam-opportunities",
        "name": "SAM.gov Opportunities API",
        "type": "Federal procurement API",
        "url": "https://api.sam.gov/opportunities/v2/search",
    },
    "grants": {
        "id": "api-grants-gov",
        "name": "Grants.gov Search API",
        "type": "Federal grants API",
        "url": "https://api.grants.gov/v1/api/search2",
    },
    "federal_register": {
        "id": "api-federal-register",
        "name": "Federal Register API",
        "type": "Federal policy API",
        "url": "https://www.federalregister.gov/api/v1/documents.json",
    },
    "medicaid": {
        "id": "api-data-medicaid",
        "name": "data.medicaid.gov Catalog API",
        "type": "Medicaid data catalog API",
        "url": "https://data.medicaid.gov/api/1/metastore/schemas/dataset/items",
    },
    "cms_provider": {
        "id": "api-cms-provider-data",
        "name": "CMS Provider Data Catalog API",
        "type": "CMS data catalog API",
        "url": "https://data.cms.gov/provider-data/api/1/metastore/schemas/dataset/items",
    },
    "usaspending": {
        "id": "api-usaspending-awards",
        "name": "USAspending.gov Awards API",
        "type": "Federal award history API",
        "url": "https://api.usaspending.gov/api/v2/search/spending_by_award/",
    },
}

SAM_PTYPE_LABELS = {
    "u": "Justification (J&A)",
    "p": "Pre-solicitation",
    "a": "Award Notice",
    "r": "Sources Sought",
    "s": "Special Notice",
    "o": "Solicitation",
    "g": "Sale of Surplus Property",
    "k": "Combined Synopsis/Solicitation",
    "i": "Intent to Bundle Requirements",
}

DEFAULT_SAM_PTYPES = ["o", "k", "r", "p"]
SAM_PTYPE_EVALUATION_SET = ["o", "k", "r", "p", "a", "s", "u", "i"]
SAM_NOTICE_BUCKETS = {
    "o": "active_opportunity",
    "k": "active_opportunity",
    "r": "early_signal",
    "p": "early_signal",
    "a": "award_notice",
    "s": "market_intel",
    "u": "market_intel",
    "i": "market_intel",
}
SAM_TARGET_AGENCY_TERMS = [
    "department of health and human services",
    "hhs",
    "centers for medicare",
    "centers for medicaid",
    "cms",
    "health resources and services administration",
    "hrsa",
    "administration for community living",
    "acl",
    "agency for healthcare research",
    "ahrq",
    "centers for disease control",
    "cdc",
]
SAM_HIGH_VALUE_TERMS = {
    "medicaid",
    "medicare",
    "cms",
    "mmis",
    "claims",
    "eligibility",
    "rural health",
    "rural health transformation",
    "critical access hospital",
}
SAM_APPROVED_LIVE_PTYPES = {"o", "k", "r", "p", "a"}

GRANTS_FETCH_ENDPOINT = "https://api.grants.gov/v1/api/fetchOpportunity"
GRANTS_DEFAULT_AGENCIES = "HHS-CMS|HHS-HRSA"
GRANTS_RHT_TERMS = [
    "rural health transformation",
    "rht",
    "rural health",
    "rural hospital",
    "critical access hospital",
    "frontier",
]

KEYWORD_WEIGHTS = {
    "rfp": 15,
    "solicitation": 14,
    "sources sought": 12,
    "request for information": 10,
    "rfi": 10,
    "medicaid": 12,
    "medicare": 10,
    "cms": 8,
    "mmis": 14,
    "claims": 10,
    "eligibility": 10,
    "enrollment": 8,
    "managed care": 8,
    "interoperability": 8,
    "fhir": 8,
    "prior authorization": 8,
    "rural health transformation": 12,
    "rural health": 10,
    "critical access hospital": 8,
    "telehealth": 6,
    "behavioral health": 6,
    "workforce": 5,
    "grant": 4,
    "waiver": 5,
    "1115": 5,
    "quality measures": 4,
}



@dataclass
class SearchConfig:
    mode: str = "continue"
    sources: list[str] | None = None
    keywords: list[str] | None = None
    max_per_source: int = 25
    days_back: int = 60
    overlap_days: int = 14
    start_date: dt.date | None = None
    end_date: dt.date | None = None
    data_dir: Path = DATA_DIR
    vendors: list[str] | None = None
    sam_ptypes: list[str] | None = None
    dry_run: bool = False
    env_file: Path = ROOT / ".env"
    sam_api_key: str = ""
    sam_quota_mode: str = "cache-only"
    sam_live_budget: int = 0
    sam_cache_dir: Path = DEFAULT_SAM_RAW_CACHE_DIR
    sam_ledger_path: Path = DEFAULT_SAM_LEDGER_PATH
    grants_agencies: str = GRANTS_DEFAULT_AGENCIES


def refresh_opportunities() -> dict:
    """Web entry point used by POST /api/refresh.

    The web path intentionally runs continue mode only. Historic backfills should
    be run from scripts/run_gov_search.py so they can be resumed and observed.
    """
    return run_gov_search(SearchConfig(mode="continue", max_per_source=25))


def run_gov_search(config: SearchConfig | None = None, progress: Callable[[str], None] | None = None) -> dict:
    config = config or SearchConfig()
    load_env_file(config.env_file)
    if not config.sam_api_key:
        config.sam_api_key = os.environ.get("SAM_API_KEY", "")

    sources = config.sources or list(SOURCE_META)
    keywords = config.keywords or DEFAULT_KEYWORDS
    config.keywords = keywords
    if "usaspending" in sources and not config.vendors:
        params = load_search_parameters(config.data_dir / "search_parameters.json")
        config.vendors = [vendor.name for vendor in vendor_searches(params)]

    started_at = now_iso()
    all_records: list[dict[str, str]] = []
    source_summaries: list[dict[str, Any]] = []

    for source in sources:
        if source not in SOURCE_FETCHERS:
            source_summaries.append({"source": source, "status": "skipped", "message": "unknown source"})
            continue

        source_started = now_iso()
        emit(progress, f"checking {source}...")
        try:
            records = SOURCE_FETCHERS[source](config)
            source_summary = {
                "source": source,
                "status": "ok",
                "records_found": len(records),
                "message": f"{len(records)} records found",
            }
            source_summary.update(source_record_summary(source, records))
            all_records.extend(records)
            emit(progress, f"{source}: {len(records)} records")
        except Exception as exc:
            records = []
            source_summary = {
                "source": source,
                "status": "error",
                "records_found": 0,
                "message": str(exc),
            }
            emit(progress, f"{source}: error: {exc}")

        source_summaries.append(source_summary)
        if not config.dry_run:
            upsert_source_run(
                config.data_dir,
                {
                    "source": source,
                    "mode": config.mode,
                    "last_successful_run": now_iso() if source_summary["status"] == "ok" else "",
                    "started_at": source_started,
                    "finished_at": now_iso(),
                    "status": source_summary["status"],
                    "records_found": str(source_summary["records_found"]),
                    "records_added": "0",
                    "records_updated": "0",
                    "message": source_summary["message"],
                },
            )
            upsert_source_status(config.data_dir, source, source_summary)

    if config.dry_run:
        added = updated = federal_added = federal_updated = grant_added = grant_updated = 0
    else:
        added, updated = upsert_opportunities(config.data_dir, all_records)
        federal_added, federal_updated = upsert_federal_opportunities(config.data_dir, all_records)
        grant_added, grant_updated = upsert_federal_grants(config.data_dir, all_records)
        stamp_source_run_counts(config.data_dir, sources, added, updated)

    finished_at = now_iso()
    status = "ok" if all(summary["status"] in {"ok", "skipped"} for summary in source_summaries) else "partial"
    message = f"Gov search complete: {len(all_records)} found, {added} added, {updated} updated."
    if federal_added or federal_updated:
        message += f" Federal opportunities: {federal_added} added, {federal_updated} updated."
    if grant_added or grant_updated:
        message += f" Federal grants: {grant_added} added, {grant_updated} updated."
    return {
        "status": status,
        "message": message,
        "mode": config.mode,
        "started_at": started_at,
        "finished_at": finished_at,
        "records_found": len(all_records),
        "opportunities_added": added,
        "opportunities_updated": updated,
        "federal_opportunities_added": federal_added,
        "federal_opportunities_updated": federal_updated,
        "federal_grants_added": grant_added,
        "federal_grants_updated": grant_updated,
        "dry_run": config.dry_run,
        "sources": source_summaries,
    }


def fetch_sam(config: SearchConfig) -> list[dict[str, str]]:
    if sam_live_enabled(config) and not config.sam_api_key:
        raise RuntimeError("SAM_API_KEY not configured for live SAM mode")

    start, end = date_window(config, "sam")
    endpoint = SOURCE_META["sam"]["url"]
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    ptype_counts: Counter[str] = Counter()
    vendor_aliases = vendor_aliases_by_key(config.data_dir / "search_parameters.json")
    keywords = source_keywords(config, "sam")
    guard = sam_quota_guard(config)

    ptypes = normalize_sam_ptypes(config.sam_ptypes)
    if sam_live_enabled(config):
        ptypes = [ptype for ptype in ptypes if ptype in SAM_APPROVED_LIVE_PTYPES]
    for ptype in ptypes:
        if ptype_counts[ptype] >= config.max_per_source:
            continue
        chunk_start, chunk_end = sam_call_window(config, ptype, start, end)
        params = {
            "api_key": config.sam_api_key,
            "limit": str(min(max(config.max_per_source * 10, config.max_per_source, 1), 1000)),
            "postedFrom": fmt_sam_date(chunk_start),
            "postedTo": fmt_sam_date(chunk_end),
            "ptype": ptype,
        }
        try:
            data = sam_http_json(endpoint, params, config, guard, caller="sam_opportunities")
        except SAMLiveCallBlocked:
            continue
        items = data.get("opportunitiesData", []) if isinstance(data, dict) else []
        for item in items or []:
            if not isinstance(item, dict) or not sam_local_match(item, keywords, vendor_aliases):
                continue
            record_id = str(item.get("noticeId") or item.get("solicitationNumber") or "")
            if not record_id or record_id in seen or ptype_counts[ptype] >= config.max_per_source:
                continue
            seen.add(record_id)
            rows.append(make_sam_opportunity(item, ptype, endpoint, vendor_aliases, config.keywords or DEFAULT_KEYWORDS))
            ptype_counts[ptype] += 1
        if all(ptype_counts[item] >= config.max_per_source for item in ptypes):
            return rows
    return rows


def sam_live_enabled(config: SearchConfig) -> bool:
    return str(config.sam_quota_mode).strip().lower() == "live" and config.sam_live_budget > 0


def sam_quota_guard(config: SearchConfig) -> SAMQuotaGuard:
    policy = policy_from_settings(config.sam_quota_mode, config.sam_live_budget, config.sam_ledger_path)
    cache = RawSAMCache(root=config.sam_cache_dir, ledger_path=config.sam_ledger_path)
    return SAMQuotaGuard(policy=policy, cache=cache)


def sam_call_window(config: SearchConfig, ptype: str, start: dt.date, end: dt.date) -> tuple[dt.date, dt.date]:
    if not sam_live_enabled(config):
        return start, end
    max_days = 90 if ptype in {"o", "k"} else 180
    return max(start, end - dt.timedelta(days=max_days)), end


def sam_local_match(item: dict[str, Any], keywords: list[str], vendor_aliases: dict[str, list[str]]) -> bool:
    text = sam_opportunity_text(item)
    hits = keyword_hits(text, keywords)
    if mentioned_vendor_keys(text, vendor_aliases):
        return True
    lower = text.lower()
    agency_match = any(term in lower for term in SAM_TARGET_AGENCY_TERMS)
    high_value_match = any(term in lower for term in SAM_HIGH_VALUE_TERMS)
    return bool(hits and (agency_match or high_value_match))


def sam_opportunity_text(item: dict[str, Any]) -> str:
    return " ".join(
        str(value or "")
        for value in [
            item.get("title"),
            item.get("type"),
            item.get("baseType"),
            item.get("fullParentPathName"),
            item.get("department"),
            item.get("subTier"),
            item.get("subAgency"),
            item.get("office"),
            item.get("officeName"),
            item.get("description"),
            item.get("naicsCode"),
            item.get("classificationCode"),
        ]
    )


def sam_http_json(
    endpoint: str,
    params: dict[str, Any],
    config: SearchConfig,
    guard: SAMQuotaGuard,
    caller: str,
) -> Any:
    cached = guard.cache.get("GET", endpoint, params)
    if cached is not None:
        data = cached_response_json(cached)
        guard.log_cache_hit("GET", endpoint, params, record_count=sam_record_count(data), caller=caller)
        return data

    guard.require_live_call("GET", endpoint, params, caller=caller)
    full_url = endpoint + "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(full_url, headers={"Accept": "application/json", "User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            response_text = response.read().decode("utf-8")
            data = json.loads(response_text)
            guard.cache.put("GET", endpoint, params, None, response.status, response_text, dict(response.headers.items()))
            guard.log_live_result("GET", endpoint, params, status="live_ok", record_count=sam_record_count(data), caller=caller)
            return data
    except urllib.error.HTTPError as exc:
        body = redact_secret(sanitize_text(exc.read(600).decode("utf-8", "replace")), config.sam_api_key)
        if exc.code == 429:
            guard.log_live_result("GET", endpoint, params, status="rate_limited", caller=caller, note="SAM_API_KEY 429")
            raise SAMQuotaError("SAM_API_KEY 429") from exc
        guard.log_live_result("GET", endpoint, params, status="http_error", caller=caller, note=f"HTTP {exc.code}")
        raise RuntimeError(f"HTTP {exc.code} from {sanitize_url(full_url)}: {body}") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        message = redact_secret(sanitize_text(str(exc)), config.sam_api_key)
        guard.log_live_result("GET", endpoint, params, status="live_error", caller=caller, note=message)
        raise RuntimeError(f"request failed: {message}") from exc


def cached_response_json(cached: dict[str, Any]) -> Any:
    return json.loads(str(cached.get("response_text") or "{}"))


def sam_record_count(data: Any) -> int:
    if not isinstance(data, dict):
        return 0
    for key in ("opportunitiesData", "awardSummary", "entityData"):
        value = data.get(key)
        if isinstance(value, list):
            return len(value)
    total = data.get("totalRecords")
    return int_or_zero(total)


def normalize_sam_ptypes(ptypes: list[str] | None) -> list[str]:
    raw = ptypes or DEFAULT_SAM_PTYPES
    normalized: list[str] = []
    for value in raw:
        ptype = str(value).strip().lower()
        if ptype == "eval":
            normalized.extend(SAM_PTYPE_EVALUATION_SET)
            continue
        if ptype in SAM_PTYPE_LABELS and ptype != "g":
            normalized.append(ptype)
    return sorted(set(normalized), key=(SAM_PTYPE_EVALUATION_SET + ["g"]).index) or DEFAULT_SAM_PTYPES


def make_sam_opportunity(
    item: dict[str, Any],
    ptype: str,
    endpoint: str,
    vendor_aliases: dict[str, list[str]],
    keywords: list[str],
) -> dict[str, str]:
    record_id = str(item.get("noticeId") or item.get("solicitationNumber") or "").strip()
    solicitation_number = str(item.get("solicitationNumber") or "").strip()
    notice_type = str(item.get("type") or item.get("baseType") or SAM_PTYPE_LABELS.get(ptype, "Opportunity"))
    agency = str(item.get("fullParentPathName") or item.get("department") or "Federal agency")
    subagency = text_value(item.get("subTier") or item.get("subAgency") or item.get("subTierName"))
    office = text_value(item.get("office") or item.get("officeName"))
    url = str(item.get("uiLink") or (f"https://sam.gov/opp/{record_id}/view" if record_id else ""))
    summary = str(item.get("description") or solicitation_number or "SAM.gov opportunity metadata.")
    due_date = item.get("responseDeadLine") or item.get("responseDeadline") or ""
    amount = award_amount(item.get("award"))
    text = " ".join(
        str(value)
        for value in [
            item.get("title"),
            notice_type,
            agency,
            subagency,
            office,
            summary,
            item.get("naicsCode"),
            item.get("classificationCode"),
        ]
    )
    hits = keyword_hits(text, keywords)
    bucket = SAM_NOTICE_BUCKETS.get(ptype, "other")
    vendor_keys = mentioned_vendor_keys(text, vendor_aliases)
    score = score_opportunity("sam", notice_type, hits, due_date, amount)
    last_checked_at = now_iso()

    legacy = make_opportunity(
        source_key="sam",
        source_record_id=record_id,
        title=item.get("title") or "Untitled SAM.gov opportunity",
        agency=agency,
        document_type=notice_type,
        document_url=url,
        source_url=endpoint,
        posted_date=item.get("postedDate") or "",
        due_date=due_date,
        summary=summary,
        budget_estimate=amount,
        text_for_score=text,
    )
    legacy.update(
        {
            "opportunity_id": stable_id("sam_opportunities", record_id),
            "source_key": "sam_opportunities",
            "sam_notice_id": record_id,
            "solicitation_number": solicitation_number,
            "notice_type": clean_text(notice_type, 80),
            "ptype": ptype,
            "notice_bucket": bucket,
            "record_type": sam_record_type(ptype),
            "subagency": clean_text(subagency, 180),
            "office": clean_text(office, 180),
            "updated_date": iso_date(item.get("modifiedDate") or item.get("updatedDate") or ""),
            "archive_date": iso_date(item.get("archiveDate") or ""),
            "naics": text_value(item.get("naicsCode") or item.get("naics")),
            "psc": text_value(item.get("classificationCode") or item.get("psc") or item.get("productServiceCode")),
            "set_aside": text_value(item.get("typeOfSetAsideDescription") or item.get("typeOfSetAside")),
            "place_of_performance_state": place_of_performance_state(item.get("placeOfPerformance")),
            "topic_keys": ";".join(normalize_key(hit) for hit in hits),
            "vendor_keys_mentioned": ";".join(vendor_keys),
            "lifecycle_status": sam_lifecycle_status(ptype, due_date),
            "importance_score": str(score),
            "score_evidence_json": json_compact(
                {
                    "ptype": ptype,
                    "notice_bucket": bucket,
                    "keyword_hits": hits,
                    "vendor_keys_mentioned": vendor_keys,
                    "due_date": iso_date(due_date),
                    "agency": agency,
                    "award_amount": amount,
                }
            ),
            "raw_json": json_compact(item),
            "last_checked_at": last_checked_at,
        }
    )
    legacy["federal_program_focus"] = ";".join(federal_program_focus(hits))
    return legacy


def fetch_grants(config: SearchConfig) -> list[dict[str, str]]:
    endpoint = SOURCE_META["grants"]["url"]
    statuses = "forecasted|posted" if config.mode == "continue" else "forecasted|posted|closed|archived"
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    detail_errors = 0

    for keyword in source_keywords(config, "grants"):
        payload = {
            "keyword": keyword,
            "oppStatuses": statuses,
            "rows": min(max(config.max_per_source * 2, 25), 1000),
            "startRecordNum": 0,
        }
        if config.grants_agencies:
            payload["agencies"] = config.grants_agencies
        data = http_json(endpoint, payload=payload)
        for item in data.get("data", {}).get("oppHits", []) or []:
            record_id = str(item.get("id") or item.get("number") or "").strip()
            if not record_id or record_id in seen:
                continue
            seen.add(record_id)
            try:
                detail = fetch_grant_detail(record_id)
            except Exception:
                detail_errors += 1
                continue
            rows.append(make_grant_opportunity(item, detail, endpoint, config.keywords or DEFAULT_KEYWORDS))
            if len(rows) >= config.max_per_source:
                return rows
    if seen and not rows and detail_errors:
        raise RuntimeError(f"fetchOpportunity failed for {detail_errors} Grants.gov search result(s)")
    return rows


def fetch_grant_detail(opportunity_id: str) -> dict[str, Any]:
    data = http_json(GRANTS_FETCH_ENDPOINT, payload={"opportunityId": opportunity_id})
    detail = data.get("data") if isinstance(data, dict) else None
    if not isinstance(detail, dict) or not detail.get("id"):
        message = detail.get("message") if isinstance(detail, dict) else "missing data"
        raise RuntimeError(f"fetchOpportunity returned no detail for {opportunity_id}: {sanitize_text(str(message))}")
    return detail


def make_grant_opportunity(
    search_item: dict[str, Any],
    detail: dict[str, Any],
    endpoint: str,
    keywords: list[str],
) -> dict[str, str]:
    synopsis = detail.get("synopsis") if isinstance(detail.get("synopsis"), dict) else {}
    forecast = detail.get("forecast") if isinstance(detail.get("forecast"), dict) else {}
    section = synopsis or forecast
    agency_details = first_dict(detail.get("agencyDetails"), section.get("agencyDetails"), detail.get("topAgencyDetails"))
    top_agency_details = first_dict(detail.get("topAgencyDetails"), section.get("topAgencyDetails"))
    raw_id = str(detail.get("id") or search_item.get("id") or "").strip()
    opportunity_number = str(detail.get("opportunityNumber") or search_item.get("number") or "").strip()
    title = str(detail.get("opportunityTitle") or search_item.get("title") or "Untitled grant opportunity")
    agency_code = str(section.get("agencyCode") or detail.get("owningAgencyCode") or search_item.get("agencyCode") or "")
    agency = str(section.get("agencyName") or agency_details.get("agencyName") or search_item.get("agency") or agency_code or "Federal grant agency")
    posted_date = grants_date(section.get("postingDateStr"), section.get("postingDate"), search_item.get("openDate"))
    close_date = grants_date(
        section.get("responseDateStr"),
        section.get("responseDate"),
        forecast.get("estApplicationResponseDateStr"),
        forecast.get("estApplicationResponseDate"),
        detail.get("originalDueDate"),
        search_item.get("closeDate"),
    )
    archive_date = grants_date(section.get("archiveDateStr"), section.get("archiveDate"))
    award_ceiling = grant_amount(section.get("awardCeiling"))
    award_floor = grant_amount(section.get("awardFloor"))
    estimated_funding = grant_amount(section.get("estimatedFunding"))
    expected_awards = text_value(section.get("numberOfAwards"))
    eligibility = grant_eligibility(section)
    funding_category = grant_descriptions(section.get("fundingActivityCategories"))
    assistance_listing = grant_assistance_listing(detail, search_item)
    summary = clean_text(strip_html(section.get("synopsisDesc") or section.get("forecastDesc") or ""), 900)
    text = " ".join(
        [
            title,
            agency,
            agency_code,
            top_agency_details.get("agencyName", ""),
            summary,
            eligibility,
            funding_category,
            assistance_listing,
            opportunity_number,
        ]
    )
    grant_keywords = sorted({*keywords, "HHS", "CMS", "HRSA", "critical access hospital"}, key=str.lower)
    hits = keyword_hits(text, grant_keywords)
    rht_flag = is_rht_grant(text)
    focus = federal_program_focus(hits)
    if rht_flag and "rht" not in focus:
        focus.append("rht")
    topic_keys = {normalize_key(hit) for hit in hits}
    if "HHS-HRSA" in agency_code:
        topic_keys.add("hrsa")
    if "HHS-CMS" in agency_code:
        topic_keys.add("cms")
    if rht_flag:
        topic_keys.add("rht")
    predictive_value = grant_predictive_value(estimated_funding, award_ceiling, expected_awards)
    score = score_grant(hits, agency_code, rht_flag, close_date, predictive_value, eligibility)
    document_url = f"https://www.grants.gov/search-results-detail/{raw_id}" if raw_id else "https://www.grants.gov/search-grants"
    evidence = {
        "agency_code": agency_code,
        "top_agency_code": top_agency_details.get("agencyCode") or top_agency_details.get("topAgencyCode") or "",
        "keyword_hits": hits,
        "rht_flag": rht_flag,
        "close_date": close_date,
        "award_ceiling": award_ceiling,
        "award_floor": award_floor,
        "estimated_total_program_funding": estimated_funding,
        "expected_awards": expected_awards,
        "funding_category": funding_category,
        "assistance_listing_number": assistance_listing,
        "detail_fetched": True,
    }
    last_checked_at = now_iso()

    row = make_opportunity(
        source_key="grants",
        source_record_id=raw_id or opportunity_number or title,
        title=title,
        agency=agency,
        document_type=search_item.get("oppStatus") or detail.get("docType") or "Grant opportunity",
        document_url=document_url,
        source_url=endpoint,
        posted_date=posted_date,
        due_date=close_date,
        summary=summary or f"Assistance listings: {assistance_listing}",
        budget_estimate=predictive_value or estimated_funding or award_ceiling or "0",
        text_for_score=text,
    )
    row.update(
        {
            "grant_id": stable_id("grants", raw_id or opportunity_number or title),
            "opportunity_number": clean_text(opportunity_number, 80),
            "opportunity_title": clean_text(title, 260),
            "agency": clean_text(agency, 180),
            "agency_code": clean_text(agency_code, 60),
            "posted_date": posted_date,
            "close_date": close_date,
            "archive_date": archive_date,
            "award_ceiling": award_ceiling,
            "award_floor": award_floor,
            "estimated_total_program_funding": estimated_funding,
            "expected_awards": clean_text(expected_awards, 80),
            "eligibility": clean_text(eligibility, 1200),
            "funding_category": clean_text(funding_category, 240),
            "assistance_listing_number": clean_text(assistance_listing, 240),
            "program_focus": ";".join(focus),
            "topic_keys": ";".join(sorted(topic_keys)),
            "rht_flag": "true" if rht_flag else "false",
            "importance_score": str(score),
            "predictive_value_usd": predictive_value,
            "score_evidence_json": json_compact(evidence),
            "document_url": document_url,
            "raw_json": json_compact({"search": search_item, "detail": detail}),
            "last_checked_at": last_checked_at,
            "source_key": "grants",
        }
    )
    return row


def fetch_federal_register(config: SearchConfig) -> list[dict[str, str]]:
    start, end = date_window(config, "federal_register")
    endpoint = SOURCE_META["federal_register"]["url"]
    rows: list[dict[str, str]] = []
    seen: set[str] = set()

    for keyword in source_keywords(config, "federal_register"):
        params = [
            ("per_page", min(config.max_per_source, 1000)),
            ("order", "newest"),
            ("conditions[term]", keyword),
            ("conditions[agencies][]", "centers-for-medicare-medicaid-services"),
            ("conditions[publication_date][gte]", start.isoformat()),
            ("conditions[publication_date][lte]", end.isoformat()),
        ]
        data = http_json(endpoint + "?" + urllib.parse.urlencode(params))
        for item in data.get("results", []) or []:
            record_id = str(item.get("document_number") or item.get("html_url") or "")
            if not record_id or record_id in seen:
                continue
            seen.add(record_id)
            agencies = ", ".join(a.get("name", "") for a in item.get("agencies", []) if isinstance(a, dict))
            summary = item.get("abstract") or item.get("excerpts") or ""
            text = " ".join([item.get("title") or "", agencies, summary, item.get("type") or ""])
            rows.append(
                make_opportunity(
                    source_key="federal_register",
                    source_record_id=record_id,
                    title=item.get("title") or "Untitled Federal Register document",
                    agency=agencies or "Centers for Medicare & Medicaid Services",
                    document_type=item.get("type") or "Policy document",
                    document_url=item.get("html_url") or "",
                    source_url=endpoint,
                    posted_date=item.get("publication_date") or "",
                    due_date=item.get("comments_close_on") or "",
                    summary=summary,
                    budget_estimate="0",
                    text_for_score=text,
                )
            )
            if len(rows) >= config.max_per_source:
                return rows
    return rows


def fetch_medicaid_catalog(config: SearchConfig) -> list[dict[str, str]]:
    return fetch_catalog(config, "medicaid", date_field="modified")


def fetch_cms_provider_catalog(config: SearchConfig) -> list[dict[str, str]]:
    return fetch_catalog(config, "cms_provider", date_field="released")


def fetch_catalog(config: SearchConfig, source_key: str, date_field: str) -> list[dict[str, str]]:
    start, end = date_window(config, source_key)
    endpoint = SOURCE_META[source_key]["url"]
    data = http_json(endpoint)
    rows: list[dict[str, str]] = []

    for item in data if isinstance(data, list) else []:
        item_date = parse_date(item.get(date_field) or item.get("modified") or item.get("issued"))
        if item_date and not (start <= item_date <= end):
            continue
        keywords = "; ".join(str(value) for value in item.get("keyword") or [])
        theme = "; ".join(str(value) for value in item.get("theme") or [])
        text = " ".join([item.get("title") or "", item.get("description") or "", keywords, theme])
        if not keyword_hits(text, source_keywords(config, source_key)):
            continue
        record_id = str(item.get("identifier") or item.get("%Ref:ds.identifier") or "")
        rows.append(
            make_opportunity(
                source_key=source_key,
                source_record_id=record_id,
                title=item.get("title") or "Untitled dataset",
                agency=publisher_name(item) or SOURCE_META[source_key]["name"],
                document_type="Dataset update",
                document_url=item.get("landingPage") or first_download_url(item),
                source_url=endpoint,
                posted_date=item.get(date_field) or item.get("modified") or item.get("issued") or "",
                due_date=item.get("nextUpdateDate") or "",
                summary=item.get("description") or keywords,
                budget_estimate="0",
                text_for_score=text,
            )
        )
    return sorted(rows, key=lambda row: (int(row["fit_score"]), row["posted_date"]), reverse=True)[: config.max_per_source]


def fetch_usaspending(config: SearchConfig) -> list[dict[str, str]]:
    vendors = config.vendors or []
    if not vendors:
        return []

    start, end = date_window(config, "usaspending")
    contracts = fetch_vendor_contracts(
        USASpendingConfig(
            vendors=[VendorSearch(name=vendor, queries=[vendor]) for vendor in vendors],
            keywords=config.keywords or DEFAULT_KEYWORDS,
            start_date=start,
            end_date=end,
            award_type_codes=["A", "B", "C", "D"],
            max_per_vendor=config.max_per_source,
            page_limit=min(config.max_per_source, 100),
        )
    )

    rows: list[dict[str, str]] = []
    for contract in contracts[: config.max_per_source]:
        doc_type = "Recompete Signal" if contract["recompete_signal"] in {"Expiring soon", "Recompete watch"} else "Contract Award"
        text = " ".join(
            [
                contract["description"],
                contract["awarding_agency"],
                contract["awarding_sub_agency"],
                contract["matched_keywords"],
            ]
        )
        rows.append(
            make_opportunity(
                source_key="usaspending",
                source_record_id=contract["generated_internal_id"] or contract["award_id"],
                title=f"{contract['recipient_name'] or contract['vendor_name']}: {contract['description'] or contract['award_id']}",
                agency=", ".join(
                    part for part in [contract["awarding_agency"], contract["awarding_sub_agency"]] if part
                ),
                document_type=doc_type,
                document_url=contract["source_url"],
                source_url=SOURCE_META["usaspending"]["url"],
                posted_date=contract["start_date"],
                due_date=contract["end_date"],
                summary=(
                    f"Recipient: {contract['recipient_name']} | Award ID: {contract['award_id']} | "
                    f"NAICS: {contract['naics_code']} | PSC: {contract['psc_code']} | "
                    f"Recompete: {contract['recompete_signal']} | {contract['description']}"
                ),
                budget_estimate=contract["award_amount"],
                text_for_score=text,
            )
        )
    return rows


SOURCE_FETCHERS = {
    "sam": fetch_sam,
    "grants": fetch_grants,
    "federal_register": fetch_federal_register,
    "medicaid": fetch_medicaid_catalog,
    "cms_provider": fetch_cms_provider_catalog,
    "usaspending": fetch_usaspending,
}


def source_record_summary(source: str, records: list[dict[str, str]]) -> dict[str, Any]:
    if source == "grants":
        agency_counts = Counter(row.get("agency_code") or row.get("agency", "") for row in records)
        return {
            "agency_counts": dict(sorted((key, count) for key, count in agency_counts.items() if key)),
            "rht_count": sum(1 for row in records if row.get("rht_flag") == "true"),
            "close_date_count": sum(1 for row in records if row.get("close_date") or row.get("due_date")),
            "dollar_field_count": sum(
                1
                for row in records
                if row.get("award_ceiling") or row.get("award_floor") or row.get("estimated_total_program_funding")
            ),
        }
    if source != "sam":
        return {}
    ptype_counts = Counter(row.get("ptype", "") for row in records if row.get("ptype"))
    bucket_counts = Counter(row.get("notice_bucket", "") for row in records if row.get("notice_bucket"))
    return {
        "ptype_counts": dict(sorted(ptype_counts.items())),
        "notice_bucket_counts": dict(sorted(bucket_counts.items())),
        "default_ptypes": ",".join(DEFAULT_SAM_PTYPES),
        "non_core_records": sum(count for ptype, count in ptype_counts.items() if ptype not in DEFAULT_SAM_PTYPES),
    }


def text_value(value: Any) -> str:
    if isinstance(value, dict):
        for key in ("name", "value", "code", "description", "title"):
            if value.get(key):
                return str(value[key])
        return json_compact(value)
    if isinstance(value, list):
        return ";".join(text_value(item) for item in value if text_value(item))
    return str(value or "")


def place_of_performance_state(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    state = value.get("state") or value.get("stateCode") or value.get("stateName")
    if isinstance(state, dict):
        return str(state.get("code") or state.get("name") or "")
    return str(state or "")


def sam_record_type(ptype: str) -> str:
    if ptype == "a":
        return "award"
    if SAM_NOTICE_BUCKETS.get(ptype) == "market_intel":
        return "intel_notice"
    return "opportunity"


def sam_lifecycle_status(ptype: str, due_date: Any) -> str:
    if ptype == "a":
        return "awarded"
    if SAM_NOTICE_BUCKETS.get(ptype) == "market_intel":
        return "unknown"
    due = parse_date(due_date)
    if due and due < dt.date.today():
        return "expired"
    if ptype in {"p", "r"}:
        return "upcoming"
    return "active"


def federal_program_focus(hits: list[str]) -> list[str]:
    hit_set = {hit.lower() for hit in hits}
    focus: list[str] = []
    if {"medicaid", "mmis", "managed care", "eligibility", "enrollment", "claims"} & hit_set:
        focus.append("medicaid")
    if {"medicare", "cms", "quality measures"} & hit_set:
        focus.append("medicare")
    if {"rural health", "rural health transformation", "critical access hospital", "telehealth", "workforce"} & hit_set:
        focus.append("rht")
    if {"interoperability", "fhir", "prior authorization", "provider data"} & hit_set:
        focus.append("interoperability")
    return focus or ["review"]


def vendor_aliases_by_key(path: Path) -> dict[str, list[str]]:
    params = load_search_parameters(path)
    aliases: dict[str, list[str]] = {}
    for vendor in vendor_searches(params):
        key = normalize_key(vendor.name)
        aliases[key] = sorted({vendor.name, *vendor.queries}, key=str.lower)
    return aliases


def mentioned_vendor_keys(text: str, aliases_by_key: dict[str, list[str]]) -> list[str]:
    lower = text.lower()
    keys = [key for key, aliases in aliases_by_key.items() if any(alias.lower() in lower for alias in aliases)]
    return sorted(set(keys))


def normalize_key(value: Any) -> str:
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", str(value or "").lower())).strip("_")


def json_compact(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def first_dict(*values: Any) -> dict[str, Any]:
    for value in values:
        if isinstance(value, dict):
            return value
    return {}


def grants_date(*values: Any) -> str:
    for value in values:
        if not value:
            continue
        text = str(value).strip()
        match = re.match(r"^(\d{4}-\d{2}-\d{2})", text)
        if match:
            return match.group(1)
        parsed = parse_date(text)
        if parsed:
            return parsed.isoformat()
        stripped = re.sub(r"\s+[A-Z]{2,4}$", "", text)
        for fmt in ("%b %d, %Y %I:%M:%S %p", "%B %d, %Y %I:%M:%S %p", "%b %d, %Y", "%B %d, %Y"):
            try:
                return dt.datetime.strptime(stripped, fmt).date().isoformat()
            except ValueError:
                pass
    return ""


def grant_amount(value: Any) -> str:
    text = str(value or "").strip()
    if not text or text.lower() in {"none", "n/a", "na", "not available", "unknown", "null"}:
        return ""
    amount = numeric_amount(text)
    return str(amount) if amount or re.search(r"\d", text) else ""


def grant_descriptions(value: Any) -> str:
    if isinstance(value, list):
        items = [text_value(item.get("description") or item.get("name") or item.get("id")) for item in value if isinstance(item, dict)]
        return "; ".join(item for item in items if item)
    return text_value(value)


def grant_assistance_listing(detail: dict[str, Any], search_item: dict[str, Any]) -> str:
    numbers = [str(item.get("cfdaNumber")) for item in detail.get("cfdas") or [] if isinstance(item, dict) and item.get("cfdaNumber")]
    numbers.extend(str(item) for item in search_item.get("cfdaList") or [] if item)
    return ";".join(sorted(set(numbers)))


def grant_eligibility(section: dict[str, Any]) -> str:
    applicant_types = grant_descriptions(section.get("applicantTypes"))
    eligibility_desc = strip_html(section.get("applicantEligibilityDesc") or "")
    return "; ".join(part for part in [applicant_types, eligibility_desc] if part)


def strip_html(value: Any) -> str:
    text = re.sub(r"<[^>]+>", " ", str(value or ""))
    text = text.replace("&nbsp;", " ").replace("&amp;", "&").replace("&quot;", '"')
    return clean_text(text, 5000)


def is_rht_grant(text: str) -> bool:
    lower = text.lower()
    return any(term in lower for term in GRANTS_RHT_TERMS)


def grant_predictive_value(estimated_funding: str, award_ceiling: str, expected_awards: str) -> str:
    estimated = numeric_amount(estimated_funding)
    if estimated:
        return str(estimated)
    ceiling = numeric_amount(award_ceiling)
    awards = numeric_amount(expected_awards)
    if ceiling and awards:
        return str(ceiling * awards)
    return str(ceiling) if ceiling else ""


def score_grant(hits: list[str], agency_code: str, rht_flag: bool, close_date: str, predictive_value: str, eligibility: str) -> int:
    score = 20
    for hit in hits:
        score += KEYWORD_WEIGHTS.get(hit.lower(), 2)
    if agency_code.startswith("HHS-CMS") or agency_code.startswith("HHS-HRSA"):
        score += 15
    elif agency_code.startswith("HHS"):
        score += 8
    if rht_flag:
        score += 15
    amount = numeric_amount(predictive_value)
    if amount >= 5_000_000:
        score += 10
    elif amount >= 1_000_000:
        score += 7
    elif amount >= 250_000:
        score += 4
    close = parse_date(close_date)
    if close:
        days = (close - dt.date.today()).days
        if 0 <= days <= 90:
            score += 8
        elif 91 <= days <= 240:
            score += 4
    if any(term in eligibility.lower() for term in ("small businesses", "for profit organizations")):
        score += 4
    return max(0, min(score, 100))


def make_opportunity(
    *,
    source_key: str,
    source_record_id: str,
    title: str,
    agency: str,
    document_type: str,
    document_url: str,
    source_url: str,
    posted_date: Any,
    due_date: Any,
    summary: str,
    budget_estimate: str,
    text_for_score: str,
) -> dict[str, str]:
    hits = keyword_hits(" ".join([title, agency, document_type, summary, text_for_score]), DEFAULT_KEYWORDS)
    focus = program_focus(hits, document_type)
    risks = risk_flags(due_date, budget_estimate, document_type)
    score = score_opportunity(source_key, document_type, hits, due_date, budget_estimate)
    recommendation = "Pursue" if score >= 80 else "Monitor" if score >= 55 else "Review"

    return {
        "id": stable_id(source_key, source_record_id or title),
        "title": clean_text(title, 260),
        "state": "Federal",
        "agency": clean_text(agency, 180),
        "source": SOURCE_META[source_key]["name"],
        "source_url": source_url,
        "document_url": document_url,
        "document_type": clean_text(document_type, 80),
        "due_date": iso_date(due_date),
        "posted_date": iso_date(posted_date),
        "last_checked_at": now_iso(),
        "last_updated_at": now_iso(),
        "budget_estimate": str(numeric_amount(budget_estimate)),
        "eligibility": "Review Needed",
        "eligibility_reason": eligibility_reason(source_key, document_type),
        "fit_score": str(score),
        "ai_recommendation": recommendation,
        "status": "Unreviewed",
        "program_focus": ";".join(focus),
        "keywords_matched": ";".join(hits),
        "risks": ";".join(risks),
        "summary": clean_text(summary, 700),
    }


def score_opportunity(source_key: str, document_type: str, hits: list[str], due_date: Any, budget_estimate: str) -> int:
    text_type = document_type.lower()
    score = 10
    source_bonus = {
        "sam": 25,
        "usaspending": 18,
        "grants": 15,
        "federal_register": 10,
        "medicaid": 8,
        "cms_provider": 8,
    }.get(source_key, 0)
    score += source_bonus

    for hit in hits:
        score += KEYWORD_WEIGHTS.get(hit.lower(), 2)

    if any(term in text_type for term in ("solicitation", "sources sought", "rfp", "rfi")):
        score += 12
    if "recompete" in text_type:
        score += 18

    amount = numeric_amount(budget_estimate)
    if amount >= 1_000_000:
        score += 8
    elif amount >= 250_000:
        score += 4

    due = parse_date(due_date)
    if due:
        days = (due - dt.date.today()).days
        if 0 <= days <= 45:
            score += 8
        elif 46 <= days <= 180:
            score += 4

    return max(0, min(score, 100))


def program_focus(hits: list[str], document_type: str) -> list[str]:
    hit_set = {hit.lower() for hit in hits}
    focus = []
    if {"rfp", "rfi", "solicitation", "sources sought"} & hit_set or "solicitation" in document_type.lower():
        focus.append("Procurement")
    if {"medicaid", "mmis", "managed care", "eligibility", "enrollment", "claims"} & hit_set:
        focus.append("Medicaid")
    if {"medicare", "cms", "provider data", "quality measures"} & hit_set:
        focus.append("CMS/Medicare")
    if {"rural health", "rural health transformation", "telehealth", "workforce"} & hit_set:
        focus.append("Rural Health")
    if {"interoperability", "fhir", "prior authorization"} & hit_set:
        focus.append("Interoperability")
    return focus or ["Review"]


def risk_flags(due_date: Any, budget_estimate: str, document_type: str) -> list[str]:
    risks = []
    due = parse_date(due_date)
    if due:
        days = (due - dt.date.today()).days
        if 0 <= days <= 21:
            risks.append("Short response window")
        elif days < 0 and "contract" not in document_type.lower():
            risks.append("Past deadline")
    if numeric_amount(budget_estimate) == 0:
        risks.append("Budget not explicitly stated")
    if "Dataset" in document_type:
        risks.append("Informational signal, not a direct procurement")
    if "Policy" in document_type or "Rule" in document_type or "Notice" in document_type:
        risks.append("Policy signal, not a direct procurement")
    return risks


def eligibility_reason(source_key: str, document_type: str) -> str:
    if source_key == "sam":
        return "Federal opportunity; review notice and set-aside details for vendor eligibility."
    if source_key == "grants":
        return "Grant opportunity; vendor may need prime eligibility or a partner applicant."
    if source_key == "usaspending":
        return "Award history signal; use incumbent and end date to prepare for recompete."
    return "Public update; review source document for vendor relevance."


def date_window(config: SearchConfig, source: str) -> tuple[dt.date, dt.date]:
    end = config.end_date or dt.date.today()
    if config.mode == "historic":
        start = config.start_date or (end - dt.timedelta(days=365 * 20))
        return start, end

    last_run = last_successful_run(config.data_dir, source)
    if last_run:
        start = last_run.date() - dt.timedelta(days=config.overlap_days)
    else:
        start = end - dt.timedelta(days=config.days_back)
    return start, end


def yearly_chunks(start: dt.date, end: dt.date) -> list[tuple[dt.date, dt.date]]:
    chunks = []
    cursor = start
    while cursor <= end:
        chunk_end = min(end, cursor + dt.timedelta(days=364))
        chunks.append((cursor, chunk_end))
        cursor = chunk_end + dt.timedelta(days=1)
    return chunks


def source_keywords(config: SearchConfig, source: str) -> list[str]:
    keywords = config.keywords or DEFAULT_KEYWORDS
    if source == "sam":
        keep = {
            "medicaid",
            "medicare",
            "cms",
            "mmis",
            "claims",
            "eligibility",
            "enrollment",
            "managed care",
            "interoperability",
            "fhir",
            "prior authorization",
            "provider data",
            "quality measures",
            "rural health",
            "rural health transformation",
            "critical access hospital",
            "telehealth",
            "behavioral health",
            "workforce",
        }
    elif source == "grants":
        keep = {
            "medicaid",
            "medicare",
            "cms",
            "hhs",
            "hrsa",
            "rural health",
            "rural health transformation",
            "critical access hospital",
            "telehealth",
            "behavioral health",
            "workforce",
        }
    elif source == "federal_register":
        keep = {"medicaid", "medicare", "cms", "rural health", "managed care", "waiver", "1115", "interoperability", "prior authorization", "provider data"}
    elif source in {"medicaid", "cms_provider"}:
        keep = {"medicaid", "medicare", "cms", "rural health", "managed care", "eligibility", "enrollment", "claims", "provider data", "quality measures", "telehealth"}
    else:
        keep = {keyword.lower() for keyword in keywords}
    filtered = [keyword for keyword in keywords if keyword.lower() in keep]
    return filtered or keywords


def upsert_opportunities(data_dir: Path, new_rows: list[dict[str, str]]) -> tuple[int, int]:
    path = data_dir / "opportunities.csv"
    existing_rows = read_csv(path)
    by_id = {row.get("id", ""): row for row in existing_rows if row.get("id")}
    added = 0
    updated = 0

    for new_row in new_rows:
        row_id = new_row["id"]
        existing = by_id.get(row_id)
        if existing is None:
            by_id[row_id] = {field: new_row.get(field, "") for field in OPPORTUNITY_FIELDS}
            added += 1
            continue

        merged = dict(existing)
        preserved_status = existing.get("status") or new_row.get("status", "Unreviewed")
        preserved_recommendation = existing.get("ai_recommendation") or new_row.get("ai_recommendation", "Review")
        changed = False
        for field in OPPORTUNITY_FIELDS:
            if field == "status":
                continue
            value = new_row.get(field, existing.get(field, ""))
            if field == "ai_recommendation" and existing.get("status") in {"Pursue", "Monitor", "Decline"}:
                value = preserved_recommendation
            if merged.get(field, "") != value:
                merged[field] = value
                changed = True
        merged["status"] = preserved_status
        merged["last_checked_at"] = now_iso()
        if changed:
            merged["last_updated_at"] = now_iso()
            updated += 1
        by_id[row_id] = merged

    rows = sorted(by_id.values(), key=lambda row: (int_or_zero(row.get("fit_score")), row.get("posted_date", "")), reverse=True)
    write_csv(path, OPPORTUNITY_FIELDS, rows)
    return added, updated


def upsert_federal_opportunities(data_dir: Path, new_rows: list[dict[str, str]]) -> tuple[int, int]:
    path = data_dir / "federal_opportunities.csv"
    federal_rows = [row for row in new_rows if row.get("source_key") == "sam_opportunities"]
    if not federal_rows:
        return 0, 0

    existing_rows = read_csv(path)
    by_id = {row.get("opportunity_id", ""): row for row in existing_rows if row.get("opportunity_id")}
    added = 0
    updated = 0

    for row in federal_rows:
        opportunity_id = row.get("opportunity_id", "")
        if not opportunity_id:
            continue
        clean_row = {field: row.get(field, "") for field in FEDERAL_OPPORTUNITY_FIELDS}
        clean_row["program_focus"] = row.get("federal_program_focus") or row.get("program_focus", "")
        existing = by_id.get(opportunity_id)
        if existing is None:
            by_id[opportunity_id] = clean_row
            added += 1
            continue
        if any(existing.get(field, "") != clean_row.get(field, "") for field in FEDERAL_OPPORTUNITY_FIELDS):
            by_id[opportunity_id] = clean_row
            updated += 1

    rows = sorted(
        by_id.values(),
        key=lambda row: (
            row.get("notice_bucket") == "active_opportunity",
            row.get("lifecycle_status") == "active",
            int_or_zero(row.get("importance_score")),
            row.get("due_date", ""),
            row.get("posted_date", ""),
        ),
        reverse=True,
    )
    write_csv(path, FEDERAL_OPPORTUNITY_FIELDS, rows)
    return added, updated


def upsert_federal_grants(data_dir: Path, new_rows: list[dict[str, str]]) -> tuple[int, int]:
    path = data_dir / "federal_grants.csv"
    grant_rows = [row for row in new_rows if row.get("grant_id")]
    if not grant_rows:
        return 0, 0

    existing_rows = read_csv(path)
    by_id = {row.get("grant_id", ""): row for row in existing_rows if row.get("grant_id")}
    added = 0
    updated = 0

    for row in grant_rows:
        grant_id = row.get("grant_id", "")
        if not grant_id:
            continue
        clean_row = {field: row.get(field, "") for field in FEDERAL_GRANT_FIELDS}
        existing = by_id.get(grant_id)
        if existing is None:
            by_id[grant_id] = clean_row
            added += 1
            continue
        if any(existing.get(field, "") != clean_row.get(field, "") for field in FEDERAL_GRANT_FIELDS):
            by_id[grant_id] = clean_row
            updated += 1

    rows = sorted(
        by_id.values(),
        key=lambda row: (
            row.get("rht_flag") == "true",
            int_or_zero(row.get("importance_score")),
            row.get("close_date", ""),
            row.get("posted_date", ""),
        ),
        reverse=True,
    )
    write_csv(path, FEDERAL_GRANT_FIELDS, rows)
    return added, updated


def upsert_source_status(data_dir: Path, source_key: str, summary: dict[str, Any]) -> None:
    path = data_dir / "sources.csv"
    rows = read_csv(path)
    meta = SOURCE_META.get(source_key)
    if not meta:
        return
    source_id = meta["id"]
    by_id = {row.get("id", ""): row for row in rows if row.get("id")}
    by_id[source_id] = {
        "id": source_id,
        "state": "Federal",
        "name": meta["name"],
        "type": meta["type"],
        "url": meta["url"],
        "last_checked_at": now_iso(),
        "status": "Healthy" if summary.get("status") == "ok" else "Needs review",
        "opportunities_found": str(summary.get("records_found", 0)),
    }
    write_csv(path, SOURCE_FIELDS, list(by_id.values()))


def upsert_source_run(data_dir: Path, row: dict[str, str]) -> None:
    path = data_dir / "source_runs.csv"
    rows = read_csv(path)
    key = (row.get("source"), row.get("mode"))
    replaced = False
    for index, existing in enumerate(rows):
        if (existing.get("source"), existing.get("mode")) == key:
            if not row.get("last_successful_run"):
                row["last_successful_run"] = existing.get("last_successful_run", "")
            rows[index] = row
            replaced = True
            break
    if not replaced:
        rows.append(row)
    write_csv(path, SOURCE_RUN_FIELDS, rows)


def stamp_source_run_counts(data_dir: Path, sources: list[str], added: int, updated: int) -> None:
    path = data_dir / "source_runs.csv"
    rows = read_csv(path)
    source_set = set(sources)
    for row in rows:
        if row.get("source") in source_set:
            row["records_added"] = str(added)
            row["records_updated"] = str(updated)
    write_csv(path, SOURCE_RUN_FIELDS, rows)


def last_successful_run(data_dir: Path, source: str) -> dt.datetime | None:
    path = data_dir / "source_runs.csv"
    rows = read_csv(path)
    latest: dt.datetime | None = None
    for row in rows:
        if row.get("source") != source or row.get("status") != "ok":
            continue
        parsed = parse_datetime(row.get("last_successful_run"))
        if parsed and (latest is None or parsed > latest):
            latest = parsed
    return latest


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


def http_json(url: str, payload: dict[str, Any] | None = None, timeout: int = 45) -> Any:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
    if payload is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers)

    last_error: Exception | None = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = sanitize_text(exc.read(600).decode("utf-8", "replace"))
            raise RuntimeError(f"HTTP {exc.code} from {sanitize_url(url)}: {body}") from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = exc
            time.sleep(1 + attempt)
    raise RuntimeError(f"request failed: {last_error}")


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


def parse_datetime(value: Any) -> dt.datetime | None:
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        parsed_date = parse_date(text)
        if not parsed_date:
            return None
        parsed = dt.datetime.combine(parsed_date, dt.time.min)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


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


def fmt_sam_date(value: dt.date) -> str:
    return value.strftime("%m/%d/%Y")


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def clean_text(value: Any, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "..."


def keyword_hits(text: str, keywords: list[str]) -> list[str]:
    lower = text.lower()
    hits = {keyword for keyword in keywords if keyword and keyword.lower() in lower}
    return sorted(hits, key=str.lower)


def stable_id(source_key: str, source_record_id: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_-]+", "-", source_record_id.strip())[:100].strip("-")
    return f"{source_key}-{cleaned or 'record'}"


def int_or_zero(value: Any) -> int:
    try:
        return int(float(value or 0))
    except ValueError:
        return 0


def numeric_amount(value: Any) -> int:
    if isinstance(value, (int, float)):
        return int(float(value))
    cleaned = re.sub(r"[^0-9.-]", "", str(value or ""))
    try:
        return int(float(cleaned or 0))
    except ValueError:
        return 0


def award_amount(award: Any) -> str:
    if isinstance(award, dict):
        return str(award.get("amount") or award.get("awardAmount") or 0)
    return "0"


def first_download_url(row: dict[str, Any]) -> str:
    for distribution in row.get("distribution") or []:
        if isinstance(distribution, dict):
            return distribution.get("downloadURL") or distribution.get("accessURL") or ""
    return ""


def publisher_name(row: dict[str, Any]) -> str:
    publisher = row.get("publisher")
    if isinstance(publisher, dict):
        return str(publisher.get("name") or "")
    return str(publisher or "")


def is_recompete_window(end_date: Any) -> bool:
    parsed = parse_date(end_date)
    if not parsed:
        return False
    days = (parsed - dt.date.today()).days
    return 0 <= days <= 36 * 30


def sanitize_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    redacted = [(key, "REDACTED") if key.lower() in {"api_key", "apikey"} else (key, value) for key, value in query]
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urllib.parse.urlencode(redacted), parsed.fragment))


def redact_secret(text: str, secret: str) -> str:
    return text.replace(secret, "REDACTED") if secret else text


def sanitize_text(text: str) -> str:
    text = re.sub(r"(?i)(api[_-]?key=)[^&\s\"']+", r"\1REDACTED", text)
    text = re.sub(r"(?i)(SAM_API_KEY\s*[=:]\s*)[^&\s\"']+", r"\1REDACTED", text)
    return text


def emit(progress: Callable[[str], None] | None, message: str) -> None:
    if progress:
        progress(message)
