from __future__ import annotations

import csv
import datetime as dt
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

CATALOG_FIELDS = [
    "update_id",
    "source_key",
    "source_record_id",
    "record_type",
    "title",
    "agency",
    "program_focus",
    "topic_keys",
    "posted_date",
    "updated_date",
    "due_date",
    "effective_date",
    "docket_id",
    "regulation_id",
    "grant_id",
    "opportunity_id",
    "contract_id",
    "vendor_keys_mentioned",
    "rht_flag",
    "comment_required_flag",
    "action_required_by",
    "importance_score",
    "predictive_value_usd",
    "score_evidence_json",
    "summary",
    "document_url",
    "source_url",
    "last_checked_at",
]

DEFAULT_INPUTS = {
    "federal_register": "data/federal_register_updates.csv",
    "regulations": "data/regulations_updates.csv",
    "grants": "data/federal_grants.csv",
    "sam_opportunities": "data/federal_opportunities.csv",
    "cms_provider": "data/cms_provider_data.csv",
    "medicaid_data": "data/medicaid_data.csv",
    "dataset_signals": "data/cms_medicaid_dataset_signals.csv",
}

SOURCE_KEY_MAP = {
    "federal register": "federal_register",
    "regulations.gov": "regulations",
    "regulations": "regulations",
    "grants.gov": "grants",
    "grants": "grants",
    "sam": "sam_opportunities",
    "sam.gov": "sam_opportunities",
    "sam opportunities": "sam_opportunities",
    "sam_opportunities": "sam_opportunities",
    "cms": "cms_data",
    "cms provider data": "cms_data",
    "cms_data": "cms_data",
    "medicaid": "medicaid_data",
    "medicaid_data": "medicaid_data",
}

TOPIC_RULES = [
    ("rht", "rht", ["rural health transformation", "rht"]),
    ("rural_health", "rural_health", ["rural health", "critical access hospital", "frontier", "rural hospital"]),
    ("medicaid", "medicaid", ["medicaid", "chip", "children's health insurance program", "1115", "1915"]),
    ("medicare", "medicare", ["medicare", "medicare-medicaid", "dual eligible", "dual enrollment"]),
    ("cms", "cms", ["cms", "centers for medicare", "centers for medicaid"]),
    ("mmis", "medicaid", ["mmis", "medicaid management information system"]),
    ("claims", "claims", ["claims", "encounter data", "payment system", "prospective payment"]),
    ("eligibility", "eligibility", ["eligibility", "enrollment", "redetermination", "renewal"]),
    ("managed_care", "managed_care", ["managed care", "mco", "capitation", "plan rating"]),
    ("provider_data", "provider_data", ["provider data", "provider enrollment", "provider directory", "revalidation", "care compare"]),
    ("interoperability", "interoperability", ["interoperability", "fhir", "api", "health information exchange"]),
    ("prior_authorization", "interoperability", ["prior authorization", "prior auth"]),
    ("quality", "quality", ["quality", "quality measures", "star ratings", "measure set"]),
    ("waiver", "medicaid", ["waiver", "section 1115", "1915(c)", "spa", "state plan amendment"]),
    ("contact_center", "eligibility", ["contact center", "call center", "customer service"]),
    ("telehealth", "rural_health", ["telehealth", "telemedicine"]),
    ("behavioral_health", "rural_health", ["behavioral health", "mental health", "substance use"]),
    ("workforce", "rural_health", ["workforce", "staffing", "provider shortage"]),
    ("grant", "rht", ["grant", "funding opportunity", "cooperative agreement"]),
]

RHT_TERMS = {
    "rural health transformation",
    "rural health",
    "critical access hospital",
    "frontier",
    "rural hospital",
    "telehealth",
    "provider shortage",
}

TRUE_VALUES = {"1", "true", "yes", "y", "open"}
FALSE_VALUES = {"0", "false", "no", "n", "closed"}


@dataclass
class CatalogBuildResult:
    rows: list[dict[str, str]]
    counts_by_source: dict[str, int]
    missing_inputs: list[str]
    empty_inputs: list[str]
    weak_inputs: list[str]


class CatalogBlockedError(RuntimeError):
    def __init__(self, missing_inputs: list[str], empty_inputs: list[str]) -> None:
        pieces = []
        if missing_inputs:
            pieces.append("missing inputs: " + ", ".join(missing_inputs))
        if empty_inputs:
            pieces.append("empty inputs: " + ", ".join(empty_inputs))
        message = "No usable source outputs exist"
        if pieces:
            message += " (" + "; ".join(pieces) + ")"
        super().__init__(message)
        self.missing_inputs = missing_inputs
        self.empty_inputs = empty_inputs


@dataclass(frozen=True)
class CatalogContext:
    monitored_keywords: list[str]
    vendor_aliases: dict[str, list[str]]


