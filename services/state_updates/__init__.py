from __future__ import annotations

import datetime as dt
import hashlib
import importlib
import json
import re
from typing import Any, Callable

from services.state_updates.store import STATE_UPDATE_FIELDS

TOPIC_RULES = [
    ("rht", "rht", ["rural health transformation", "rht"]),
    ("rural_health", "rural_health", ["rural health", "critical access hospital", "frontier", "rural hospital"]),
    ("medicaid", "medicaid", ["medicaid", "chip", "children's health insurance program", "1115", "1915"]),
    ("medicare", "medicare", ["medicare", "medicare-medicaid", "dual eligible", "dual enrollment"]),
    ("cms", "cms", ["cms", "centers for medicare", "centers for medicaid"]),
    ("mmis", "medicaid", ["mmis", "medicaid management information system"]),
    ("claims", "claims", ["claims", "encounter data", "payment system"]),
    ("eligibility", "eligibility", ["eligibility", "enrollment", "redetermination", "renewal"]),
    ("managed_care", "managed_care", ["managed care", "mco", "capitation", "plan rating"]),
    ("provider_data", "provider_data", ["provider data", "provider enrollment", "provider directory", "revalidation"]),
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

ACCEPTED_RECORD_TYPES = {
    "policy_update",
    "provider_bulletin",
    "medicaid_notice",
    "waiver_notice",
    "spa_notice",
    "rht_notice",
    "grant_notice",
    "public_comment_notice",
    "dataset_update",
    "guidance",
}

StateUpdateFetcher = Callable[..., list[dict[str, str]]]
STATE_CLIENTS: dict[str, StateUpdateFetcher] = {}


def fetch_state_updates(
    *,
    states: list[str],
    keywords: list[str],
    max_records: int,
    progress: Callable[[str], None] | None = None,
) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    for state in states:
        state_code = state.strip().upper()
        if not state_code:
            continue
        fetcher = STATE_CLIENTS.get(state_code)
        if not fetcher:
            emit(progress, f"{state_code}: no state update adapter yet")
            continue
        emit(progress, f"{state_code}: searching state updates")
        records.extend(fetcher(keywords=keywords, max_records=max_records, progress=progress))
    return sorted(records, key=sort_key, reverse=True)[:max_records]


def state_update_record(
    *,
    state: str,
    source: str,
    source_record_id: str = "",
    record_type: str = "policy_update",
    title: str,
    agency: str = "",
    summary: str = "",
    posted_date: Any = "",
    updated_date: Any = "",
    due_date: Any = "",
    effective_date: Any = "",
    comment_required: bool = False,
    action_required_by: Any = "",
    document_url: str = "",
    source_url: str = "",
    keywords: list[str] | None = None,
    raw: Any = None,
    last_checked: Any = "",
    record_id: str = "",
) -> dict[str, str]:
    title_text = clean_text(title, 500) or "Untitled state update"
    summary_text = clean_text(summary, 1200)
    text = searchable_text(title_text, summary_text, agency, source_record_id, source_url, document_url, raw)
    topic_keys, program_focus = classify_text(text)
    matched_keywords = keyword_matches(text, keywords or [])
    rht_flag = has_rht_signal(text)
    row = {field: "" for field in STATE_UPDATE_FIELDS}
    row.update(
        {
            "id": record_id,
            "state": state.strip().upper(),
            "source": clean_key(source),
            "source_record_id": clean_text(source_record_id, 240),
            "record_type": clean_record_type(record_type),
            "title": title_text,
            "agency": clean_text(agency, 240),
            "program_focus": join_list(program_focus),
            "topic_keys": join_list(topic_keys),
            "posted_date": iso_date(posted_date),
            "updated_date": iso_date(updated_date),
            "due_date": iso_date(due_date),
            "effective_date": iso_date(effective_date),
            "comment_required_flag": bool_text(comment_required),
            "action_required_by": iso_date(action_required_by),
            "importance_score": str(score_update_text(text, topic_keys, rht_flag, comment_required)),
            "summary": summary_text,
            "document_url": clean_text(document_url, 1000),
            "source_url": clean_text(source_url, 1000),
            "matched_keywords": join_list(matched_keywords),
            "rht_flag": bool_text(rht_flag),
            "raw_json": json_text(raw),
            "last_checked_at": clean_text(last_checked, 80) or utc_now(),
        }
    )
    if not row["id"]:
        row["id"] = make_state_update_id(row)
    return row


def make_state_update_id(row: dict[str, str]) -> str:
    if row.get("source_record_id"):
        seed = "|".join([row.get("state", ""), row.get("source", ""), row.get("source_record_id", "")])
    else:
        seed = "|".join(
            [
                row.get("state", ""),
                row.get("source", ""),
                row.get("record_type", ""),
                row.get("title", ""),
                row.get("posted_date") or row.get("updated_date") or row.get("effective_date") or "",
                row.get("source_url") or row.get("document_url") or "",
            ]
        )
    return "stupd-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]


def classify_text(text: str) -> tuple[list[str], list[str]]:
    lower = text.lower()
    topics: list[str] = []
    focuses: list[str] = []
    for topic, focus, terms in TOPIC_RULES:
        if any(term in lower for term in terms):
            topics.append(topic)
            focuses.append(focus)
    return unique_sorted(topics), unique_sorted(focuses)


def keyword_matches(text: str, keywords: list[str]) -> list[str]:
    lower = text.lower()
    hits = []
    for keyword in keywords:
        keyword_text = str(keyword).strip()
        if keyword_text and keyword_text.lower() in lower:
            hits.append(keyword_text)
    return unique_sorted(hits)


def has_rht_signal(text: str) -> bool:
    lower = text.lower()
    return any(term in lower for term in RHT_TERMS)


def score_update_text(text: str, topic_keys: list[str], rht_flag: bool, comment_required: bool) -> int:
    score = 10 + min(len(topic_keys) * 4, 32)
    lower = text.lower()
    if rht_flag:
        score += 12
    if comment_required:
        score += 10
    if any(term in lower for term in ("medicaid", "mmis", "eligibility", "claims", "managed care")):
        score += 8
    if any(term in lower for term in ("waiver", "state plan amendment", "spa", "public comment")):
        score += 8
    if any(term in lower for term in ("funding", "grant", "award")):
        score += 6
    return max(0, min(score, 100))


def sort_key(row: dict[str, str]) -> tuple[int, str, str, str, str]:
    return (
        int_or_zero(row.get("importance_score")),
        row.get("posted_date") or row.get("updated_date") or row.get("effective_date") or "",
        row.get("due_date") or row.get("action_required_by") or "",
        row.get("state", ""),
        row.get("title", ""),
    )


def iso_date(value: Any) -> str:
    parsed = parse_date(value)
    return parsed.isoformat() if parsed else ""


def parse_date(value: Any) -> dt.date | None:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    text = text.replace("Z", "+00:00")
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y", "%B %d, %Y", "%b %d, %Y", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z"):
        try:
            return dt.datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    try:
        return dt.datetime.fromisoformat(text).date()
    except ValueError:
        return None


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def clean_record_type(value: str) -> str:
    key = clean_key(value) or "policy_update"
    return key if key in ACCEPTED_RECORD_TYPES else "policy_update"


