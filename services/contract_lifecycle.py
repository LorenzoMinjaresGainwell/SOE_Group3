from __future__ import annotations

import calendar
import csv
import datetime as dt
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from services.search_taxonomy import load_search_taxonomy

LIFECYCLE_FIELDS = [
    "contract_id",
    "source_keys",
    "source_record_ids",
    "lifecycle_status",
    "contract_vehicle",
    "piid",
    "parent_award_id",
    "solicitation_number",
    "title",
    "agency",
    "subagency",
    "office",
    "vendor_key",
    "vendor_name",
    "uei_sam",
    "incumbent_vendor_key",
    "competitor_flag",
    "gwt_relation",
    "known_bid_status",
    "award_date",
    "period_start_date",
    "period_end_date",
    "potential_end_date",
    "recompete_window_start",
    "days_until_end",
    "award_amount",
    "current_total_value",
    "potential_total_value",
    "predictive_value_usd",
    "program_focus",
    "topic_keys",
    "importance_score",
    "score_evidence_json",
    "source_urls",
    "last_checked_at",
]

DEFAULT_OPTIONAL_INPUTS = {
    "sam_contract_awards": "data/sam_contract_awards.csv",
    "sam_opportunities": "data/federal_opportunities.csv",
    "vendor_entities": "data/vendor_entities.csv",
}

LIFECYCLE_TAXONOMY = load_search_taxonomy()
DEFAULT_KEYWORDS = LIFECYCLE_TAXONOMY.business_terms

PREDECESSOR_TERMS = ("health management systems", "dxc")
COMPETITOR_KEYS = set(LIFECYCLE_TAXONOMY.aliases_by_organization) - {"gainwell"}

TOPIC_RULES = [
    ("rht", "rht", ["rural health transformation", "rht"]),
    ("rural_health", "rural_health", ["rural health", "critical access hospital", "frontier", "rural hospital"]),
    ("medicaid", "medicaid", ["medicaid", "chip", "children's health insurance program", "1115", "1915"]),
    ("medicare", "medicare", ["medicare", "medicare-medicaid", "dual eligible", "dual enrollment"]),
    ("cms", "cms", ["cms", "centers for medicare", "centers for medicaid"]),
    ("mmis", "medicaid", ["mmis", "medicaid management information system"]),
    ("claims", "claims", ["claims", "encounter data", "payment system"]),
    ("eligibility", "eligibility", ["eligibility", "enrollment", "redetermination", "renewal"]),
    ("managed_care", "managed_care", ["managed care", "mco", "capitation"]),
    ("provider_data", "provider_data", ["provider data", "provider enrollment", "provider directory", "revalidation", "care compare"]),
    ("interoperability", "interoperability", ["interoperability", "fhir", "api", "health information exchange"]),
    ("prior_authorization", "interoperability", ["prior authorization", "prior auth"]),
    ("quality", "quality", ["quality", "quality measures", "star ratings"]),
    ("contact_center", "eligibility", ["contact center", "call center", "customer service"]),
    ("telehealth", "rural_health", ["telehealth", "telemedicine"]),
    ("behavioral_health", "rural_health", ["behavioral health", "mental health", "substance use"]),
    ("workforce", "rural_health", ["workforce", "staffing", "provider shortage"]),
]

PROGRAM_PRIORITY = [
    "rht",
    "rural_health",
    "medicaid",
    "medicare",
    "cms",
    "eligibility",
    "claims",
    "managed_care",
    "provider_data",
    "interoperability",
    "quality",
]

STATUS_ORDER = {
    "near_expiry": 0,
    "opportunity": 1,
    "award_notice": 2,
    "awarded": 3,
    "active": 4,
    "upcoming": 5,
    "expired": 6,
    "unknown": 7,
}

STATUS_MERGE_PRIORITY = {
    "near_expiry": 100,
    "active": 95,
    "expired": 90,
    "award_notice": 80,
    "awarded": 70,
    "opportunity": 60,
    "upcoming": 50,
    "unknown": 0,
}

SAM_OPPORTUNITY_PTYPES = {"a", "o", "k", "r", "p"}
SAM_CURRENT_OPPORTUNITY_STATUSES = {"active", "upcoming", "opportunity"}
ENTITY_SOURCE_KEY = "sam_entity_information"
COMPLETENESS_FIELDS = [
    "piid",
    "solicitation_number",
    "vendor_name",
    "uei_sam",
    "award_date",
    "period_start_date",
    "period_end_date",
    "award_amount",
    "current_total_value",
    "potential_total_value",
]


@dataclass(frozen=True)
class VendorMatch:
    vendor_key: str
    gwt_relation: str
    matched_alias: str


@dataclass(frozen=True)
class EntityMatch:
    vendor_key: str
    names: tuple[str, ...]
    uei_sam: str
    cage_code: str
    source_key: str
    source_record_id: str
    source_url: str
    match_confidence: str
    match_reason: str
    entity_status: str


@dataclass(frozen=True)
class EntityIndex:
    by_uei: dict[str, EntityMatch]
    by_vendor_key: dict[str, list[EntityMatch]]


@dataclass(frozen=True)
class LifecycleContext:
    keywords: list[str]
    vendor_aliases: dict[str, list[str]]
    recompete_months: int


@dataclass
class LifecycleBuildResult:
    rows: list[dict[str, str]]
    counts_by_status: dict[str, int]
    counts_by_vendor: dict[str, int]
    source_counts: dict[str, int]
    missing_optional_inputs: list[str]
    empty_optional_inputs: list[str]


class LifecycleBlockedError(RuntimeError):
    def __init__(self, message: str) -> None:
        super().__init__(message)