def build_federal_update_catalog(
    root: Path,
    input_paths: dict[str, str | Path] | None = None,
    search_parameters_path: str | Path = "data/search_parameters.json",
    progress: Callable[[str], None] | None = None,
) -> CatalogBuildResult:
    paths = dict(DEFAULT_INPUTS)
    if input_paths:
        paths.update({key: str(value) for key, value in input_paths.items()})

    context = load_context(root / search_parameters_path)
    all_rows: list[dict[str, str]] = []
    missing_inputs: list[str] = []
    empty_inputs: list[str] = []
    weak_inputs: list[str] = []

    normalizers = {
        "federal_register": normalize_federal_register_rows,
        "regulations": normalize_regulations_rows,
        "grants": normalize_grant_rows,
        "sam_opportunities": normalize_opportunity_rows,
        "cms_provider": normalize_cms_provider_rows,
        "medicaid_data": normalize_medicaid_rows,
        "dataset_signals": normalize_dataset_signal_rows,
    }

    for source_name, relative_path in paths.items():
        path = root / relative_path
        display_path = str(relative_path)
        if not path.exists():
            missing_inputs.append(display_path)
            continue
        source_rows = read_csv(path)
        if not source_rows:
            empty_inputs.append(display_path)
            continue
        emit(progress, f"normalizing {display_path}: {len(source_rows)} rows")
        normalizer = normalizers[source_name]
        normalized = normalizer(source_rows, display_path, context)
        if not normalized:
            weak_inputs.append(display_path)
            continue
        all_rows.extend(normalized)

    if not all_rows:
        raise CatalogBlockedError(missing_inputs, empty_inputs)

    rows = sort_catalog_rows(dedupe_rows(all_rows))
    counts: dict[str, int] = {}
    for row in rows:
        source_key = row.get("source_key", "") or "unknown"
        counts[source_key] = counts.get(source_key, 0) + 1

    return CatalogBuildResult(
        rows=rows,
        counts_by_source=dict(sorted(counts.items())),
        missing_inputs=missing_inputs,
        empty_inputs=empty_inputs,
        weak_inputs=weak_inputs,
    )


def normalize_federal_register_rows(
    rows: list[dict[str, str]],
    source_file: str,
    context: CatalogContext,
) -> list[dict[str, str]]:
    output = []
    for row in rows:
        title = clean_text(value(row, "title"), 500)
        summary = clean_text(value(row, "abstract", "summary"), 1200)
        agency = clean_text(value(row, "agency"), 240)
        document_type = clean_text(value(row, "document_type", "type"), 120)
        raw = parse_json(value(row, "raw_json"))
        text = searchable_text(title, summary, agency, document_type, value(row, "matched_keywords"), value(row, "docket_ids"), raw)
        topic_keys, program_focus = classify_text(text)
        keyword_hits = keyword_matches(text, context.monitored_keywords)
        rht_flag = has_rht_signal(text)
        due_date = iso_date(value(row, "comment_close_date", "comments_close_on"))
        posted_date = iso_date(value(row, "publication_date", "posted_date"))
        effective_date = first_iso_date(raw, ["effective_on", "effective_date"])
        source_record_id = value(row, "source_record_id", "document_number", "id")
        evidence = base_evidence(
            source_file,
            row,
            topic_keys,
            program_focus,
            keyword_hits,
            rht_flag,
            {
                "source_relevance_score": int_or_none(value(row, "relevance_score")),
                "document_type": document_type,
                "docket_ids": split_list(value(row, "docket_ids")),
                "comment_close_date": due_date,
            },
        )
        importance_score = placeholder_importance_score(
            row,
            text,
            topic_keys,
            rht_flag,
            comment_required=bool(due_date),
            score_fields=("relevance_score",),
        )
        output.append(
            catalog_row(
                source_key="federal_register",
                source_record_id=source_record_id,
                record_type="policy_update",
                title=title,
                agency=agency,
                program_focus=program_focus,
                topic_keys=topic_keys,
                posted_date=posted_date,
                updated_date=value(row, "last_checked_at"),
                due_date=due_date,
                effective_date=effective_date,
                docket_id=value(row, "docket_ids"),
                regulation_id="",
                grant_id="",
                opportunity_id="",
                contract_id="",
                vendor_keys_mentioned=vendor_keys(text, context.vendor_aliases),
                rht_flag=rht_flag,
                comment_required_flag=bool(due_date),
                action_required_by=due_date,
                importance_score=importance_score,
                predictive_value_usd=value(row, "predictive_value_usd"),
                score_evidence_json=evidence,
                summary=summary,
                document_url=value(row, "url", "document_url", "pdf_url"),
                source_url=value(row, "url", "source_url"),
                last_checked_at=value(row, "last_checked_at"),
            )
        )
    return output