def clean_key(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"[^a-z0-9]+", "_", text)
    return text.strip("_")


def clean_text(value: Any, limit: int = 1000) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


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


def split_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).strip()
    if not text:
        return []
    parts = re.split(r"[;|,]", text)
    return [part.strip() for part in parts if part.strip()]


def join_list(value: Any) -> str:
    return ";".join(unique_sorted(split_list(value)))


def unique_sorted(values: list[str]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for value in values:
        item = str(value).strip()
        if not item:
            continue
        key = item.lower()
        if key in seen:
            continue
        seen.add(key)
        output.append(item)
    return sorted(output, key=str.lower)


def json_text(value: Any) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def bool_text(value: bool) -> str:
    return "true" if value else "false"


def int_or_zero(value: Any) -> int:
    try:
        return int(float(str(value or "0").replace(",", "")))
    except ValueError:
        return 0


def emit(progress: Callable[[str], None] | None, message: str) -> None:
    if progress:
        progress(message)


from . import il as il_updates  # noqa: E402
from . import mi as mi_updates  # noqa: E402
from . import nj as nj_updates  # noqa: E402

STATE_CLIENTS.update(
    {
        "IL": il_updates.fetch_updates,
        "MI": mi_updates.fetch_updates,
        "NJ": nj_updates.fetch_updates,
    }
)


from services.state_updates import pa, tx  # noqa: E402

STATE_CLIENTS.update(
    {
        "PA": pa.fetch_updates,
        "TX": tx.fetch_updates,
    }
)

def _register_state_clients() -> None:
    for state_code, module_name in {
        "AK": "ak",
        "AL": "al",
        "AR": "ar",
        "AZ": "az",
        "CA": "ca",
        "CO": "co",
        "FL": "fl",
        "OR": "or",
        "PR": "pr",
        "SD": "sd",
        "TN": "tn",
        "VA": "va",
        "VT": "vt",
        "WY": "wy",
    }.items():
        module = importlib.import_module(f"services.state_updates.{module_name}")
        STATE_CLIENTS[state_code] = module.fetch_updates


_register_state_clients()