def build_contract_lifecycle(
    root: Path,
    *,
    contracts_path: str | Path = "data/contracts.csv",
    optional_inputs: dict[str, str | Path] | None = None,
    search_parameters_path: str | Path = "data/search_parameters.json",
    recompete_months: int | None = None,
    today: dt.date | None = None,
    progress: Callable[[str], None] | None = None,
) -> LifecycleBuildResult:
    """Build a federal contract lifecycle catalog from existing CSV output."""
    source_path = root / contracts_path
    if not source_path.exists():
        raise LifecycleBlockedError(f"missing required input: {contracts_path}")

    source_rows = read_csv(source_path)
    if not source_rows:
        raise LifecycleBlockedError(f"empty required input: {contracts_path}")

    input_paths = optional_inputs or DEFAULT_OPTIONAL_INPUTS
    context = load_context(root / search_parameters_path, recompete_months=recompete_months)
    missing_optional, empty_optional = optional_input_status(root, input_paths)
    run_date = today or dt.date.today()

    vendor_entities_path = input_paths.get("vendor_entities")
    vendor_entity_rows = read_optional_csv(root, vendor_entities_path) if vendor_entities_path else []
    entity_index = build_entity_index(vendor_entity_rows)
    if vendor_entity_rows:
        emit(progress, f"indexing {vendor_entities_path}: {len(vendor_entity_rows)} rows")

    emit(progress, f"normalizing {contracts_path}: {len(source_rows)} rows")
    rows = normalize_usaspending_rows(
        source_rows,
        source_file=str(contracts_path),
        context=context,
        today=run_date,
        missing_optional_inputs=missing_optional,
        entity_index=entity_index,
    )

    sam_awards_path = input_paths.get("sam_contract_awards")
    sam_awards_rows = read_optional_csv(root, sam_awards_path) if sam_awards_path else []
    if sam_awards_rows:
        emit(progress, f"normalizing {sam_awards_path}: {len(sam_awards_rows)} rows")
        rows.extend(
            normalize_sam_award_rows(
                sam_awards_rows,
                source_file=str(sam_awards_path),
                context=context,
                today=run_date,
                entity_index=entity_index,
            )
        )

    sam_opportunities_path = input_paths.get("sam_opportunities")
    sam_opportunity_rows = read_optional_csv(root, sam_opportunities_path) if sam_opportunities_path else []
    if sam_opportunity_rows:
        emit(progress, f"normalizing {sam_opportunities_path}: {len(sam_opportunity_rows)} rows")
        rows.extend(
            normalize_sam_opportunity_rows(
                sam_opportunity_rows,
                source_file=str(sam_opportunities_path),
                context=context,
                today=run_date,
                entity_index=entity_index,
            )
        )

    if not rows:
        raise LifecycleBlockedError(f"no usable lifecycle rows from {contracts_path} or optional SAM inputs")

    rows = sort_lifecycle_rows(dedupe_rows(rows))
    counts_by_status: dict[str, int] = {}
    counts_by_vendor: dict[str, int] = {}
    source_counts: dict[str, int] = {}
    for row in rows:
        counts_by_status[row["lifecycle_status"]] = counts_by_status.get(row["lifecycle_status"], 0) + 1
        counts_by_vendor[row["vendor_key"]] = counts_by_vendor.get(row["vendor_key"], 0) + 1
        for source_key in split_list(row.get("source_keys", "")):
            source_counts[source_key] = source_counts.get(source_key, 0) + 1

    return LifecycleBuildResult(
        rows=rows,
        counts_by_status=dict(sorted(counts_by_status.items())),
        counts_by_vendor=dict(sorted(counts_by_vendor.items())),
        source_counts=dict(sorted(source_counts.items())),
        missing_optional_inputs=missing_optional,
        empty_optional_inputs=empty_optional,
    )


def normalize_usaspending_rows(
    rows: list[dict[str, str]],
    *,
    source_file: str,
    context: LifecycleContext,
    today: dt.date,
    missing_optional_inputs: list[str],
    entity_index: EntityIndex,
) -> list[dict[str, str]]:
    output: list[dict[str, str]] = []
    for row in rows:
        source_record_id = value(row, "generated_internal_id", "award_id", "id")
        if not source_record_id:
            continue

        parsed_award = parse_generated_award_id(value(row, "generated_internal_id"))
        piid = value(row, "award_id") or parsed_award.get("piid", "")
        parent_award_id = parsed_award.get("parent_award_id", "")
        start_date = parse_date(value(row, "start_date"))
        end_date = parse_date(value(row, "end_date"))
        recompete_start = shift_months(end_date, -context.recompete_months) if end_date else None
        status = lifecycle_status(start_date, end_date, recompete_start, today)
        days_until_end = (end_date - today).days if end_date else None
        award_amount = money(value(row, "award_amount"))
        recipient_name = clean_text(value(row, "recipient_name", "vendor_name"), 240)
        vendor_match = resolve_vendor(row, context.vendor_aliases)
        entity_match = match_vendor_entity(vendor_match, recipient_name, "", entity_index)
        source_keys, source_record_ids = lifecycle_sources("usaspending", source_record_id, entity_match)
        title = clean_text(value(row, "description"), 500) or f"USAspending award {piid or source_record_id}"
        text = searchable_text(
            title,
            value(row, "matched_keywords"),
            value(row, "awarding_agency"),
            value(row, "awarding_sub_agency"),
            value(row, "naics_code"),
            value(row, "psc_code"),
            recipient_name,
        )
        topic_keys, program_focus = classify_text(text)
        matched_keywords = split_list(value(row, "matched_keywords")) or keyword_hits(text, context.keywords)
        importance_score = score_importance(
            source_score=int_or_none(value(row, "relevance_score")),
            amount=money_int(award_amount),
            status=status,
            days_until_end=days_until_end,
            topic_keys=topic_keys,
            agency=value(row, "awarding_agency"),
            vendor_match=vendor_match,
        )
        evidence = {
            "source_file": source_file,
            "source_key": "usaspending",
            "join_confidence": "medium_usaspending_with_entity" if entity_match else "usaspending_only_no_sam_entity_match",
            "source_relevance_score": int_or_none(value(row, "relevance_score")),
            "award_amount": money_int(award_amount),
            "days_until_end": days_until_end,
            "recompete_window_months": context.recompete_months,
            "recompete_signal": value(row, "recompete_signal"),
            "agency": value(row, "awarding_agency"),
            "subagency": value(row, "awarding_sub_agency"),
            "topic_hits": topic_keys,
            "matched_keywords": matched_keywords,
            "vendor_key": vendor_match.vendor_key,
            "gwt_relation": vendor_match.gwt_relation,
            "matched_vendor_alias": vendor_match.matched_alias,
            "missing_join_inputs": missing_optional_inputs,
            "vendor_entity": entity_evidence(entity_match),
        }
        lifecycle = lifecycle_row(
            contract_id=stable_contract_id("usaspending", source_record_id),
            source_keys=source_keys,
            source_record_ids=source_record_ids,
            lifecycle_status=status,
            contract_vehicle="task_order" if parent_award_id else "standalone_award",
            piid=piid,
            parent_award_id=parent_award_id,
            solicitation_number="",
            title=title,
            agency=clean_text(value(row, "awarding_agency"), 240),
            subagency=clean_text(value(row, "awarding_sub_agency"), 240),
            office="",
            vendor_key=vendor_match.vendor_key,
            vendor_name=recipient_name,
            uei_sam=entity_match.uei_sam if entity_match else "",
            incumbent_vendor_key=vendor_match.vendor_key if vendor_match.vendor_key != "unknown" else "",
            competitor_flag=vendor_match.gwt_relation == "competitor",
            gwt_relation=vendor_match.gwt_relation,
            known_bid_status=known_bid_status(vendor_match),
            award_date="",
            period_start_date=date_text(start_date),
            period_end_date=date_text(end_date),
            potential_end_date=date_text(end_date),
            recompete_window_start=date_text(recompete_start),
            days_until_end="" if days_until_end is None else str(days_until_end),
            award_amount=award_amount,
            current_total_value=award_amount,
            potential_total_value="",
            predictive_value_usd="",
            program_focus=program_focus,
            topic_keys=";".join(topic_keys),
            importance_score=str(importance_score),
            score_evidence_json="",
            source_urls=join_unique_text(value(row, "source_url"), entity_match.source_url if entity_match else ""),
            last_checked_at=now_iso(),
        )
        output.append(apply_evidence(lifecycle, evidence))
    return output