def normalize_regulations_rows(
    rows: list[dict[str, str]],
    source_file: str,
    context: CatalogContext,
) -> list[dict[str, str]]:
    output = []
    for row in rows:
        docket_id = value(row, "regulations_docket_id", "federal_register_docket_id")
        title = clean_text(value(row, "title", "docket_title"), 500)
        if not title and not docket_id:
            continue
        title = title or f"Regulations.gov docket {docket_id}"
        summary = clean_text(
            "; ".join(
                part
                for part in [
                    value(row, "docket_title"),
                    "status=" + value(row, "docket_status") if value(row, "docket_status") else "",
                    "comments=" + value(row, "comment_count") if value(row, "comment_count") else "",
                    "documents=" + value(row, "document_count") if value(row, "document_count") else "",
                ]
                if part
            ),
            1200,
        )
        agency = clean_text(value(row, "agency_id", "agency"), 120)
        text = searchable_text(title, summary, agency, value(row, "document_types"), value(row, "federal_register_docket_id"), value(row, "regulations_docket_id"))
        topic_keys, program_focus = classify_text(text)
        keyword_hits = keyword_matches(text, context.monitored_keywords)
        rht_flag = has_rht_signal(text)
        due_date = iso_date(value(row, "comment_end_date", "due_date"))
        posted_date = iso_date(value(row, "posted_date"))
        open_comment = truthy(value(row, "open_for_comment")) or truthy(value(row, "within_comment_period"))
        comment_required = open_comment or future_or_today(due_date)
        source_record_id = value(row, "regulations_docket_id", "federal_register_docket_id", "id")
        evidence = base_evidence(
            source_file,
            row,
            topic_keys,
            program_focus,
            keyword_hits,
            rht_flag,
            {
                "docket_status": value(row, "docket_status"),
                "document_types": split_list(value(row, "document_types")),
                "comment_count": int_or_none(value(row, "comment_count")),
                "document_count": int_or_none(value(row, "document_count")),
                "attachment_count": int_or_none(value(row, "attachment_count")),
                "open_for_comment": open_comment,
                "comment_end_date": due_date,
            },
        )
        importance_score = placeholder_importance_score(
            row,
            text,
            topic_keys,
            rht_flag,
            comment_required=comment_required,
            score_fields=("importance_score",),
        )
        output.append(
            catalog_row(
                source_key="regulations",
                source_record_id=source_record_id,
                record_type="docket",
                title=title,
                agency=agency,
                program_focus=program_focus,
                topic_keys=topic_keys,
                posted_date=posted_date,
                updated_date=value(row, "last_checked_at"),
                due_date=due_date,
                effective_date="",
                docket_id=docket_id,
                regulation_id=value(row, "regulations_document_ids"),
                grant_id="",
                opportunity_id="",
                contract_id="",
                vendor_keys_mentioned=vendor_keys(text, context.vendor_aliases),
                rht_flag=rht_flag,
                comment_required_flag=comment_required,
                action_required_by=due_date if comment_required else "",
                importance_score=importance_score,
                predictive_value_usd=value(row, "predictive_value_usd"),
                score_evidence_json=evidence,
                summary=summary,
                document_url=value(row, "documents_url"),
                source_url=value(row, "docket_url", "federal_register_url"),
                last_checked_at=value(row, "last_checked_at"),
            )
        )
    return output


def normalize_grant_rows(
    rows: list[dict[str, str]],
    source_file: str,
    context: CatalogContext,
) -> list[dict[str, str]]:
    output = []
    for row in rows:
        title = clean_text(value(row, "opportunity_title", "title"), 500)
        summary = clean_text(value(row, "summary", "description", "eligibility", "funding_category"), 1200)
        agency = clean_text(value(row, "agency", "agency_code"), 240)
        source_record_id = value(row, "grant_id", "id", "opportunity_number")
        text = searchable_text(title, summary, agency, value(row, "program_focus"), value(row, "topic_keys"), value(row, "assistance_listing_number"))
        topic_keys, program_focus = merge_classification(text, value(row, "topic_keys"), value(row, "program_focus"))
        keyword_hits = keyword_matches(text, context.monitored_keywords)
        rht_flag = row_bool(row, "rht_flag") or has_rht_signal(text)
        due_date = iso_date(value(row, "close_date", "due_date"))
        posted_date = iso_date(value(row, "posted_date", "open_date"))
        evidence = base_evidence(
            source_file,
            row,
            topic_keys,
            program_focus,
            keyword_hits,
            rht_flag,
            {
                "opportunity_number": value(row, "opportunity_number"),
                "award_ceiling": money_or_blank(value(row, "award_ceiling")),
                "award_floor": money_or_blank(value(row, "award_floor")),
                "estimated_total_program_funding": money_or_blank(value(row, "estimated_total_program_funding")),
                "expected_awards": int_or_none(value(row, "expected_awards")),
                "close_date": due_date,
            },
        )
        importance_score = placeholder_importance_score(
            row,
            text,
            topic_keys,
            rht_flag,
            comment_required=False,
            score_fields=("importance_score", "fit_score"),
        )
        output.append(
            catalog_row(
                source_key="grants",
                source_record_id=source_record_id,
                record_type="grant",
                title=title,
                agency=agency,
                program_focus=program_focus,
                topic_keys=topic_keys,
                posted_date=posted_date,
                updated_date=value(row, "last_checked_at"),
                due_date=due_date,
                effective_date="",
                docket_id="",
                regulation_id="",
                grant_id=value(row, "grant_id", "opportunity_number", "id"),
                opportunity_id="",
                contract_id="",
                vendor_keys_mentioned=vendor_keys(text, context.vendor_aliases),
                rht_flag=rht_flag,
                comment_required_flag=False,
                action_required_by=due_date,
                importance_score=importance_score,
                predictive_value_usd=money_or_blank(value(row, "predictive_value_usd")),
                score_evidence_json=evidence,
                summary=summary,
                document_url=value(row, "document_url"),
                source_url=value(row, "source_url", "url"),
                last_checked_at=value(row, "last_checked_at"),
            )
        )
    return output


