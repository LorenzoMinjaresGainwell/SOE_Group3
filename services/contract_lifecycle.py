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

DEFAULT_KEYWORDS = [
    "Medicaid",
    "Medicare",
    "CMS",
    "MMIS",
    "claims",
    "eligibility",
    "enrollment",
    "managed care",
    "interoperability",
    "FHIR",
    "prior authorization",
    "contact center",
    "provider data",
    "quality measures",
    "rural health",
    "rural health transformation",
    "critical access hospital",
    "telehealth",
    "behavioral health",
    "workforce",
]

DEFAULT_VENDOR_ALIASES = {
    "gainwell_technologies": [
        "Gainwell Technologies",
        "Gainwell",
        "Gainwell Technologies LLC",
        "Health Management Systems",
        "Health Management Systems Inc",
        "DXC Technology Services LLC",
    ],
    "maximus": ["MAXIMUS", "MAXIMUS Federal Services", "MAXIMUS Federal Services Inc"],
    "deloitte": ["Deloitte", "Deloitte Consulting", "Deloitte Consulting LLP"],
    "accenture_federal_services": ["Accenture Federal Services", "Accenture LLP"],
    "optum": ["Optum", "OptumServe", "OptumServe Technology Services"],
    "conduent": ["Conduent", "Conduent State Healthcare"],
}

PREDECESSOR_TERMS = ("health management systems", "dxc")
COMPETITOR_KEYS = {"maximus", "deloitte", "accenture_federal_services", "optum", "conduent"}

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
    "upcoming": 1,
    "awarded": 2,
    "active": 3,
    "expired": 4,
    "unknown": 5,
}


@dataclass(frozen=True)
class VendorMatch:
    vendor_key: str
    gwt_relation: str
    matched_alias: str


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
    """Build a USAspending-only contract lifecycle catalog from existing CSV output."""
    source_path = root / contracts_path
    if not source_path.exists():
        raise LifecycleBlockedError(f"missing required input: {contracts_path}")

    source_rows = read_csv(source_path)
    if not source_rows:
        raise LifecycleBlockedError(f"empty required input: {contracts_path}")

    context = load_context(root / search_parameters_path, recompete_months=recompete_months)
    missing_optional, empty_optional = optional_input_status(root, optional_inputs or DEFAULT_OPTIONAL_INPUTS)
    run_date = today or dt.date.today()
    emit(progress, f"normalizing {contracts_path}: {len(source_rows)} rows")

    rows = normalize_usaspending_rows(
        source_rows,
        source_file=str(contracts_path),
        context=context,
        today=run_date,
        missing_optional_inputs=missing_optional,
    )
    if not rows:
        raise LifecycleBlockedError(f"no usable USAspending rows in {contracts_path}")

    rows = sort_lifecycle_rows(dedupe_rows(rows))
    counts_by_status: dict[str, int] = {}
    counts_by_vendor: dict[str, int] = {}
    for row in rows:
        counts_by_status[row["lifecycle_status"]] = counts_by_status.get(row["lifecycle_status"], 0) + 1
        counts_by_vendor[row["vendor_key"]] = counts_by_vendor.get(row["vendor_key"], 0) + 1

    return LifecycleBuildResult(
        rows=rows,
        counts_by_status=dict(sorted(counts_by_status.items())),
        counts_by_vendor=dict(sorted(counts_by_vendor.items())),
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
            "join_confidence": "usaspending_only_no_sam_entity_match",
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
        }
        output.append(
            lifecycle_row(
                contract_id=stable_contract_id("usaspending", source_record_id),
                source_keys="usaspending",
                source_record_ids=source_record_id,
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
                uei_sam="",
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
                score_evidence_json=json.dumps(evidence, sort_keys=True, separators=(",", ":")),
                source_urls=value(row, "source_url"),
                last_checked_at=now_iso(),
            )
        )
    return output


def load_context(path: Path, *, recompete_months: int | None = None) -> LifecycleContext:
    params: dict[str, Any] = {}
    if path.exists():
        with path.open(encoding="utf-8") as handle:
            params = json.load(handle)
    usaspending = params.get("usaspending") if isinstance(params.get("usaspending"), dict) else {}
    configured_months = int_or_none(usaspending.get("recompete_months")) if usaspending else None
    return LifecycleContext(
        keywords=[str(item) for item in params.get("monitored_keywords") or DEFAULT_KEYWORDS],
        vendor_aliases=vendor_alias_map(params),
        recompete_months=recompete_months or configured_months or 36,
    )


def vendor_alias_map(params: dict[str, Any]) -> dict[str, list[str]]:
    aliases = {key: list(values) for key, values in DEFAULT_VENDOR_ALIASES.items()}
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
    elif status == "awarded":
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


def optional_input_status(root: Path, optional_inputs: dict[str, str | Path]) -> tuple[list[str], list[str]]:
    missing: list[str] = []
    empty: list[str] = []
    for relative_path in optional_inputs.values():
        path = root / relative_path
        display = str(relative_path)
        if not path.exists():
            missing.append(display)
        elif path.stat().st_size == 0:
            empty.append(display)
    return missing, empty


def dedupe_rows(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    by_key: dict[str, dict[str, str]] = {}
    for row in rows:
        key = row.get("source_record_ids") or row.get("contract_id", "")
        if key not in by_key:
            by_key[key] = row
    return list(by_key.values())


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
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=LIFECYCLE_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


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