def normalize_sam_award_rows(
    rows: list[dict[str, str]],
    *,
    source_file: str,
    context: LifecycleContext,
    today: dt.date,
    entity_index: EntityIndex,
) -> list[dict[str, str]]:
    output: list[dict[str, str]] = []
    for row in rows:
        source_record_id = value(row, "source_record_id", "award_id", "piid")
        if not source_record_id:
            continue

        piid = value(row, "piid")
        parent_award_id = value(row, "parent_award_id")
        start_date = parse_date(value(row, "period_start_date"))
        period_end = parse_date(value(row, "period_end_date"))
        potential_end = parse_date(value(row, "potential_end_date"))
        end_date = potential_end or period_end
        recompete_start = parse_date(value(row, "recompete_window_start"))
        if end_date and not recompete_start:
            recompete_start = shift_months(end_date, -context.recompete_months)
        status = sam_award_lifecycle_status(row, start_date, end_date, recompete_start, today)
        days_until_end = (end_date - today).days if end_date else None

        vendor_match = resolve_sam_vendor(row, context.vendor_aliases)
        vendor_name = clean_text(value(row, "vendor_name"), 240)
        entity_match = match_vendor_entity(vendor_match, vendor_name, value(row, "uei_sam"), entity_index)
        source_keys, source_record_ids = lifecycle_sources("sam_contract_awards", source_record_id, entity_match)
        title = clean_text(value(row, "description"), 500) or f"SAM award {piid or source_record_id}"
        text = searchable_text(
            title,
            value(row, "agency"),
            value(row, "subagency"),
            value(row, "office"),
            value(row, "naics"),
            value(row, "psc"),
            vendor_name,
            value(row, "program_focus"),
            value(row, "topic_keys"),
        )
        topic_keys = split_list(value(row, "topic_keys")) or classify_text(text)[0]
        program_focus = first_list_item(value(row, "program_focus")) or classify_text(text)[1]
        award_amount = money(value(row, "award_amount"))
        current_total = money(value(row, "current_total_value"))
        potential_total = money(value(row, "potential_total_value"))
        predictive_value = money(value(row, "predictive_value_usd")) or best_money_text(
            potential_total, current_total, award_amount
        )
        score = int_or_none(value(row, "importance_score"))
        if score is None:
            score = score_importance(
                source_score=None,
                amount=money_int(predictive_value or current_total or award_amount),
                status=status,
                days_until_end=days_until_end,
                topic_keys=topic_keys,
                agency=value(row, "agency"),
                vendor_match=vendor_match,
            )
        evidence = {
            "source_file": source_file,
            "source_key": "sam_contract_awards",
            "join_confidence": "sam_contract_awards_with_entity" if entity_match else "sam_contract_awards_normalized",
            "source_record_id": source_record_id,
            "award_amount": money_int(award_amount),
            "current_total_value": money_int(current_total),
            "potential_total_value": money_int(potential_total),
            "days_until_end": days_until_end,
            "recompete_window_months": context.recompete_months,
            "agency": value(row, "agency"),
            "subagency": value(row, "subagency"),
            "office": value(row, "office"),
            "naics": value(row, "naics"),
            "psc": value(row, "psc"),
            "topic_hits": topic_keys,
            "vendor_key": vendor_match.vendor_key,
            "gwt_relation": vendor_match.gwt_relation,
            "vendor_entity": entity_evidence(entity_match),
        }
        lifecycle = lifecycle_row(
            contract_id=stable_contract_id("sam_contract_awards", source_record_id),
            source_keys=source_keys,
            source_record_ids=source_record_ids,
            lifecycle_status=status,
            contract_vehicle="task_order" if parent_award_id else "standalone_award",
            piid=piid,
            parent_award_id=parent_award_id,
            solicitation_number=value(row, "solicitation_number"),
            title=title,
            agency=clean_text(value(row, "agency"), 240),
            subagency=clean_text(value(row, "subagency"), 240),
            office=clean_text(value(row, "office"), 240),
            vendor_key=vendor_match.vendor_key,
            vendor_name=vendor_name,
            uei_sam=value(row, "uei_sam") or (entity_match.uei_sam if entity_match else ""),
            incumbent_vendor_key=vendor_match.vendor_key if vendor_match.vendor_key != "unknown" else "",
            competitor_flag=truthy(value(row, "competitor_flag")) or vendor_match.gwt_relation == "competitor",
            gwt_relation=vendor_match.gwt_relation,
            known_bid_status=known_bid_status(vendor_match),
            award_date=value(row, "award_date"),
            period_start_date=date_text(start_date),
            period_end_date=date_text(period_end),
            potential_end_date=date_text(potential_end),
            recompete_window_start=date_text(recompete_start),
            days_until_end="" if days_until_end is None else str(days_until_end),
            award_amount=award_amount,
            current_total_value=current_total,
            potential_total_value=potential_total,
            predictive_value_usd=predictive_value,
            program_focus=program_focus,
            topic_keys=";".join(topic_keys),
            importance_score=str(score),
            score_evidence_json="",
            source_urls=join_unique_text(value(row, "document_url"), entity_match.source_url if entity_match else ""),
            last_checked_at=value(row, "last_checked_at") or now_iso(),
        )
        output.append(apply_evidence(lifecycle, evidence))
    return output