def normalize_opportunity_rows(
    rows: list[dict[str, str]],
    source_file: str,
    context: CatalogContext,
) -> list[dict[str, str]]:
    output = []
    for row in rows:
        title = clean_text(value(row, "title", "opportunity_title"), 500)
        summary = clean_text(value(row, "summary", "description"), 1200)
        agency = clean_text(value(row, "agency", "subagency", "office"), 240)
        notice_type = value(row, "notice_type", "document_type", "type")
        ptype = value(row, "ptype")
        source_key = canonical_source_key(value(row, "source_key", "source") or "sam_opportunities")
        if source_key not in {"sam_opportunities", "grants"}:
            source_key = "sam_opportunities"
        record_type = "award" if looks_like_award_notice(notice_type, ptype) else "opportunity"
        source_record_id = value(row, "opportunity_id", "sam_notice_id", "id", "source_record_id", "solicitation_number")
        text = searchable_text(title, summary, agency, notice_type, ptype, value(row, "program_focus"), value(row, "topic_keys"), value(row, "vendor_keys_mentioned"))
        topic_keys, program_focus = merge_classification(text, value(row, "topic_keys"), value(row, "program_focus"))
        keyword_hits = keyword_matches(text, context.monitored_keywords)
        rht_flag = row_bool(row, "rht_flag") or has_rht_signal(text)
        due_date = iso_date(value(row, "due_date", "response_deadline", "close_date"))
        posted_date = iso_date(value(row, "posted_date"))
        evidence = base_evidence(
            source_file,
            row,
            topic_keys,
            program_focus,
            keyword_hits,
            rht_flag,
            {
                "notice_type": notice_type,
                "ptype": ptype,
                "lifecycle_status": value(row, "lifecycle_status", "status"),
                "solicitation_number": value(row, "solicitation_number"),
                "naics": value(row, "naics"),
                "psc": value(row, "psc"),
                "due_date": due_date,
            },
        )
        importance_score = placeholder_importance_score(
            row,
            text,
            topic_keys,
            rht_flag,
            comment_required=False,
            score_fields=("importance_score", "fit_score"),
        )
        output.append(
            catalog_row(
                source_key=source_key,
                source_record_id=source_record_id,
                record_type=record_type,
                title=title,
                agency=agency,
                program_focus=program_focus,
                topic_keys=topic_keys,
                posted_date=posted_date,
                updated_date=value(row, "updated_date", "last_updated_at", "last_checked_at"),
                due_date=due_date,
                effective_date="",
                docket_id="",
                regulation_id="",
                grant_id=value(row, "grant_id") if source_key == "grants" else "",
                opportunity_id=value(row, "opportunity_id", "sam_notice_id", "id"),
                contract_id=value(row, "contract_id"),
                vendor_keys_mentioned=value(row, "vendor_keys_mentioned") or join_list(vendor_keys(text, context.vendor_aliases)),
                rht_flag=rht_flag,
                comment_required_flag=False,
                action_required_by=due_date,
                importance_score=importance_score,
                predictive_value_usd=money_or_blank(value(row, "predictive_value_usd", "budget_estimate")),
                score_evidence_json=evidence,
                summary=summary,
                document_url=value(row, "document_url"),
                source_url=value(row, "source_url"),
                last_checked_at=value(row, "last_checked_at"),
            )
        )
    return output


def normalize_cms_provider_rows(
    rows: list[dict[str, str]],
    source_file: str,
    context: CatalogContext,
) -> list[dict[str, str]]:
    return normalize_dataset_rows(
        rows=rows,
        source_file=source_file,
        context=context,
        source_key="cms_data",
        default_agency="Centers for Medicare & Medicaid Services",
        id_fields=("source_record_id", "dataset_id", "id"),
        title_fields=("title", "dataset_title"),
        posted_fields=("released_date", "issued_date", "date_released"),
        updated_fields=("modified_date", "date_modified", "last_checked_at"),
        summary_fields=("description", "summary", "keywords"),
        source_url_fields=("landing_page", "source_url"),
        document_url_fields=("download_url", "document_url"),
    )


