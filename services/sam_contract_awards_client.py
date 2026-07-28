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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from services.search_taxonomy import (
    load_search_parameters as load_taxonomy_parameters,
    load_search_taxonomy,
)
from services.sam_cache import DEFAULT_SAM_LEDGER_PATH, DEFAULT_SAM_RAW_CACHE_DIR, RawSAMCache
from services.sam_quota import SAMLiveCallBlocked, SAMQuotaError, SAMQuotaGuard, policy_from_settings

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
SAM_CONTRACT_AWARDS_URL = "https://api.sam.gov/contract-awards/v1/search"
USER_AGENT = "soe-group3-sam-contract-awards/0.1"
LEGACY_INCLUDE_SECTIONS = "contractId,coreData,awardDetails,awardeeData"
ENRICHED_INCLUDE_SECTIONS = ",".join(
    [
        "contractId",
        "coreData",
        "awardDetails",
        "awardeeData",
        "dates",
        "dollars",
        "totalContractDollars",
        "productOrServiceInformation",
        "principalPlaceOfPerformance",
    ]
)

SAM_CONTRACT_AWARD_FIELDS = [
    "award_id",
    "source_key",
    "source_record_id",
    "piid",
    "parent_award_id",
    "solicitation_number",
    "award_date",
    "period_start_date",
    "period_end_date",
    "potential_end_date",
    "lifecycle_status",
    "recompete_window_start",
    "award_amount",
    "current_total_value",
    "potential_total_value",
    "vendor_key",
    "vendor_name",
    "uei_sam",
    "cage_code",
    "agency",
    "subagency",
    "office",
    "naics",
    "psc",
    "description",
    "place_of_performance_state",
    "program_focus",
    "topic_keys",
    "competitor_flag",
    "gwt_relation",
    "importance_score",
    "predictive_value_usd",
    "score_evidence_json",
    "document_url",
    "raw_json",
    "last_checked_at",
]

PROGRAM_TERMS = {
    "medicaid": ["medicaid", "mmis", "medicaid management information"],
    "medicare": ["medicare"],
    "cms": ["cms", "centers for medicare", "centers for medicaid"],
    "rht": ["rural health transformation"],
    "managed_care": ["managed care", "mco"],
    "eligibility": ["eligibility", "enrollment"],
    "claims": ["claims", "claim processing"],
    "provider_data": ["provider data", "provider directory", "provider enrollment"],
    "interoperability": ["interoperability", "fhir", "api gateway"],
    "quality": ["quality measures", "star rating", "stars"],
    "rural_health": ["rural health", "critical access hospital", "telehealth"],
    "contact_center": ["contact center", "call center", "customer service"],
}

CANONICAL_VENDOR_KEYS = {
    "gainwell": "gainwell_technologies",
    "gainwell_technologies": "gainwell_technologies",
    "dxc": "gainwell_technologies",
    "dxc_technology_services": "gainwell_technologies",
    "health_management_systems": "gainwell_technologies",
    "hms": "gainwell_technologies",
    "maximus": "maximus",
    "maximus_federal_services": "maximus",
    "deloitte": "deloitte",
    "deloitte_consulting": "deloitte",
    "accenture": "accenture_federal_services",
    "accenture_federal_services": "accenture_federal_services",
    "optum": "optum",
    "optumserve": "optum",
    "conduent": "conduent",
    "conduent_state_healthcare": "conduent",
}

DATE_FILTER_FIELDS = {
    "approvedDate",
    "closedDate",
    "solicitationDate",
    "createdDate",
    "currentCompletionDate",
    "dateSigned",
    "lastModifiedDate",
    "periodOfPerformanceStartDate",
    "ultimateCompletionDate",
}


class RateLimitError(RuntimeError):
    """Raised when SAM.gov returns 429 so callers can preserve existing CSVs."""


@dataclass(frozen=True)
class VendorConfig:
    vendor_key: str
    name: str
    aliases: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class SearchSpec:
    kind: str
    label: str
    params: dict[str, str]
    vendor_key: str = ""
    vendor_name: str = ""
    deleted: bool = False
    date_field: str = ""
    window: str = ""


@dataclass
class SAMContractAwardsConfig:
    api_key: str = ""
    params_path: Path = DATA_DIR / "search_parameters.json"
    vendor_entities_path: Path = DATA_DIR / "vendor_entities.csv"
    mode: str = "historic"
    keywords: list[str] | None = None
    vendors_override: list[str] | None = None
    vendor_group: list[str] | None = None
    start_date: dt.date | None = None
    end_date: dt.date | None = None
    years_back: int = 8
    days_back: int = 365
    agency_codes: list[str] = field(default_factory=lambda: ["7500"])
    max_searches: int = 12
    max_per_search: int = 25
    page_limit: int = 25
    max_pages: int = 1
    include_broad_agency_searches: bool = True
    include_vendor_searches: bool = False
    include_keyword_searches: bool = False
    include_deleted: bool = False
    env_file: Path = ROOT / ".env"
    sam_quota_mode: str = "cache-only"
    sam_live_budget: int = 0
    sam_cache_dir: Path = DEFAULT_SAM_RAW_CACHE_DIR
    sam_ledger_path: Path = DEFAULT_SAM_LEDGER_PATH