def normalize_sam_opportunity_rows(
    rows: list[dict[str, str]],
    *,
    source_file: str,
    context: LifecycleContext,
    today: dt.date,
    entity_index: EntityIndex,
) -> list[dict[str, str]]:
    output: list[dict[str, str]] = []
    for row in rows:
        source_record_id = value(row, "sam_notice_id", "opportunity_id", "solicitation_number")
        if not source_record_id:
            continue

        ptype = value(row, "ptype").lower()
        award_details = sam_opportunity_award_details(row)
        award_number = award_details.get("award_number", "")
        solicitation_number = value(row, "solicitation_number")
        piid = award_number if ptype == "a" else ""
        status = sam_opportunity_status(row, ptype, today)
        due_date = parse_date(value(row, "due_date"))
        archive_date = parse_date(value(row, "archive_date"))
        days_basis = due_date or archive_date
        days_until_end = (days_basis - today).days if days_basis and status == "opportunity" else None

        award_amount = money(award_details.get("award_amount", ""))
        award_date = date_text(parse_date(award_details.get("award_date", "")))
        vendor_name = clean_text(award_details.get("vendor_name", ""), 240)
        vendor_match = resolve_opportunity_vendor(row, vendor_name, context.vendor_aliases)
        entity_match = match_vendor_entity(vendor_match, vendor_name, award_details.get("uei_sam", ""), entity_index)
        source_keys, source_record_ids = lifecycle_sources("sam_opportunities", source_record_id, entity_match)
        agency, subagency, office = opportunity_agency_parts(row)
        title = clean_text(value(row, "title"), 500) or f"SAM opportunity {solicitation_number or source_record_id}"
        text = searchable_text(
            title,
            value(row, "summary"),
            agency,
            subagency,
            office,
            value(row, "notice_type"),
            value(row, "naics"),
            value(row, "psc"),
            value(row, "program_focus"),
            value(row, "topic_keys"),
            vendor_name,
        )
        classified_topics, classified_program = classify_text(text)
        topic_keys = split_list(value(row, "topic_keys")) or classified_topics
        program_focus = first_list_item(value(row, "program_focus")) or classified_program
        score = int_or_none(value(row, "importance_score"))
        if score is None:
            score = score_importance(
                source_score=None,
                amount=money_int(award_amount),
                status=status,
                days_until_end=days_until_end,
                topic_keys=topic_keys,
                agency=agency,
                vendor_match=vendor_match,
            )
        bid_status = "award_notice" if ptype == "a" else ("open_opportunity" if status == "opportunity" else known_bid_status(vendor_match))
        evidence = {
            "source_file": source_file,
            "source_key": "sam_opportunities",
            "join_confidence": "sam_award_notice" if ptype == "a" else "sam_opportunity_notice",
            "source_record_id": source_record_id,
            "ptype": ptype,
            "notice_type": value(row, "notice_type"),
            "notice_bucket": value(row, "notice_bucket"),
            "source_lifecycle_status": value(row, "lifecycle_status"),
            "notice_dates": {
                "posted_date": value(row, "posted_date"),
                "due_date": value(row, "due_date"),
                "archive_date": value(row, "archive_date"),
            },
            "join_keys": {
                "piid": piid,
                "solicitation_number": solicitation_number,
                "source_record_id": source_record_id,
            },
            "award_notice": {
                "award_number": award_number,
                "award_amount": money_int(award_amount),
                "award_date": award_date,
                "vendor_name": vendor_name,
                "uei_sam": award_details.get("uei_sam", ""),
                "cage_code": award_details.get("cage_code", ""),
            },
            "topic_hits": topic_keys,
            "vendor_key": vendor_match.vendor_key,
            "gwt_relation": vendor_match.gwt_relation,
            "vendor_entity": entity_evidence(entity_match),
        }
        lifecycle = lifecycle_row(
            contract_id=stable_contract_id("sam_opportunities", source_record_id),
            source_keys=source_keys,
            source_record_ids=source_record_ids,
            lifecycle_status=status,
            contract_vehicle="award_notice" if ptype == "a" else "opportunity_notice",
            piid=piid,
            parent_award_id="",
            solicitation_number=solicitation_number,
            title=title,
            agency=agency,
            subagency=subagency,
            office=office,
            vendor_key=vendor_match.vendor_key,
            vendor_name=vendor_name,
            uei_sam=award_details.get("uei_sam", "") or (entity_match.uei_sam if entity_match else ""),
            incumbent_vendor_key=vendor_match.vendor_key if vendor_match.vendor_key != "unknown" else "",
            competitor_flag=vendor_match.gwt_relation == "competitor",
            gwt_relation=vendor_match.gwt_relation,
            known_bid_status=bid_status,
            award_date=award_date,
            period_start_date="",
            period_end_date="",
            potential_end_date="",
            recompete_window_start="",
            days_until_end="" if days_until_end is None else str(days_until_end),
            award_amount=award_amount,
            current_total_value=award_amount,
            potential_total_value=award_amount,
            predictive_value_usd=award_amount,
            program_focus=program_focus,
            topic_keys=";".join(topic_keys),
            importance_score=str(score),
            score_evidence_json="",
            source_urls=join_unique_text(
                value(row, "document_url"),
                value(row, "source_url"),
                value(row, "summary"),
                entity_match.source_url if entity_match else "",
            ),
            last_checked_at=value(row, "last_checked_at") or now_iso(),
        )
        output.append(apply_evidence(lifecycle, evidence))
    return output