def normalize_medicaid_rows(
    rows: list[dict[str, str]],
    source_file: str,
    context: CatalogContext,
) -> list[dict[str, str]]:
    return normalize_dataset_rows(
        rows=rows,
        source_file=source_file,
        context=context,
        source_key="medicaid_data",
        default_agency="Centers for Medicare & Medicaid Services",
        id_fields=("source_record_id", "dataset_id", "id"),
        title_fields=("title", "dataset_title"),
        posted_fields=("released_date", "issued_date", "date_released"),
        updated_fields=("modified_date", "date_modified", "last_checked_at"),
        summary_fields=("description", "summary", "keywords"),
        source_url_fields=("landing_page", "source_url"),
        document_url_fields=("download_url", "document_url"),
    )


def normalize_dataset_signal_rows(
    rows: list[dict[str, str]],
    source_file: str,
    context: CatalogContext,
) -> list[dict[str, str]]:
    output = []
    for row in rows:
        source_key = canonical_source_key(value(row, "source_key") or "cms_data")
        if source_key not in {"cms_data", "medicaid_data", "healthcare_data"}:
            source_key = "cms_data"
        title = clean_text(value(row, "dataset_title", "title", "metric_name"), 500)
        metric_summary = "; ".join(part for part in [value(row, "metric_name"), value(row, "metric_value"), value(row, "state")] if part)
        summary = clean_text(value(row, "summary") or metric_summary, 1200)
        agency = clean_text(value(row, "agency") or "Centers for Medicare & Medicaid Services", 240)
        text = searchable_text(title, summary, agency, value(row, "program_focus"), value(row, "topic_keys"), value(row, "state"))
        topic_keys, program_focus = merge_classification(text, value(row, "topic_keys"), value(row, "program_focus"))
        keyword_hits = keyword_matches(text, context.monitored_keywords)
        rht_flag = row_bool(row, "rht_flag") or has_rht_signal(text)
        evidence = base_evidence(
            source_file,
            row,
            topic_keys,
            program_focus,
            keyword_hits,
            rht_flag,
            {
                "dataset_id": value(row, "dataset_id"),
                "state": value(row, "state"),
                "metric_name": value(row, "metric_name"),
                "metric_value": value(row, "metric_value"),
                "metric_period": value(row, "metric_period"),
            },
        )
        importance_score = placeholder_importance_score(
            row,
            text,
            topic_keys,
            rht_flag,
            comment_required=False,
            score_fields=("importance_score",),
        )
        output.append(
            catalog_row(
                source_key=source_key,
                source_record_id=value(row, "signal_id", "source_record_id", "dataset_id"),
                record_type=value(row, "record_type") or "dataset_signal",
                title=title,
                agency=agency,
                program_focus=program_focus,
                topic_keys=topic_keys,
                posted_date=iso_date(value(row, "date_released", "posted_date", "metric_period")),
                updated_date=iso_date(value(row, "date_modified", "updated_date", "last_checked_at")),
                due_date="",
                effective_date="",
                docket_id="",
                regulation_id="",
                grant_id="",
                opportunity_id="",
                contract_id="",
                vendor_keys_mentioned=join_list(vendor_keys(text, context.vendor_aliases)),
                rht_flag=rht_flag,
                comment_required_flag=False,
                action_required_by="",
                importance_score=importance_score,
                predictive_value_usd=value(row, "predictive_value_usd"),
                score_evidence_json=evidence,
                summary=summary,
                document_url=value(row, "document_url"),
                source_url=value(row, "source_url"),
                last_checked_at=value(row, "last_checked_at"),
            )
        )
    return output


