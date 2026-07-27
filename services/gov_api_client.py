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
        added = updated = 0
    else:
        added, updated = upsert_opportunities(config.data_dir, all_records)
        stamp_source_run_counts(config.data_dir, sources, added, updated)

    finished_at = now_iso()
    status = "ok" if all(summary["status"] in {"ok", "skipped"} for summary in source_summaries) else "partial"
    message = f"Gov search complete: {len(all_records)} found, {added} added, {updated} updated."
    return {
        "status": status,
        "message": message,
        "mode": config.mode,
        "started_at": started_at,
        "finished_at": finished_at,
        "records_found": len(all_records),
        "opportunities_added": added,
        "opportunities_updated": updated,
        "dry_run": config.dry_run,
        "sources": source_summaries,
    }


def fetch_sam(config: SearchConfig) -> list[dict[str, str]]:
    if not config.sam_api_key:
        return []

    start, end = date_window(config, "sam")
    endpoint = SOURCE_META["sam"]["url"]
    rows: list[dict[str, str]] = []
    seen: set[str] = set()

    ptypes = config.sam_ptypes or [""]
    for chunk_start, chunk_end in yearly_chunks(start, end):
        for keyword in source_keywords(config, "sam"):
            for ptype in ptypes:
                params = {
                    "api_key": config.sam_api_key,
                    "limit": str(min(config.max_per_source, 1000)),
                    "postedFrom": fmt_sam_date(chunk_start),
                    "postedTo": fmt_sam_date(chunk_end),
                    "title": keyword,
                }
                if ptype:
                    params["ptype"] = ptype
                data = http_json(endpoint + "?" + urllib.parse.urlencode(params))
                for item in data.get("opportunitiesData", []) or []:
                    record_id = str(item.get("noticeId") or item.get("solicitationNumber") or "")
                    if not record_id or record_id in seen:
                        continue
                    seen.add(record_id)
                    text = " ".join(str(item.get(key, "")) for key in ("title", "type", "baseType", "fullParentPathName"))
                    agency = item.get("fullParentPathName") or item.get("department") or "Federal agency"
                    url = item.get("uiLink") or (f"https://sam.gov/opp/{record_id}/view" if record_id else "")
                    rows.append(
                        make_opportunity(
                            source_key="sam",
                            source_record_id=record_id,
                            title=item.get("title") or "Untitled SAM.gov opportunity",
                            agency=agency,
                            document_type=item.get("type") or item.get("baseType") or SAM_PTYPE_LABELS.get(ptype, "Opportunity"),
                            document_url=url,
                            source_url=endpoint,
                            posted_date=item.get("postedDate") or "",
                            due_date=item.get("responseDeadLine") or "",
                            summary=item.get("description") or item.get("solicitationNumber") or "SAM.gov opportunity metadata.",
                            budget_estimate=award_amount(item.get("award")),
                            text_for_score=text,
                        )
                    )
                    if len(rows) >= config.max_per_source:
                        return rows
    return rows


def fetch_grants(config: SearchConfig) -> list[dict[str, str]]:
    endpoint = SOURCE_META["grants"]["url"]
    statuses = "forecasted|posted" if config.mode == "continue" else "forecasted|posted|closed|archived"
    rows: list[dict[str, str]] = []
    seen: set[str] = set()

    for keyword in source_keywords(config, "grants"):
        payload = {
            "keyword": keyword,
            "oppStatuses": statuses,
            "rows": min(max(config.max_per_source, 25), 1000),
            "startRecordNum": 0,
        }
        data = http_json(endpoint, payload=payload)
        for item in data.get("data", {}).get("oppHits", []) or []:
            record_id = str(item.get("id") or item.get("number") or "")
            if not record_id or record_id in seen:
                continue
            seen.add(record_id)
            text = " ".join(str(item.get(key, "")) for key in ("title", "agency", "agencyCode", "number"))
            rows.append(
                make_opportunity(
                    source_key="grants",
                    source_record_id=record_id,
                    title=item.get("title") or "Untitled grant opportunity",
                    agency=item.get("agency") or item.get("agencyCode") or "Federal grant agency",
                    document_type=item.get("oppStatus") or "Grant opportunity",
                    document_url=f"https://www.grants.gov/search-results-detail/{record_id}",
                    source_url=endpoint,
                    posted_date=item.get("openDate") or "",
                    due_date=item.get("closeDate") or "",
                    summary="CFDA: " + "; ".join(item.get("cfdaList") or []),
                    budget_estimate="0",
                    text_for_score=text,
                )
            )
            if len(rows) >= config.max_per_source:
                return rows
    return rows


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
        "budget_estimate": str(int(float(budget_estimate or 0))),
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

    amount = int(float(budget_estimate or 0))
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
    if int(float(budget_estimate or 0)) == 0:
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
        keep = {"medicaid", "medicare", "rural health", "mmis", "eligibility", "claims"}
    elif source == "grants":
        keep = {"medicaid", "medicare", "cms", "rural health", "rural health transformation", "telehealth", "behavioral health", "workforce"}
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
            body = exc.read(600).decode("utf-8", "replace")
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


def emit(progress: Callable[[str], None] | None, message: str) -> None:
    if progress:
        progress(message)