def resolve_sam_vendor(row: dict[str, str], aliases: dict[str, list[str]]) -> VendorMatch:
    vendor_key = value(row, "vendor_key") or "unknown"
    relation = value(row, "gwt_relation")
    if vendor_key and vendor_key != "unknown":
        if not relation or relation == "unknown":
            if vendor_key == "gainwell_technologies":
                relation = "self"
            elif vendor_key in COMPETITOR_KEYS:
                relation = "competitor"
            else:
                relation = "unknown"
        return VendorMatch(vendor_key, relation, "")
    vendor_name = value(row, "vendor_name")
    return resolve_vendor({"vendor_name": vendor_name, "recipient_name": vendor_name}, aliases)


def resolve_opportunity_vendor(row: dict[str, str], vendor_name: str, aliases: dict[str, list[str]]) -> VendorMatch:
    if vendor_name:
        match = resolve_vendor({"vendor_name": vendor_name, "recipient_name": vendor_name}, aliases)
        if match.vendor_key != "unknown":
            return match
    for vendor_key in split_list(value(row, "vendor_keys_mentioned")):
        relation = relation_for_vendor_key(vendor_key)
        return VendorMatch(vendor_key, relation, "")
    return VendorMatch("unknown", "unknown", "")


def relation_for_vendor_key(vendor_key: str) -> str:
    if vendor_key == "gainwell_technologies":
        return "self"
    if vendor_key in COMPETITOR_KEYS:
        return "competitor"
    return "unknown"


def sam_award_lifecycle_status(
    row: dict[str, str],
    start_date: dt.date | None,
    end_date: dt.date | None,
    recompete_start: dt.date | None,
    today: dt.date,
) -> str:
    if end_date:
        return lifecycle_status(start_date, end_date, recompete_start, today)
    source_status = value(row, "lifecycle_status")
    if source_status in {"awarded", "award_notice"}:
        return source_status
    if start_date or parse_date(value(row, "award_date")):
        return "awarded"
    return "unknown"


def sam_opportunity_status(row: dict[str, str], ptype: str, today: dt.date) -> str:
    if ptype == "a":
        return "award_notice"
    source_status = value(row, "lifecycle_status").lower()
    if source_status in SAM_CURRENT_OPPORTUNITY_STATUSES:
        return "opportunity"
    if source_status == "expired":
        return "expired"
    archive_date = parse_date(value(row, "archive_date"))
    due_date = parse_date(value(row, "due_date"))
    if archive_date and archive_date < today:
        return "expired"
    if due_date and due_date < today and not archive_date:
        return "expired"
    if ptype in SAM_OPPORTUNITY_PTYPES:
        return "opportunity"
    return "unknown"


def sam_opportunity_award_details(row: dict[str, str]) -> dict[str, str]:
    raw = json_object(value(row, "raw_json"))
    award = raw.get("award") if isinstance(raw.get("award"), dict) else {}
    awardee = award.get("awardee") if isinstance(award.get("awardee"), dict) else {}
    return {
        "award_number": scalar_text(award.get("number")),
        "award_amount": scalar_text(award.get("amount")),
        "award_date": scalar_text(award.get("date")),
        "vendor_name": scalar_text(awardee.get("name")),
        "uei_sam": scalar_text(awardee.get("ueiSAM") or awardee.get("uei")),
        "cage_code": scalar_text(awardee.get("cageCode") or awardee.get("cage")),
    }


def opportunity_agency_parts(row: dict[str, str]) -> tuple[str, str, str]:
    agency = clean_text(value(row, "agency"), 240)
    subagency = clean_text(value(row, "subagency"), 240)
    office = clean_text(value(row, "office"), 240)
    if subagency or office:
        return agency, subagency, office
    parts = [part.strip() for part in agency.split(".") if part.strip()]
    if len(parts) >= 3:
        return clean_text(parts[0], 240), clean_text(parts[1], 240), clean_text(".".join(parts[2:]), 240)
    if len(parts) == 2:
        return clean_text(parts[0], 240), clean_text(parts[1], 240), ""
    return agency, subagency, office


def read_optional_csv(root: Path, relative_path: str | Path) -> list[dict[str, str]]:
    if not relative_path:
        return []
    path = root / relative_path
    if not path.exists() or path.is_dir() or path.stat().st_size == 0:
        return []
    return read_csv(path)


def first_list_item(value_text: str) -> str:
    values = split_list(value_text)
    return values[0] if values else ""


def best_money_text(*values: str) -> str:
    parsed = [amount for amount in (money_int(value) for value in values) if amount is not None]
    return str(max(parsed)) if parsed else ""


def truthy(value_text: str) -> bool:
    return value_text.strip().lower() in {"1", "true", "yes", "y"}


def load_context(path: Path, *, recompete_months: int | None = None) -> LifecycleContext:
    taxonomy = load_search_taxonomy(path)
    params = dict(taxonomy.raw_parameters)
    usaspending = params.get("usaspending") if isinstance(params.get("usaspending"), dict) else {}
    configured_months = int_or_none(usaspending.get("recompete_months")) if usaspending else None
    aliases = {
        "gainwell_technologies" if key == "gainwell" else key: list(values)
        for key, values in taxonomy.aliases_by_organization.items()
    }
    return LifecycleContext(
        keywords=taxonomy.business_terms,
        vendor_aliases=aliases or vendor_alias_map(params),
        recompete_months=recompete_months or configured_months or 36,
    )