def normalize_dataset_rows(
    rows: list[dict[str, str]],
    source_file: str,
    context: CatalogContext,
    source_key: str,
    default_agency: str,
    id_fields: tuple[str, ...],
    title_fields: tuple[str, ...],
    posted_fields: tuple[str, ...],
    updated_fields: tuple[str, ...],
    summary_fields: tuple[str, ...],
    source_url_fields: tuple[str, ...],
    document_url_fields: tuple[str, ...],
) -> list[dict[str, str]]:
    output = []
    for row in rows:
        title = clean_text(value(row, *title_fields), 500)
        summary = clean_text(value(row, *summary_fields), 1200)
        agency = clean_text(value(row, "publisher", "agency") or default_agency, 240)
        source_record_id = value(row, *id_fields)
        text = searchable_text(title, summary, agency, value(row, "keywords"), value(row, "program_focus"), value(row, "topic_keys"))
        topic_keys, program_focus = merge_classification(text, value(row, "topic_keys"), value(row, "program_focus"))
        keyword_hits = keyword_matches(text, context.monitored_keywords)
        rht_flag = row_bool(row, "rht_flag") or has_rht_signal(text)
        next_update_date = iso_date(value(row, "next_update_date"))
        evidence = base_evidence(
            source_file,
            row,
            topic_keys,
            program_focus,
            keyword_hits,
            rht_flag,
            {
                "dataset_id": source_record_id,
                "issued_date": iso_date(value(row, "issued_date")),
                "released_date": iso_date(value(row, "released_date", "date_released")),
                "modified_date": iso_date(value(row, "modified_date", "date_modified")),
                "next_update_date": next_update_date,
            },
        )
        importance_score = placeholder_importance_score(
            row,
            text,
            topic_keys,
            rht_flag,
            comment_required=False,
            score_fields=("importance_score",),
        )
        output.append(
            catalog_row(
                source_key=source_key,
                source_record_id=source_record_id,
                record_type="dataset_signal",
                title=title,
                agency=agency,
                program_focus=program_focus,
                topic_keys=topic_keys,
                posted_date=iso_date(value(row, *posted_fields)),
                updated_date=iso_date(value(row, *updated_fields)),
                due_date=next_update_date,
                effective_date="",
                docket_id="",
                regulation_id="",
                grant_id="",
                opportunity_id="",
                contract_id="",
                vendor_keys_mentioned=join_list(vendor_keys(text, context.vendor_aliases)),
                rht_flag=rht_flag,
                comment_required_flag=False,
                action_required_by="",
                importance_score=importance_score,
                predictive_value_usd=value(row, "predictive_value_usd"),
                score_evidence_json=evidence,
                summary=summary,
                document_url=value(row, *document_url_fields),
                source_url=value(row, *source_url_fields),
                last_checked_at=value(row, "last_checked_at"),
            )
        )
    return output


def catalog_row(**kwargs: Any) -> dict[str, str]:
    source_key = canonical_source_key(str(kwargs.get("source_key") or ""))
    source_record_id = str(kwargs.get("source_record_id") or "").strip()
    title = clean_text(kwargs.get("title"), 500) or "Untitled federal update"
    posted_date = iso_date(kwargs.get("posted_date"))
    updated_date = iso_date(kwargs.get("updated_date")) or str(kwargs.get("updated_date") or "")
    row = {field: "" for field in CATALOG_FIELDS}
    row.update(
        {
            "source_key": source_key,
            "source_record_id": source_record_id,
            "record_type": clean_key(kwargs.get("record_type") or "dataset_signal"),
            "title": title,
            "agency": clean_text(kwargs.get("agency"), 240),
            "program_focus": join_list(kwargs.get("program_focus") or []),
            "topic_keys": join_list(kwargs.get("topic_keys") or []),
            "posted_date": posted_date,
            "updated_date": updated_date,
            "due_date": iso_date(kwargs.get("due_date")),
            "effective_date": iso_date(kwargs.get("effective_date")),
            "docket_id": join_list(split_list(kwargs.get("docket_id"))),
            "regulation_id": join_list(split_list(kwargs.get("regulation_id"))),
            "grant_id": clean_text(kwargs.get("grant_id"), 160),
            "opportunity_id": clean_text(kwargs.get("opportunity_id"), 160),
            "contract_id": clean_text(kwargs.get("contract_id"), 160),
            "vendor_keys_mentioned": join_list(split_list(kwargs.get("vendor_keys_mentioned"))),
            "rht_flag": bool_text(bool(kwargs.get("rht_flag"))),
            "comment_required_flag": bool_text(bool(kwargs.get("comment_required_flag"))),
            "action_required_by": iso_date(kwargs.get("action_required_by")),
            "importance_score": str(int_or_zero(kwargs.get("importance_score"))),
            "predictive_value_usd": money_or_blank(kwargs.get("predictive_value_usd")),
            "score_evidence_json": stringify_evidence(kwargs.get("score_evidence_json")),
            "summary": clean_text(kwargs.get("summary"), 1200),
            "document_url": clean_text(kwargs.get("document_url"), 1000),
            "source_url": clean_text(kwargs.get("source_url"), 1000),
            "last_checked_at": clean_text(kwargs.get("last_checked_at"), 80),
        }
    )
    row["update_id"] = make_update_id(row)
    return row