def fetch_sam_contract_awards(
    config: SAMContractAwardsConfig,
    progress: Callable[[str], None] | None = None,
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    load_env_file(config.env_file)
    if not config.api_key:
        config.api_key = os.environ.get("SAM_API_KEY", "")
    if sam_live_enabled(config) and not config.api_key:
        raise RuntimeError("SAM_API_KEY not configured for live SAM mode")

    params = load_search_parameters(config.params_path)
    vendors = configured_vendors(params, config.vendors_override, config.vendor_group)
    keywords = config.keywords or load_search_taxonomy(config.params_path).business_terms
    specs = build_search_specs(config, vendors, keywords)
    if sam_live_enabled(config):
        specs = [spec for spec in specs if spec.kind in {"agency_signed", "agency_expiring"} and not spec.deleted]
    if config.max_searches > 0:
        specs = specs[: config.max_searches]

    start, end = date_window(config)
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    checked_specs: list[dict[str, str]] = []

    for spec in specs:
        spec_count = 0
        emit(progress, f"search {spec.kind}: {spec.label}")
        date_field = spec.date_field or ("lastModifiedDate" if config.mode == "continue" or spec.deleted else "dateSigned")
        spec_start, spec_end = date_window_for_spec(config, spec, start, end)
        chunks = [(spec_start, spec_end)] if spec.window else date_chunks(spec_start, spec_end, date_field)
        for chunk_start, chunk_end in chunks:
            page = 0
            while page < max(config.max_pages, 1) and spec_count < config.max_per_search:
                limit = min(max(config.page_limit, 1), 100, max(config.max_per_search - spec_count, 1))
                request_params = build_request_params(config, spec, date_field, chunk_start, chunk_end, page, limit)
                try:
                    data = http_json(SAM_CONTRACT_AWARDS_URL, request_params, config)
                except SAMLiveCallBlocked:
                    break
                items = data.get("awardSummary") or []
                if not isinstance(items, list):
                    items = []

                for item in items:
                    if not isinstance(item, dict) or not award_local_match(item, spec, vendors, keywords):
                        continue
                    row = normalize_award(item, spec, vendors)
                    key = contract_key(row)
                    if not key or key in seen:
                        continue
                    seen.add(key)
                    rows.append(row)
                    spec_count += 1
                    if spec_count >= config.max_per_search:
                        break

                total = int_or_zero(data.get("totalRecords"))
                if len(items) < limit or ((page + 1) * limit >= total > 0):
                    break
                page += 1
        checked_specs.append({"kind": spec.kind, "label": spec.label, "records": str(spec_count)})
        emit(progress, f"{spec.label}: {spec_count} records")

    rows = sorted(rows, key=sort_key, reverse=True)
    summary = summarize_sam_awards(rows)
    summary["searches_run"] = len(checked_specs)
    summary["searches"] = checked_specs
    summary["window_start"] = start.isoformat()
    summary["window_end"] = end.isoformat()
    return rows, summary


def build_request_params(
    config: SAMContractAwardsConfig,
    spec: SearchSpec,
    date_field: str,
    start: dt.date,
    end: dt.date,
    page: int,
    limit: int,
) -> dict[str, str]:
    params = dict(spec.params)
    params.update(
        {
            "api_key": config.api_key,
            "limit": str(limit),
            "offset": str(page * limit),
            "includeSections": include_sections_for_config(config),
        }
    )
    if spec.deleted:
        params["deletedStatus"] = "yes"
    if not DATE_FILTER_FIELDS.intersection(params):
        params[date_field] = format_date_range(start, end)
    return params


def include_sections_for_config(config: SAMContractAwardsConfig) -> str:
    # Preserve cache keys for existing raw payloads; use broader sections only on next live reset.
    return ENRICHED_INCLUDE_SECTIONS if sam_live_enabled(config) else LEGACY_INCLUDE_SECTIONS


def build_search_specs(config: SAMContractAwardsConfig, vendors: list[VendorConfig], keywords: list[str]) -> list[SearchSpec]:
    specs: list[SearchSpec] = []

    if config.include_broad_agency_searches:
        for agency_code in config.agency_codes or ["7500"]:
            base_params = {"contractingDepartmentCode": agency_code} if agency_code else {}
            specs.append(
                SearchSpec(
                    kind="agency_signed",
                    label=f"{agency_code or 'all'} / signed awards",
                    params=dict(base_params),
                    date_field="dateSigned",
                    window="signed_365",
                )
            )
            specs.append(
                SearchSpec(
                    kind="agency_expiring",
                    label=f"{agency_code or 'all'} / expiring awards",
                    params=dict(base_params),
                    date_field="currentCompletionDate",
                    window="expiring_24m",
                )
            )

    if config.include_vendor_searches:
        for vendor in vendors:
            for query in unique_list([vendor.name, *vendor.aliases]):
                specs.append(
                    SearchSpec(
                        kind="vendor_alias",
                        label=f"{vendor.name} / {query}",
                        params={"awardeeLegalBusinessName": query},
                        vendor_key=vendor.vendor_key,
                        vendor_name=vendor.name,
                    )
                )

        for row in load_vendor_entities(config.vendor_entities_path):
            vendor_key = canonical_vendor_key(row.get("vendor_key") or row.get("vendor") or row.get("vendor_name") or "")
            if vendors and vendor_key not in {vendor.vendor_key for vendor in vendors}:
                continue
            vendor_name = row.get("vendor_name") or row.get("name") or vendor_key
            uei = row.get("uei_sam") or row.get("uei") or row.get("unique_entity_id")
            cage = row.get("cage_code") or row.get("cage")
            if uei:
                specs.append(
                    SearchSpec(
                        kind="vendor_uei",
                        label=f"{vendor_name} / UEI",
                        params={"awardeeUniqueEntityId": uei},
                        vendor_key=vendor_key,
                        vendor_name=vendor_name,
                    )
                )
            if cage:
                specs.append(
                    SearchSpec(
                        kind="vendor_cage",
                        label=f"{vendor_name} / CAGE",
                        params={"awardeeCageCode": cage},
                        vendor_key=vendor_key,
                        vendor_name=vendor_name,
                    )
                )

    if config.include_keyword_searches:
        for agency_code in config.agency_codes or [""]:
            for keyword in keywords:
                params = {"q": keyword}
                if agency_code:
                    params["contractingDepartmentCode"] = agency_code
                specs.append(SearchSpec(kind="keyword_agency", label=f"{agency_code or 'all'} / {keyword}", params=params))

    if config.include_deleted:
        deleted_specs = [
            SearchSpec(
                kind=f"deleted_{spec.kind}",
                label=spec.label,
                params=dict(spec.params),
                vendor_key=spec.vendor_key,
                vendor_name=spec.vendor_name,
                deleted=True,
                date_field=spec.date_field,
                window=spec.window,
            )
            for spec in specs
        ]
        specs.extend(deleted_specs)

    return specs


def configured_vendors(
    params: dict[str, Any], vendors_override: list[str] | None = None, vendor_group: list[str] | None = None
) -> list[VendorConfig]:
    if vendors_override:
        return [VendorConfig(canonical_vendor_key(name), name, []) for name in vendors_override if name]

    selected = {canonical_vendor_key(name) for name in vendor_group or [] if name}
    vendors: list[VendorConfig] = []
    for item in params.get("vendors") or []:
        if isinstance(item, str):
            name = item.strip()
            aliases: list[str] = []
        elif isinstance(item, dict):
            name = str(item.get("name") or "").strip()
            aliases = [str(alias).strip() for alias in item.get("aliases") or [] if str(alias).strip()]
        else:
            continue
        if not name:
            continue
        vendor_key = canonical_vendor_key(name)
        if selected and vendor_key not in selected and normalize_token(name) not in selected:
            continue
        vendors.append(VendorConfig(vendor_key, name, aliases))
    return vendors


def award_local_match(item: dict[str, Any], spec: SearchSpec, vendors: list[VendorConfig], keywords: list[str]) -> bool:
    if spec.kind.startswith("vendor_") or spec.kind.startswith("deleted_vendor_"):
        return True
    text = award_text(item)
    lower = text.lower()
    if any(keyword and keyword.lower() in lower for keyword in keywords):
        return True
    focus, topics = program_focus(text)
    if any(value != "review" for value in focus) or topics:
        return True
    vendor_name = first_path(
        item,
        [
            "awardDetails.awardeeData.awardeeHeader.awardeeName",
            "awardDetails.awardeeData.awardeeHeader.legalBusinessName",
            "awardDetails.awardeeData.awardeeHeader.awardeeNameFromContract",
        ],
    )
    return resolve_vendor_key(vendor_name, vendors) != "unknown"


def award_text(item: dict[str, Any]) -> str:
    paths = [
        "awardDetails.awardeeData.awardeeHeader.awardeeName",
        "awardDetails.awardeeData.awardeeHeader.legalBusinessName",
        "coreData.federalOrganization.contractingInformation.contractingDepartment.name",
        "coreData.federalOrganization.contractingInformation.contractingSubtier.name",
        "coreData.federalOrganization.contractingInformation.contractingOffice.name",
        "awardDetails.productOrServiceInformation.descriptionOfContractRequirement",
        "coreData.productOrServiceInformation.descriptionOfContractRequirement",
        "coreData.acquisitionData.majorProgramCode",
        "awardDetails.productOrServiceInformation.principalNAICS.code",
        "awardDetails.productOrServiceInformation.principalNAICS.name",
        "coreData.productOrServiceInformation.principalNAICS.code",
        "coreData.productOrServiceInformation.principalNAICS.name",
        "awardDetails.productOrServiceInformation.productOrService.code",
        "awardDetails.productOrServiceInformation.productOrService.name",
        "coreData.productOrServiceInformation.productOrService.code",
        "coreData.productOrServiceInformation.productOrService.name",
    ]
    return " ".join(first_path(item, [path]) for path in paths)


def normalize_award(item: dict[str, Any], spec: SearchSpec, vendors: list[VendorConfig]) -> dict[str, str]:
    piid = first_path(item, ["contractId.piid"])
    parent_award_id = first_path(
        item,
        [
            "contractId.referencedIDVPiid",
            "contractId.referencedIdvPiid",
            "awardDetails.contractData.referencedIDVPiid",
        ],
    )
    source_record_id = source_id(item, piid, parent_award_id)
    award_id = stable_id("sam-award", source_record_id or json.dumps(item, sort_keys=True))

    award_date = iso_date(
        first_path(item, ["awardDetails.dates.dateSigned", "awardDetails.dateSigned", "coreData.dateSigned", "dateSigned"])
    )
    period_start = iso_date(
        first_path(
            item,
            [
                "awardDetails.dates.periodOfPerformanceStartDate",
                "awardDetails.periodOfPerformanceStartDate",
                "coreData.periodOfPerformanceStartDate",
                "periodOfPerformanceStartDate",
            ],
        )
    )
    period_end = iso_date(
        first_path(
            item,
            [
                "awardDetails.dates.currentCompletionDate",
                "awardDetails.currentCompletionDate",
                "coreData.currentCompletionDate",
                "currentCompletionDate",
            ],
        )
    )
    potential_end = iso_date(
        first_path(
            item,
            [
                "awardDetails.dates.ultimateCompletionDate",
                "awardDetails.ultimateCompletionDate",
                "coreData.ultimateCompletionDate",
                "ultimateCompletionDate",
            ],
        )
    )
    lifecycle_status = lifecycle(period_end, potential_end, spec.deleted)
    recompete_start = iso_date(add_months(parse_date(potential_end or period_end), -36)) if (potential_end or period_end) else ""

    vendor_name = clean_text(
        first_path(
            item,
            [
                "awardDetails.awardeeData.awardeeHeader.awardeeName",
                "awardDetails.awardeeData.awardeeHeader.legalBusinessName",
                "awardDetails.awardeeData.awardeeHeader.awardeeNameFromContract",
                "awardDetails.awardeeData.awardeeHeader.awardeeDoingBusinessAsName",
            ],
        )
        or spec.vendor_name,
        180,
    )
    vendor_key = spec.vendor_key or resolve_vendor_key(vendor_name, vendors)
    gwt_relation = relation_to_gwt(vendor_key, vendor_name)
    competitor_flag = "yes" if vendor_key not in {"", "unknown", "gainwell_technologies"} else "no"

    agency = clean_text(
        first_path(
            item,
            [
                "coreData.federalOrganization.contractingInformation.contractingDepartment.name",
                "coreData.federalOrganization.fundingInformation.fundingDepartment.name",
                "contractingDepartment.name",
                "department.name",
            ],
        ),
        180,
    )
    subagency = clean_text(
        first_path(
            item,
            [
                "coreData.federalOrganization.contractingInformation.contractingSubtier.name",
                "coreData.federalOrganization.fundingInformation.fundingSubtier.name",
                "contractId.subtier.name",
                "subtier.name",
            ],
        ),
        180,
    )
    office = clean_text(
        first_path(
            item,
            [
                "coreData.federalOrganization.contractingInformation.contractingOffice.name",
                "coreData.federalOrganization.fundingInformation.fundingOffice.name",
                "contractingOffice.name",
                "office.name",
            ],
        ),
        180,
    )
    naics = first_path(
        item,
        [
            "awardDetails.productOrServiceInformation.principalNAICS.code",
            "awardDetails.productOrServiceInformation.idvNAICS.code",
            "awardDetails.productOrServiceInformation.naicsCode",
            "coreData.productOrServiceInformation.principalNAICS.code",
            "coreData.productOrServiceInformation.idvNAICS.code",
            "coreData.productOrServiceInformation.naicsCode",
        ],
    )
    psc = first_path(
        item,
        [
            "awardDetails.productOrServiceInformation.productOrService.code",
            "awardDetails.productOrServiceInformation.pscCode",
            "coreData.productOrServiceInformation.productOrService.code",
            "coreData.productOrServiceInformation.pscCode",
        ],
    )
    description = clean_text(
        first_path(
            item,
            [
                "awardDetails.productOrServiceInformation.descriptionOfContractRequirement",
                "coreData.productOrServiceInformation.descriptionOfContractRequirement",
                "coreData.acquisitionData.majorProgramCode",
                "awardDetails.productOrServiceInformation.productOrService.name",
                "coreData.productOrServiceInformation.productOrService.name",
                "awardDetails.productOrServiceInformation.principalNAICS.name",
                "coreData.productOrServiceInformation.principalNAICS.name",
            ],
        ),
        1000,
    )
    place_state = first_path(
        item,
        [
            "coreData.principalPlaceOfPerformance.state.code",
            "coreData.principalPlaceOfPerformance.state.name",
            "awardDetails.principalPlaceOfPerformance.state.code",
            "awardDetails.principalPlaceOfPerformance.state.name",
        ],
    )
    award_amount = money(
        first_path(
            item,
            [
                "awardDetails.dollars.actionObligation",
                "awardDetails.dollars.baseDollarsObligated",
                "awardDetails.dollars.obligatedAmount",
                "awardDetails.actionObligation",
                "coreData.dollars.actionObligation",
                "coreData.actionObligation",
                "actionObligation",
            ],
        )
    )
    current_total = money(
        first_path(
            item,
            [
                "awardDetails.totalContractDollars.totalBaseAndExercisedOptionsValue",
                "awardDetails.totalContractDollars.totalActionObligation",
                "awardDetails.dollars.baseAndExercisedOptionsValue",
                "awardDetails.dollars.currentTotalValue",
                "awardDetails.currentTotalValue",
                "coreData.totalContractDollars.totalBaseAndExercisedOptionsValue",
                "coreData.dollars.baseAndExercisedOptionsValue",
                "currentTotalValue",
            ],
        )
    )
    potential_total = money(
        first_path(
            item,
            [
                "awardDetails.totalContractDollars.totalBaseAndAllOptionsValue",
                "awardDetails.dollars.baseAndAllOptionsValue",
                "awardDetails.dollars.totalEstimatedOrderValue",
                "awardDetails.dollars.potentialTotalValue",
                "awardDetails.potentialTotalValue",
                "coreData.totalContractDollars.totalBaseAndAllOptionsValue",
                "coreData.dollars.baseAndAllOptionsValue",
                "potentialTotalValue",
            ],
        )
    )

    text_for_topics = " ".join([vendor_name, agency, subagency, office, naics, psc, description])
    focus, topics = program_focus(text_for_topics)
    predictive_value = best_money(potential_total, current_total, award_amount)
    score = importance_score(vendor_key, agency, subagency, focus, lifecycle_status, predictive_value)
    evidence = {
        "search_kind": spec.kind,
        "search_label": spec.label,
        "topic_keys": topics,
        "date_basis": {
            "period_end_date": period_end,
            "potential_end_date": potential_end,
            "recompete_window_start": recompete_start,
        },
        "amount_basis": predictive_value,
    }

    return {
        "award_id": award_id,
        "source_key": "sam_contract_awards",
        "source_record_id": source_record_id,
        "piid": piid,
        "parent_award_id": parent_award_id,
        "solicitation_number": first_path(
            item,
            [
                "coreData.solicitationId",
                "coreData.solicitationID",
                "awardDetails.solicitationId",
                "awardDetails.contractData.solicitationId",
                "solicitationId",
            ],
        ),
        "award_date": award_date,
        "period_start_date": period_start,
        "period_end_date": period_end,
        "potential_end_date": potential_end,
        "lifecycle_status": lifecycle_status,
        "recompete_window_start": recompete_start,
        "award_amount": award_amount,
        "current_total_value": current_total,
        "potential_total_value": potential_total,
        "vendor_key": vendor_key or "unknown",
        "vendor_name": vendor_name,
        "uei_sam": first_path(
            item,
            [
                "awardDetails.awardeeData.awardeeUEIInformation.uniqueEntityId",
                "awardDetails.awardeeData.awardeeUEIInformation.ueiSAM",
                "awardDetails.awardeeData.awardeeUEIInformation.uei",
                "awardDetails.awardeeData.ueiSAM",
                "awardeeData.awardeeUEIInformation.uniqueEntityId",
            ],
        ),
        "cage_code": first_path(
            item,
            [
                "awardDetails.awardeeData.awardeeUEIInformation.cageCode",
                "awardDetails.awardeeData.cageCode",
                "awardeeData.awardeeUEIInformation.cageCode",
            ],
        ),
        "agency": agency,
        "subagency": subagency,
        "office": office,
        "naics": naics,
        "psc": psc,
        "description": description,
        "place_of_performance_state": place_state,
        "program_focus": ";".join(focus),
        "topic_keys": ";".join(topics),
        "competitor_flag": competitor_flag,
        "gwt_relation": gwt_relation,
        "importance_score": str(score),
        "predictive_value_usd": predictive_value,
        "score_evidence_json": json.dumps(evidence, separators=(",", ":"), sort_keys=True),
        "document_url": document_url(piid),
        "raw_json": json.dumps(item, separators=(",", ":"), sort_keys=True),
        "last_checked_at": now_iso(),
    }


def upsert_sam_contract_awards(path: Path, new_rows: list[dict[str, str]]) -> tuple[int, int, int]:
    existing = read_csv(path)
    by_key = {contract_key(row): row for row in existing if contract_key(row)}
    added = 0
    updated = 0

    for row in new_rows:
        key = contract_key(row)
        if not key:
            continue
        old = by_key.get(key)
        if old is None:
            by_key[key] = {field: row.get(field, "") for field in SAM_CONTRACT_AWARD_FIELDS}
            added += 1
            continue
        merged = dict(old)
        changed = False
        for field in SAM_CONTRACT_AWARD_FIELDS:
            value = row.get(field, "")
            if merged.get(field, "") != value:
                merged[field] = value
                changed = True
        if changed:
            updated += 1
        by_key[key] = merged

    rows = sorted(by_key.values(), key=sort_key, reverse=True)
    write_csv(path, SAM_CONTRACT_AWARD_FIELDS, rows)
    return added, updated, len(rows)


def summarize_sam_awards(rows: list[dict[str, str]]) -> dict[str, Any]:
    summary = {
        "rows": len(rows),
        "by_vendor_key": count_by(rows, "vendor_key"),
        "by_agency": count_by(rows, "agency"),
        "by_lifecycle_status": count_by(rows, "lifecycle_status"),
        "by_program_focus": count_list_field(rows, "program_focus"),
        "period_end_present": sum(1 for row in rows if row.get("period_end_date")),
        "potential_end_present": sum(1 for row in rows if row.get("potential_end_date")),
        "award_amount_present": sum(1 for row in rows if int_or_zero(row.get("award_amount")) != 0),
    }
    return summary


def load_search_parameters(path: Path) -> dict[str, Any]:
    return load_taxonomy_parameters(path)


def load_vendor_entities(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    return read_csv(path)


def http_json(url: str, params: dict[str, str], config: SAMContractAwardsConfig, timeout: int = 60) -> Any:
    if sam_live_enabled(config) and not config.api_key:
        raise RuntimeError("SAM_API_KEY not configured for live SAM mode")

    guard = sam_quota_guard(config)
    cached = guard.cache.get("GET", url, params)
    if cached is not None:
        data = cached_response_json(cached)
        guard.log_cache_hit("GET", url, params, record_count=sam_award_record_count(data), caller="sam_contract_awards")
        return data

    full_url = url + "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(full_url, headers={"Accept": "application/json", "User-Agent": USER_AGENT})
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            guard.require_live_call("GET", url, params, caller="sam_contract_awards")
        except SAMLiveCallBlocked:
            raise
        except SAMQuotaError as exc:
            raise RuntimeError(str(exc)) from exc
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                response_text = response.read().decode("utf-8")
                data = json.loads(response_text)
                guard.cache.put("GET", url, params, None, response.status, response_text, dict(response.headers.items()))
                guard.log_live_result("GET", url, params, status="live_ok", record_count=sam_award_record_count(data), caller="sam_contract_awards")
                return data
        except urllib.error.HTTPError as exc:
            body = redact_secret(exc.read(800).decode("utf-8", "replace"), config.api_key)
            if exc.code == 429:
                guard.log_live_result("GET", url, params, status="rate_limited", caller="sam_contract_awards", note="SAM_API_KEY 429")
                raise RateLimitError("SAM_API_KEY 429") from exc
            guard.log_live_result("GET", url, params, status="http_error", caller="sam_contract_awards", note=f"HTTP {exc.code}")
            raise RuntimeError(f"HTTP {exc.code} from {sanitize_url(full_url)}: {body}") from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = exc
            status = "timeout" if isinstance(exc, TimeoutError) else "live_error"
            note = redact_secret(str(exc), config.api_key)
            guard.log_live_result("GET", url, params, status=status, caller="sam_contract_awards", note=note)
            time.sleep(1 + attempt)
    message = redact_secret(str(last_error), config.api_key) if last_error else "unknown error"
    raise RuntimeError(f"request failed: {message}")


def sam_live_enabled(config: SAMContractAwardsConfig) -> bool:
    return str(config.sam_quota_mode).strip().lower() == "live" and config.sam_live_budget > 0


def sam_quota_guard(config: SAMContractAwardsConfig) -> SAMQuotaGuard:
    policy = policy_from_settings(config.sam_quota_mode, config.sam_live_budget, config.sam_ledger_path)
    cache = RawSAMCache(root=config.sam_cache_dir, ledger_path=config.sam_ledger_path)
    return SAMQuotaGuard(policy=policy, cache=cache)


def cached_response_json(cached: dict[str, Any]) -> Any:
    return json.loads(str(cached.get("response_text") or "{}"))


def sam_award_record_count(data: Any) -> int:
    rows = data.get("awardSummary") if isinstance(data, dict) else []
    if isinstance(rows, list):
        return len(rows)
    return int_or_zero(data.get("totalRecords")) if isinstance(data, dict) else 0


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


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.tmp")
    with tmp_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    tmp_path.replace(path)


def date_window(config: SAMContractAwardsConfig) -> tuple[dt.date, dt.date]:
    end = config.end_date or dt.date.today()
    if config.start_date:
        return config.start_date, end
    if config.mode == "continue":
        return end - dt.timedelta(days=max(config.days_back, 1)), end
    return end - dt.timedelta(days=365 * max(config.years_back, 1)), end


def date_window_for_spec(
    config: SAMContractAwardsConfig,
    spec: SearchSpec,
    default_start: dt.date,
    default_end: dt.date,
) -> tuple[dt.date, dt.date]:
    anchor = config.end_date or dt.date.today()
    if spec.window == "signed_365":
        return anchor - dt.timedelta(days=365), anchor
    if spec.window == "expiring_24m":
        return anchor, anchor + dt.timedelta(days=730)
    return default_start, default_end


def date_chunks(start: dt.date, end: dt.date, date_field: str) -> list[tuple[dt.date, dt.date]]:
    if date_field == "lastModifiedDate":
        return [(start, end)]
    chunks: list[tuple[dt.date, dt.date]] = []
    cursor = start
    while cursor <= end:
        chunk_end = min(end, cursor + dt.timedelta(days=364))
        chunks.append((cursor, chunk_end))
        cursor = chunk_end + dt.timedelta(days=1)
    return chunks


def format_date_range(start: dt.date, end: dt.date) -> str:
    return f"[{start.strftime('%m/%d/%Y')},{end.strftime('%m/%d/%Y')}]"


def source_id(item: dict[str, Any], piid: str, parent_award_id: str) -> str:
    parts = [
        first_path(item, ["contractId.subtier.code"]),
        piid,
        first_path(item, ["contractId.modificationNumber"]),
        first_path(item, ["contractId.transactionNumber"]),
        parent_award_id,
        first_path(item, ["contractId.referencedIDVModificationNumber"]),
    ]
    value = "|".join(part for part in parts if part)
    return value or stable_id("sam-source", json.dumps(item, sort_keys=True))


def contract_key(row: dict[str, str]) -> str:
    return row.get("source_record_id") or row.get("award_id") or row.get("piid", "")


def sort_key(row: dict[str, str]) -> tuple[int, int, int, str]:
    status_rank = {"near_expiry": 5, "active": 4, "awarded": 3, "expired": 2, "unknown": 1, "canceled": 0}
    return (
        status_rank.get(row.get("lifecycle_status", "unknown"), 1),
        int_or_zero(row.get("importance_score")),
        int_or_zero(row.get("predictive_value_usd")),
        row.get("potential_end_date") or row.get("period_end_date") or row.get("award_date") or "",
    )


def first_path(obj: dict[str, Any], paths: list[str]) -> str:
    for path in paths:
        for value in path_values(obj, path.split(".")):
            text = scalar_text(value)
            if text:
                return text
    return ""


def path_values(value: Any, parts: list[str]) -> list[Any]:
    if not parts:
        if isinstance(value, list):
            output: list[Any] = []
            for item in value:
                output.extend(path_values(item, []))
            return output
        return [value]
    if isinstance(value, list):
        output = []
        for item in value:
            output.extend(path_values(item, parts))
        return output
    if not isinstance(value, dict):
        return []
    key = matching_key(value, parts[0])
    if key is None:
        return []
    return path_values(value[key], parts[1:])


def matching_key(mapping: dict[str, Any], requested: str) -> str | None:
    if requested in mapping:
        return requested
    requested_lower = requested.lower()
    for key in mapping:
        if str(key).lower() == requested_lower:
            return str(key)
    requested_compact = re.sub(r"[^a-z0-9]+", "", requested_lower)
    for key in mapping:
        if re.sub(r"[^a-z0-9]+", "", str(key).lower()) == requested_compact:
            return str(key)
    return None


def scalar_text(value: Any) -> str:
    if value in (None, "", [], {}):
        return ""
    if isinstance(value, list):
        for item in value:
            text = scalar_text(item)
            if text:
                return text
        return ""
    if isinstance(value, dict):
        for requested in ("name", "code", "value", "description"):
            key = matching_key(value, requested)
            text = scalar_text(value.get(key)) if key is not None else ""
            if text:
                return text
        return ""
    return str(value)


def lifecycle(period_end: str, potential_end: str, deleted: bool) -> str:
    if deleted:
        return "canceled"
    end = parse_date(potential_end) or parse_date(period_end)
    if not end:
        return "awarded" if period_end or potential_end else "unknown"
    days = (end - dt.date.today()).days
    if days < 0:
        return "expired"
    if days <= 18 * 30:
        return "near_expiry"
    return "active"


def program_focus(text: str) -> tuple[list[str], list[str]]:
    lower = text.lower()
    focus = [key for key, terms in PROGRAM_TERMS.items() if any(term in lower for term in terms)]
    topics = sorted({term for terms in PROGRAM_TERMS.values() for term in terms if term in lower}, key=str.lower)
    return focus or ["review"], topics


def importance_score(
    vendor_key: str, agency: str, subagency: str, focus: list[str], lifecycle_status: str, predictive_value: str
) -> int:
    score = 10
    if vendor_key == "gainwell_technologies":
        score += 16
    elif vendor_key and vendor_key != "unknown":
        score += 12
    if any(term in (agency + " " + subagency).lower() for term in ["health", "medicare", "medicaid", "cms"]):
        score += 15
    score += min(30, len([key for key in focus if key != "review"]) * 7)
    amount = int_or_zero(predictive_value)
    if amount >= 10_000_000:
        score += 20
    elif amount >= 1_000_000:
        score += 12
    elif amount >= 250_000:
        score += 6
    if lifecycle_status == "near_expiry":
        score += 20
    elif lifecycle_status == "active":
        score += 10
    elif lifecycle_status == "awarded":
        score += 8
    return min(score, 100)


def relation_to_gwt(vendor_key: str, vendor_name: str) -> str:
    if vendor_key != "gainwell_technologies":
        return "competitor" if vendor_key and vendor_key != "unknown" else "unknown"
    normalized = normalize_token(vendor_name)
    if "dxc" in normalized or "health_management_systems" in normalized or normalized == "hms":
        return "predecessor_alias"
    return "self"


def resolve_vendor_key(vendor_name: str, vendors: list[VendorConfig]) -> str:
    normalized = normalize_token(vendor_name)
    for vendor in vendors:
        names = [vendor.name, *vendor.aliases]
        if any(normalize_token(name) and normalize_token(name) in normalized for name in names):
            return vendor.vendor_key
    return canonical_vendor_key(vendor_name)


def canonical_vendor_key(value: str) -> str:
    normalized = normalize_token(value)
    if normalized in CANONICAL_VENDOR_KEYS:
        return CANONICAL_VENDOR_KEYS[normalized]
    for token, vendor_key in CANONICAL_VENDOR_KEYS.items():
        if token and token in normalized:
            return vendor_key
    return normalized or "unknown"


def normalize_token(value: str) -> str:
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", str(value or "").lower())).strip("_")


def unique_list(values: list[str]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for value in values:
        cleaned = value.strip()
        key = cleaned.lower()
        if cleaned and key not in seen:
            seen.add(key)
            output.append(cleaned)
    return output


def count_by(rows: list[dict[str, str]], field: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        key = row.get(field) or "unknown"
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


def count_list_field(rows: list[dict[str, str]], field: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        values = [item.strip() for item in row.get(field, "").split(";") if item.strip()] or ["unknown"]
        for value in values:
            counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


def parse_date(value: Any) -> dt.date | None:
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    for fmt in (
        "%Y-%m-%d",
        "%m/%d/%Y",
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S.%f%z",
    ):
        try:
            return dt.datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    try:
        return dt.datetime.fromisoformat(text).date()
    except ValueError:
        return None


def iso_date(value: Any) -> str:
    if isinstance(value, dt.date):
        return value.isoformat()
    parsed = parse_date(value)
    return parsed.isoformat() if parsed else ""


def add_months(value: dt.date | None, months: int) -> dt.date | None:
    if value is None:
        return None
    month = value.month - 1 + months
    year = value.year + month // 12
    month = month % 12 + 1
    day = min(value.day, month_days(year, month))
    return dt.date(year, month, day)


def month_days(year: int, month: int) -> int:
    if month == 12:
        return 31
    return (dt.date(year, month + 1, 1) - dt.timedelta(days=1)).day


def money(value: Any) -> str:
    if value in (None, ""):
        return ""
    try:
        return str(int(round(float(str(value).replace(",", "").replace("$", "")))))
    except (TypeError, ValueError):
        return ""


def best_money(*values: str) -> str:
    parsed = [int_or_zero(value) for value in values if value not in (None, "")]
    return str(max(parsed)) if parsed else ""


def int_or_zero(value: Any) -> int:
    try:
        return int(float(str(value or 0).replace(",", "").replace("$", "")))
    except (TypeError, ValueError):
        return 0


def stable_id(prefix: str, value: str) -> str:
    digest = hashlib.sha1(value.encode("utf-8", "ignore")).hexdigest()[:16]
    return f"{prefix}-{digest}"


def document_url(piid: str) -> str:
    if not piid:
        return "https://sam.gov/search/?index=awards"
    return "https://sam.gov/search/?" + urllib.parse.urlencode({"index": "awards", "keywords": piid})


def clean_text(value: Any, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "..."


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def redact_secret(text: str, secret: str) -> str:
    return text.replace(secret, "REDACTED") if secret else text


def sanitize_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    redacted = [(key, "REDACTED") if key.lower() in {"api_key", "apikey"} else (key, value) for key, value in query]
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urllib.parse.urlencode(redacted), parsed.fragment))


def emit(progress: Callable[[str], None] | None, message: str) -> None:
    if progress:
        progress(message)