def vendor_alias_map(params: dict[str, Any]) -> dict[str, list[str]]:
    aliases = {
        "gainwell_technologies" if key == "gainwell" else key: list(values)
        for key, values in LIFECYCLE_TAXONOMY.aliases_by_organization.items()
    }
    for item in params.get("vendors") or []:
        if isinstance(item, str):
            key = vendor_key_for_name(item)
            aliases.setdefault(key, []).append(item)
            continue
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        key = vendor_key_for_name(name)
        aliases.setdefault(key, []).append(name)
        aliases[key].extend(str(alias).strip() for alias in item.get("aliases") or [] if str(alias).strip())
    return {key: sorted(set(values), key=str.lower) for key, values in aliases.items()}


def resolve_vendor(row: dict[str, str], aliases: dict[str, list[str]]) -> VendorMatch:
    candidates = [value(row, "vendor_name"), value(row, "vendor_query"), value(row, "recipient_name")]
    normalized_candidates = [normalize_name(candidate) for candidate in candidates if candidate]
    best: tuple[int, str, str] | None = None
    for vendor_key, vendor_aliases in aliases.items():
        for alias in vendor_aliases:
            normalized_alias = normalize_name(alias)
            if not normalized_alias:
                continue
            for candidate in normalized_candidates:
                if normalized_alias == candidate or normalized_alias in candidate or candidate in normalized_alias:
                    score = len(normalized_alias)
                    if best is None or score > best[0]:
                        best = (score, vendor_key, alias)
    if best is None:
        return VendorMatch("unknown", "unknown", "")

    _, vendor_key, alias = best
    relation = "unknown"
    if vendor_key == "gainwell_technologies":
        relation = "predecessor_alias" if any(term in normalize_name(alias) for term in PREDECESSOR_TERMS) else "self"
    elif vendor_key in COMPETITOR_KEYS:
        relation = "competitor"
    return VendorMatch(vendor_key, relation, alias)


def vendor_key_for_name(name: str) -> str:
    override = {
        "gainwell technologies": "gainwell_technologies",
        "maximus": "maximus",
        "deloitte": "deloitte",
        "accenture federal services": "accenture_federal_services",
        "optum": "optum",
        "conduent": "conduent",
    }.get(name.strip().lower())
    return override or re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", name.lower())).strip("_") or "unknown"


def lifecycle_status(
    start_date: dt.date | None,
    end_date: dt.date | None,
    recompete_start: dt.date | None,
    today: dt.date,
) -> str:
    if end_date and end_date < today:
        return "expired"
    if start_date and start_date > today:
        return "awarded"
    if start_date and end_date and start_date <= today <= end_date:
        if recompete_start and today >= recompete_start:
            return "near_expiry"
        return "active"
    return "unknown"


def classify_text(text: str) -> tuple[list[str], str]:
    lower = text.lower()
    topics: list[str] = []
    programs: list[str] = []
    for topic, program, terms in TOPIC_RULES:
        if any(term in lower for term in terms):
            topics.append(topic)
            programs.append(program)
    topics = sorted(set(topics), key=lambda item: [topic for topic, _, _ in TOPIC_RULES].index(item))
    program_focus = "unknown"
    for candidate in PROGRAM_PRIORITY:
        if candidate in programs:
            program_focus = candidate
            break
    return topics, program_focus


def score_importance(
    *,
    source_score: int | None,
    amount: int | None,
    status: str,
    days_until_end: int | None,
    topic_keys: list[str],
    agency: str,
    vendor_match: VendorMatch,
) -> int:
    score = max(0, min(100, source_score or 0))
    if status == "near_expiry":
        score += 12
    elif status == "opportunity":
        score += 10
    elif status in {"award_notice", "awarded"}:
        score += 8
    elif status == "active":
        score += 5
    if amount is not None:
        if amount >= 50_000_000:
            score += 10
        elif amount >= 10_000_000:
            score += 6
        elif amount >= 1_000_000:
            score += 3
    if days_until_end is not None and 0 <= days_until_end <= 365:
        score += 6
    if vendor_match.gwt_relation in {"self", "predecessor_alias", "competitor"}:
        score += 5
    if any(topic in topic_keys for topic in ("medicaid", "medicare", "cms", "rht", "rural_health")):
        score += 5
    if "health and human services" in agency.lower() or "medicare" in agency.lower() or "medicaid" in agency.lower():
        score += 4
    return min(score, 100)


def known_bid_status(vendor_match: VendorMatch) -> str:
    if vendor_match.gwt_relation in {"self", "predecessor_alias"}:
        return "incumbent"
    if vendor_match.gwt_relation == "competitor":
        return "competitor_awarded"
    if vendor_match.vendor_key != "unknown":
        return "awarded"
    return "unknown"


def parse_generated_award_id(value: str) -> dict[str, str]:
    if not value.startswith("CONT_AWD_"):
        return {}
    parts = value[len("CONT_AWD_") :].split("_")
    parent_award_id = parts[2] if len(parts) > 2 else ""
    if parent_award_id.upper() in {"", "NONE", "-NONE-"}:
        parent_award_id = ""
    return {
        "piid": parts[0] if parts else "",
        "parent_award_id": parent_award_id,
    }


def build_entity_index(rows: list[dict[str, str]]) -> EntityIndex:
    by_uei: dict[str, EntityMatch] = {}
    by_vendor_key: dict[str, list[EntityMatch]] = {}
    for row in rows:
        uei_sam = value(row, "uei_sam", "source_record_id")
        if not uei_sam:
            continue
        names = tuple(
            unique_texts(
                [
                    value(row, "matched_name"),
                    value(row, "legal_business_name"),
                    value(row, "dba_name"),
                    value(row, "search_name"),
                ]
            )
        )
        match = EntityMatch(
            vendor_key=value(row, "vendor_key") or "unknown",
            names=names,
            uei_sam=uei_sam,
            cage_code=value(row, "cage_code"),
            source_key=value(row, "source_key") or ENTITY_SOURCE_KEY,
            source_record_id=value(row, "source_record_id") or uei_sam,
            source_url=value(row, "sam_entity_url"),
            match_confidence=value(row, "match_confidence"),
            match_reason=value(row, "match_reason"),
            entity_status=value(row, "entity_status"),
        )
        by_uei[normalize_identifier(uei_sam)] = match
        by_vendor_key.setdefault(match.vendor_key, []).append(match)
    for matches in by_vendor_key.values():
        matches.sort(key=lambda item: (item.entity_status.lower() == "active", float_or_zero(item.match_confidence)), reverse=True)
    return EntityIndex(by_uei=by_uei, by_vendor_key=by_vendor_key)