def dedupe_rows(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    by_key: dict[tuple[str, str], dict[str, str]] = {}
    fallback: dict[tuple[str, str, str, str], dict[str, str]] = {}

    for row in rows:
        source_key = row.get("source_key", "")
        source_record_id = row.get("source_record_id", "")
        if source_record_id:
            key = (source_key, source_record_id)
            by_key[key] = merge_preferred(by_key.get(key), row)
            continue
        fkey = (
            source_key,
            clean_key(row.get("record_type") or ""),
            clean_key(row.get("title") or "")[:120],
            row.get("posted_date") or row.get("updated_date") or row.get("due_date") or "",
        )
        fallback[fkey] = merge_preferred(fallback.get(fkey), row)

    return list(by_key.values()) + list(fallback.values())


def merge_preferred(old: dict[str, str] | None, new: dict[str, str]) -> dict[str, str]:
    if old is None:
        return new
    old_score = int_or_zero(old.get("importance_score"))
    new_score = int_or_zero(new.get("importance_score"))
    preferred, other = (new, old) if new_score >= old_score else (old, new)
    merged = dict(preferred)
    for field in CATALOG_FIELDS:
        if not merged.get(field) and other.get(field):
            merged[field] = other[field]
    merged["update_id"] = make_update_id(merged)
    return merged


def sort_catalog_rows(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    def row_key(row: dict[str, str]) -> tuple[bool, str, int, str, str]:
        substantive_date = row.get("posted_date") or row.get("due_date") or row.get("effective_date") or ""
        fallback_date = row.get("updated_date") or ""
        return (
            bool(substantive_date),
            substantive_date or fallback_date,
            int_or_zero(row.get("importance_score")),
            row.get("source_key", ""),
            row.get("title", ""),
        )

    return sorted(rows, key=row_key, reverse=True)


def write_catalog(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CATALOG_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def load_context(search_parameters_path: Path) -> CatalogContext:
    if not search_parameters_path.exists():
        return CatalogContext(monitored_keywords=[], vendor_aliases={})
    data = json.loads(search_parameters_path.read_text(encoding="utf-8"))
    keywords = [str(item) for item in data.get("monitored_keywords") or []]
    vendor_aliases: dict[str, list[str]] = {}
    for vendor in data.get("vendors") or []:
        name = str(vendor.get("name") or "").strip()
        if not name:
            continue
        key = vendor_key_from_name(name)
        aliases = [name]
        aliases.extend(str(alias) for alias in vendor.get("aliases") or [])
        vendor_aliases[key] = [alias for alias in aliases if alias]
    return CatalogContext(monitored_keywords=keywords, vendor_aliases=vendor_aliases)


def base_evidence(
    source_file: str,
    row: dict[str, str],
    topic_keys: list[str],
    program_focus: list[str],
    keyword_hits: list[str],
    rht_flag: bool,
    extra: dict[str, Any],
) -> dict[str, Any]:
    evidence: dict[str, Any] = {
        "score_version": "placeholder-rule-v1",
        "source_file": source_file,
        "topic_keys": topic_keys,
        "program_focus": program_focus,
        "matched_keywords": keyword_hits,
        "rht_flag": rht_flag,
    }
    for key, value_ in extra.items():
        if value_ not in (None, "", [], {}):
            evidence[key] = value_
    existing = parse_json(value(row, "score_evidence_json"))
    if existing:
        evidence["source_score_evidence_keys"] = sorted(existing)[:12]
    return evidence


def placeholder_importance_score(
    row: dict[str, str],
    text: str,
    topic_keys: list[str],
    rht_flag: bool,
    comment_required: bool,
    score_fields: tuple[str, ...],
) -> int:
    for field in score_fields:
        parsed = int_or_none(value(row, field))
        if parsed is not None:
            return clamp(parsed, 0, 100)

    score = 10
    lower = text.lower()
    topic_bonus = min(len(topic_keys) * 4, 32)
    score += topic_bonus
    if rht_flag:
        score += 12
    if comment_required:
        score += 10
    if any(term in lower for term in ("proposed rule", "final rule", "rulemaking")):
        score += 10
    if any(term in lower for term in ("medicaid", "mmis", "eligibility", "claims", "managed care")):
        score += 8
    if any(term in lower for term in ("funding", "grant", "award", "solicitation", "sources sought")):
        score += 6
    return clamp(score, 0, 100)


def classify_text(text: str) -> tuple[list[str], list[str]]:
    lower = text.lower()
    topics: list[str] = []
    focuses: list[str] = []
    for topic, focus, terms in TOPIC_RULES:
        if any(term in lower for term in terms):
            topics.append(topic)
            focuses.append(focus)
    return unique_sorted(topics), unique_sorted(focuses)


def merge_classification(text: str, topic_value: Any, focus_value: Any) -> tuple[list[str], list[str]]:
    detected_topics, detected_focus = classify_text(text)
    topics = unique_sorted([clean_key(item) for item in split_list(topic_value)] + detected_topics)
    focus = unique_sorted([clean_key(item) for item in split_list(focus_value)] + detected_focus)
    return topics, focus


def keyword_matches(text: str, keywords: list[str]) -> list[str]:
    lower = text.lower()
    hits = []
    for keyword in keywords:
        keyword_text = str(keyword).strip()
        if keyword_text and keyword_text.lower() in lower:
            hits.append(keyword_text)
    return unique_sorted(hits)


def vendor_keys(text: str, aliases_by_key: dict[str, list[str]]) -> list[str]:
    lower = text.lower()
    hits = []
    for key, aliases in aliases_by_key.items():
        if any(alias and alias.lower() in lower for alias in aliases):
            hits.append(key)
    return unique_sorted(hits)


def has_rht_signal(text: str) -> bool:
    lower = text.lower()
    return any(term in lower for term in RHT_TERMS)


def looks_like_award_notice(notice_type: str, ptype: str) -> bool:
    lower = f"{notice_type} {ptype}".lower()
    return "award" in lower or ptype.lower() == "a"


def canonical_source_key(value_: str) -> str:
    cleaned = clean_key(value_)
    return SOURCE_KEY_MAP.get(cleaned.replace("_", " "), SOURCE_KEY_MAP.get(cleaned, cleaned or "unknown"))


def make_update_id(row: dict[str, str]) -> str:
    source_key = row.get("source_key", "unknown")
    source_record_id = row.get("source_record_id", "")
    if source_record_id:
        seed = f"{source_key}|{source_record_id}"
    else:
        seed = "|".join(
            [
                source_key,
                row.get("record_type", ""),
                row.get("title", ""),
                row.get("posted_date") or row.get("updated_date") or row.get("due_date") or "",
            ]
        )
    return "upd-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]


def clean_key(value_: Any) -> str:
    text = str(value_ or "").strip().lower()
    text = re.sub(r"[^a-z0-9]+", "_", text)
    return text.strip("_")


def vendor_key_from_name(name: str) -> str:
    return clean_key(name)


def searchable_text(*parts: Any) -> str:
    values = []
    for part in parts:
        if isinstance(part, dict):
            values.append(json.dumps(part, ensure_ascii=True, sort_keys=True))
        elif isinstance(part, list):
            values.append(" ".join(str(item) for item in part))
        else:
            values.append(str(part or ""))
    return " ".join(values)


def value(row: dict[str, str], *fields: str) -> str:
    for field in fields:
        raw = row.get(field)
        if raw is not None and str(raw).strip() != "":
            return str(raw).strip()
    return ""


def split_list(value_: Any) -> list[str]:
    if value_ is None:
        return []
    if isinstance(value_, list):
        return [str(item).strip() for item in value_ if str(item).strip()]
    text = str(value_).strip()
    if not text:
        return []
    parts = re.split(r"[;|,]", text)
    return [part.strip() for part in parts if part.strip()]


def join_list(value_: Any) -> str:
    items = split_list(value_)
    return ";".join(unique_sorted(items))


def unique_sorted(values: list[str]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for value_ in values:
        item = str(value_).strip()
        if not item:
            continue
        key = item.lower()
        if key in seen:
            continue
        seen.add(key)
        output.append(item)
    return sorted(output, key=str.lower)


def clean_text(value_: Any, limit: int = 1000) -> str:
    text = re.sub(r"\s+", " ", str(value_ or "")).strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def parse_json(value_: Any) -> dict[str, Any]:
    text = str(value_ or "").strip()
    if not text:
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def stringify_evidence(value_: Any) -> str:
    if isinstance(value_, str):
        parsed = parse_json(value_)
        value_ = parsed or {"note": clean_text(value_, 300)}
    if not isinstance(value_, dict):
        value_ = {}
    compact = {key: val for key, val in value_.items() if val not in (None, "", [], {})}
    return json.dumps(compact, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def row_bool(row: dict[str, str], field: str) -> bool:
    return truthy(value(row, field))


def truthy(value_: Any) -> bool:
    text = str(value_ or "").strip().lower()
    if text in TRUE_VALUES:
        return True
    if text in FALSE_VALUES:
        return False
    return False


def bool_text(value_: bool) -> str:
    return "true" if value_ else "false"


def iso_date(value_: Any) -> str:
    parsed = parse_date(value_)
    return parsed.isoformat() if parsed else ""


def parse_date(value_: Any) -> dt.date | None:
    if isinstance(value_, dt.datetime):
        return value_.date()
    if isinstance(value_, dt.date):
        return value_
    text = str(value_ or "").strip()
    if not text:
        return None
    text = text.replace("Z", "+00:00")
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z"):
        try:
            return dt.datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    try:
        return dt.datetime.fromisoformat(text).date()
    except ValueError:
        return None


def first_iso_date(data: dict[str, Any], fields: list[str]) -> str:
    for field in fields:
        parsed = iso_date(data.get(field))
        if parsed:
            return parsed
    return ""


def future_or_today(date_text: str) -> bool:
    parsed = parse_date(date_text)
    return bool(parsed and parsed >= dt.date.today())


def int_or_none(value_: Any) -> int | None:
    text = str(value_ or "").strip().replace(",", "")
    if not text:
        return None
    try:
        return int(float(text))
    except ValueError:
        return None


def int_or_zero(value_: Any) -> int:
    return int_or_none(value_) or 0


def clamp(value_: int, low: int, high: int) -> int:
    return max(low, min(value_, high))


def money_or_blank(value_: Any) -> str:
    text = str(value_ or "").strip()
    if not text:
        return ""
    cleaned = re.sub(r"[^0-9.\-]", "", text)
    if cleaned in {"", "-", "."}:
        return ""
    try:
        amount = float(cleaned)
    except ValueError:
        return ""
    return str(int(amount)) if amount.is_integer() else f"{amount:.2f}"


def emit(progress: Callable[[str], None] | None, message: str) -> None:
    if progress:
        progress(message)