def match_vendor_entity(
    vendor_match: VendorMatch,
    vendor_name: str,
    uei_sam: str,
    entity_index: EntityIndex,
) -> EntityMatch | None:
    if uei_sam:
        entity_match = entity_index.by_uei.get(normalize_identifier(uei_sam))
        if entity_match:
            return entity_match
    if vendor_match.vendor_key == "unknown":
        return None
    candidates = entity_index.by_vendor_key.get(vendor_match.vendor_key, [])
    if not candidates:
        return None
    scored = [(entity_name_score(vendor_name or vendor_match.matched_alias, candidate), candidate) for candidate in candidates]
    score, candidate = max(scored, key=lambda item: item[0])
    if score >= 80:
        return candidate
    if len(candidates) == 1 and score >= 50:
        return candidate
    return None


def entity_name_score(vendor_name: str, entity: EntityMatch) -> int:
    candidate = normalize_name(vendor_name)
    if not candidate:
        return 0
    best = 0
    candidate_tokens = set(candidate.split())
    for name in entity.names:
        normalized = normalize_name(name)
        if not normalized:
            continue
        if normalized == candidate:
            return 100
        if normalized in candidate or candidate in normalized:
            best = max(best, 85 if min(len(normalized), len(candidate)) >= 8 else 50)
            continue
        entity_tokens = set(normalized.split())
        overlap = candidate_tokens & entity_tokens
        if overlap:
            best = max(best, int(60 * len(overlap) / max(len(candidate_tokens), len(entity_tokens), 1)))
    return best


def entity_evidence(entity_match: EntityMatch | None) -> dict[str, Any]:
    if not entity_match:
        return {"matched": False}
    return {
        "matched": True,
        "source_key": entity_match.source_key,
        "source_record_id": entity_match.source_record_id,
        "uei_sam": entity_match.uei_sam,
        "cage_code": entity_match.cage_code,
        "match_confidence": entity_match.match_confidence,
        "match_reason": entity_match.match_reason,
        "entity_status": entity_match.entity_status,
    }


def lifecycle_sources(source_key: str, source_record_id: str, entity_match: EntityMatch | None) -> tuple[str, str]:
    source_keys = [source_key]
    source_record_ids = [source_record_id]
    if entity_match:
        source_keys.append(entity_match.source_key)
        source_record_ids.append(entity_match.source_record_id)
    return join_unique_text(*source_keys), join_unique_text(*source_record_ids)


def apply_evidence(row: dict[str, str], evidence: dict[str, Any]) -> dict[str, str]:
    evidence = dict(evidence)
    evidence.setdefault(
        "join_keys",
        {
            "piid": row.get("piid", ""),
            "solicitation_number": row.get("solicitation_number", ""),
            "source_record_ids": split_list(row.get("source_record_ids", "")),
        },
    )
    evidence["missing_fields"] = missing_lifecycle_fields(row)
    row["score_evidence_json"] = json.dumps(evidence, sort_keys=True, separators=(",", ":"))
    return row


def missing_lifecycle_fields(row: dict[str, str]) -> list[str]:
    return [field for field in COMPLETENESS_FIELDS if not row.get(field)]


def optional_input_status(root: Path, optional_inputs: dict[str, str | Path]) -> tuple[list[str], list[str]]:
    missing: list[str] = []
    empty: list[str] = []
    for relative_path in optional_inputs.values():
        if not relative_path:
            continue
        path = root / relative_path
        display = str(relative_path)
        if not path.exists():
            missing.append(display)
        elif path.is_dir() or path.stat().st_size == 0:
            empty.append(display)
    return missing, empty


def dedupe_rows(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    by_key: dict[str, dict[str, str]] = {}
    for row in rows:
        key = dedupe_key(row)
        if not key:
            continue
        if key in by_key:
            by_key[key] = merge_lifecycle_rows(by_key[key], row, key)
        else:
            by_key[key] = dict(row)
    return [finalize_evidence(row, key) for key, row in by_key.items()]


def dedupe_key(row: dict[str, str]) -> str:
    piid = normalize_identifier(row.get("piid", ""))
    if piid:
        return f"piid:{piid}"
    solicitation_number = normalize_identifier(row.get("solicitation_number", ""))
    if solicitation_number:
        return f"solicitation:{solicitation_number}"
    for source_record_id in split_list(row.get("source_record_ids", "")):
        if source_record_id:
            return f"source_record:{source_record_id}"
    return f"contract:{row.get('contract_id', '')}"


def merge_lifecycle_rows(left: dict[str, str], right: dict[str, str], join_key: str) -> dict[str, str]:
    merged = dict(left)
    merged["source_keys"] = join_unique_text(left.get("source_keys", ""), right.get("source_keys", ""))
    merged["source_record_ids"] = join_unique_text(left.get("source_record_ids", ""), right.get("source_record_ids", ""))
    merged["source_urls"] = join_unique_text(left.get("source_urls", ""), right.get("source_urls", ""))
    for field in LIFECYCLE_FIELDS:
        if field in {"contract_id", "source_keys", "source_record_ids", "score_evidence_json", "source_urls"}:
            continue
        merged[field] = choose_merge_value(field, merged.get(field, ""), right.get(field, ""))
    merged["score_evidence_json"] = merge_evidence_json(left, right, merged, join_key)
    return merged


def choose_merge_value(field: str, current: str, incoming: str) -> str:
    if field == "lifecycle_status":
        return choose_status(current, incoming)
    if field == "importance_score":
        return str(max(int_or_none(current) or 0, int_or_none(incoming) or 0))
    if field == "competitor_flag":
        return "true" if truthy(current) or truthy(incoming) else "false"
    if field == "topic_keys":
        return join_unique_text(current, incoming)
    if field in {"award_amount", "current_total_value", "potential_total_value", "predictive_value_usd"}:
        return best_money_text(current, incoming)
    if field == "last_checked_at":
        return max(current, incoming)
    if field == "title":
        if not current or placeholder_title(current):
            return incoming or current
        return current
    if field in {"vendor_key", "gwt_relation", "known_bid_status", "program_focus"}:
        if current in {"", "unknown"} and incoming not in {"", "unknown"}:
            return incoming
        return current or incoming
    return current or incoming


def choose_status(current: str, incoming: str) -> str:
    current = current or "unknown"
    incoming = incoming or "unknown"
    current_score = STATUS_MERGE_PRIORITY.get(current, 0)
    incoming_score = STATUS_MERGE_PRIORITY.get(incoming, 0)
    return incoming if incoming_score > current_score else current


def placeholder_title(title: str) -> bool:
    return title.startswith(("SAM award ", "SAM opportunity ", "USAspending award "))


def merge_evidence_json(left: dict[str, str], right: dict[str, str], merged: dict[str, str], join_key: str) -> str:
    evidence = {
        "join_confidence": merge_join_confidence(join_key, merged),
        "join_key": join_key,
        "source_keys": split_list(merged.get("source_keys", "")),
        "source_record_ids": split_list(merged.get("source_record_ids", "")),
        "missing_fields": missing_lifecycle_fields(merged),
        "source_evidence": evidence_items(left) + evidence_items(right),
    }
    return json.dumps(evidence, sort_keys=True, separators=(",", ":"))


def evidence_items(row: dict[str, str]) -> list[dict[str, Any]]:
    evidence = json_object(row.get("score_evidence_json", ""))
    items = evidence.get("source_evidence")
    if isinstance(items, list):
        return [item for item in items if isinstance(item, dict)]
    return [evidence] if evidence else []


def merge_join_confidence(join_key: str, row: dict[str, str]) -> str:
    lifecycle_sources_count = len([source for source in split_list(row.get("source_keys", "")) if source != ENTITY_SOURCE_KEY])
    suffix = "multi_source" if lifecycle_sources_count > 1 else "single_source"
    if join_key.startswith("piid:"):
        return f"high_piid_{suffix}"
    if join_key.startswith("solicitation:"):
        return f"medium_solicitation_{suffix}"
    return f"source_record_{suffix}"


def finalize_evidence(row: dict[str, str], join_key: str) -> dict[str, str]:
    evidence = json_object(row.get("score_evidence_json", ""))
    evidence.setdefault("join_confidence", merge_join_confidence(join_key, row))
    evidence.setdefault("join_key", join_key)
    evidence.setdefault("source_keys", split_list(row.get("source_keys", "")))
    evidence["missing_fields"] = missing_lifecycle_fields(row)
    row["score_evidence_json"] = json.dumps(evidence, sort_keys=True, separators=(",", ":"))
    return row


def sort_lifecycle_rows(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    return sorted(
        rows,
        key=lambda row: (
            STATUS_ORDER.get(row.get("lifecycle_status", "unknown"), 99),
            days_sort_value(row.get("days_until_end")),
            row.get("agency", ""),
            row.get("vendor_key", ""),
            -money_int(row.get("current_total_value")) if money_int(row.get("current_total_value")) is not None else 0,
        ),
    )


def lifecycle_row(**values: Any) -> dict[str, str]:
    row: dict[str, str] = {}
    for field in LIFECYCLE_FIELDS:
        value = values.get(field, "")
        if isinstance(value, bool):
            row[field] = "true" if value else "false"
        elif value is None:
            row[field] = ""
        else:
            row[field] = str(value)
    return row


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def write_lifecycle(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.tmp")
    with tmp_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=LIFECYCLE_FIELDS, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    tmp_path.replace(path)


def stable_contract_id(source_key: str, source_record_id: str) -> str:
    digest = hashlib.sha1(f"{source_key}|{source_record_id}".encode("utf-8")).hexdigest()[:16]
    return f"contract-lifecycle-{digest}"


def searchable_text(*parts: Any) -> str:
    return " ".join(clean_text(part, 2000) for part in parts if part)


def keyword_hits(text: str, keywords: list[str]) -> list[str]:
    lower = text.lower()
    return sorted({keyword for keyword in keywords if keyword and keyword.lower() in lower}, key=str.lower)


def split_list(value: str) -> list[str]:
    return [item.strip() for item in re.split(r"[;,]", value or "") if item.strip()]


def join_unique_text(*values: str) -> str:
    return ";".join(unique_texts(item for value in values for item in split_list(value)))


def unique_texts(values: Any) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for value_text in values:
        text = str(value_text or "").strip()
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        output.append(text)
    return output


def normalize_identifier(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower()).upper()


def json_object(value_text: str) -> dict[str, Any]:
    if not value_text:
        return {}
    try:
        parsed = json.loads(value_text)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def scalar_text(value: Any) -> str:
    if isinstance(value, (dict, list)) or value is None:
        return ""
    return str(value).strip()


def float_or_zero(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def value(row: dict[str, Any], *keys: str) -> str:
    for key in keys:
        item = row.get(key)
        if item is not None and str(item).strip():
            return str(item).strip()
    return ""


def clean_text(value: Any, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "..."


def normalize_name(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", value.lower())).strip()


def parse_date(value: Any) -> dt.date | None:
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return dt.datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    try:
        return dt.datetime.fromisoformat(text).date()
    except ValueError:
        return None


def date_text(value: dt.date | None) -> str:
    return value.isoformat() if value else ""


def shift_months(value: dt.date, months: int) -> dt.date:
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return dt.date(year, month, day)


def money(value: Any) -> str:
    amount = money_int(value)
    return "" if amount is None else str(amount)


def money_int(value: Any) -> int | None:
    text = str(value or "").replace(",", "").replace("$", "").strip()
    if not text:
        return None
    try:
        return int(round(float(text)))
    except ValueError:
        return None


def int_or_none(value: Any) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def days_sort_value(value: str) -> int:
    parsed = int_or_none(value)
    return parsed if parsed is not None else 999999


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def emit(progress: Callable[[str], None] | None, message: str) -> None:
    if progress:
        progress(message)
