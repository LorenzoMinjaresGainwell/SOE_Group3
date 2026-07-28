from __future__ import annotations

import datetime as dt
import hashlib
import html
import json
import re
from typing import Any

PROGRAM_FOCUS_RULES = {
    "medicaid_mmis": ["Medicaid", "MMIS", "CHIP", "Medicaid enterprise", "claims", "eligibility", "enrollment"],
    "medicare_cms": ["Medicare", "CMS", "Centers for Medicare", "quality measures"],
    "managed_care": ["managed care", "MCO", "care management", "behavioral health"],
    "health_it": ["FHIR", "interoperability", "prior authorization", "provider data", "health information exchange"],
    "rural_health_rht": [
        "rural health",
        "rural health transformation",
        "critical access hospital",
        "telehealth",
        "workforce",
    ],
}

DEFAULT_TOPIC_TERMS = sorted({term for terms in PROGRAM_FOCUS_RULES.values() for term in terms}, key=str.lower)
DATE_FORMATS = (
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%m/%d/%Y",
    "%m/%d/%y",
    "%m-%d-%Y",
    "%m-%d-%y",
    "%B %d, %Y",
    "%b %d, %Y",
    "%d %B %Y",
    "%d %b %Y",
    "%Y-%m-%d %H:%M:%S",
    "%m/%d/%Y %I:%M:%S %p",
    "%m/%d/%Y %H:%M:%S",
)


def clean_text(value: Any, limit: int | None = None) -> str:
    text = html.unescape(str(value or ""))
    text = re.sub(r"\s+", " ", text).strip()
    if limit is not None and limit > 0 and len(text) > limit:
        return text[: limit - 1].rstrip() + "..."
    return text


def parse_date(value: Any) -> dt.date | None:
    if value is None or value == "":
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value

    text = clean_text(value)
    if not text:
        return None

    dotnet_match = re.search(r"/Date\((-?\d+)", text)
    if dotnet_match:
        try:
            timestamp = int(dotnet_match.group(1)) / 1000
            return dt.datetime.fromtimestamp(timestamp, tz=dt.timezone.utc).date()
        except (OverflowError, OSError, ValueError):
            return None

    text = re.sub(r"\b(\d{1,2})(st|nd|rd|th)\b", r"\1", text, flags=re.IGNORECASE)
    iso_text = text.replace("Z", "+00:00")
    try:
        return dt.datetime.fromisoformat(iso_text).date()
    except ValueError:
        try:
            return dt.date.fromisoformat(iso_text)
        except ValueError:
            pass

    for fmt in DATE_FORMATS:
        try:
            return dt.datetime.strptime(text, fmt).date()
        except ValueError:
            pass

    for pattern, fmt in (
        (r"\b\d{4}-\d{1,2}-\d{1,2}\b", "%Y-%m-%d"),
        (r"\b\d{1,2}/\d{1,2}/\d{2,4}\b", "%m/%d/%Y"),
    ):
        match = re.search(pattern, text)
        if not match:
            continue
        candidate = match.group(0)
        if fmt == "%m/%d/%Y" and len(candidate.rsplit("/", 1)[-1]) == 2:
            fmt = "%m/%d/%y"
        try:
            return dt.datetime.strptime(candidate, fmt).date()
        except ValueError:
            continue
    return None


def iso_date(value: Any) -> str:
    parsed = parse_date(value)
    return parsed.isoformat() if parsed else ""


def parse_amount(value: Any) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(round(float(value)))

    text = clean_text(value)
    if not text or text.lower() in {"n/a", "na", "none", "not available", "various", "tbd"}:
        return None

    lower = text.lower()
    negative = lower.startswith("(") and ")" in lower
    multiplier = 1
    if "billion" in lower or re.search(r"\d\s*b\b", lower):
        multiplier = 1_000_000_000
    elif "million" in lower or re.search(r"\d\s*m\b", lower):
        multiplier = 1_000_000
    elif "thousand" in lower or re.search(r"\d\s*k\b", lower):
        multiplier = 1_000

    normalized = text.replace(",", "")
    match = re.search(r"[-+]?\d+(?:\.\d+)?", normalized)
    if not match:
        return None
    amount = float(match.group(0)) * multiplier
    if negative and amount > 0:
        amount = -amount
    return int(round(amount))


def amount_string(value: Any) -> str:
    amount = parse_amount(value)
    return "" if amount is None else str(amount)


def stable_id(*parts: Any, prefix: str = "state", max_length: int = 160) -> str:
    raw_parts = [clean_text(part) for part in parts if clean_text(part)]
    raw = "\x1f".join(raw_parts) or "record"
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]
    slug_source = " ".join(raw_parts) or "record"
    slug = re.sub(r"[^A-Za-z0-9]+", "-", slug_source).strip("-").lower() or "record"
    safe_prefix = re.sub(r"[^A-Za-z0-9]+", "-", prefix).strip("-").lower()

    prefix_part = f"{safe_prefix}-" if safe_prefix else ""
    suffix = f"-{digest}"
    budget = max(1, max_length - len(prefix_part) - len(suffix))
    slug = slug[:budget].strip("-") or "record"
    return f"{prefix_part}{slug}{suffix}"[:max_length].strip("-")


def clean_id(value: Any) -> str:
    if value is None or value == "":
        return ""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return clean_text(value)
    return str(int(number)) if number.is_integer() else clean_text(value)


def term_matches(text: Any, term: str) -> bool:
    parts = [re.escape(part) for part in re.split(r"\s+", clean_text(term)) if part]
    if not parts:
        return False
    pattern = r"(?<![A-Za-z0-9])" + r"\s+".join(parts) + r"(?![A-Za-z0-9])"
    return re.search(pattern, clean_text(text), re.IGNORECASE) is not None


def keyword_hits(text: Any, keywords: list[str]) -> list[str]:
    return sorted({keyword for keyword in keywords if keyword and term_matches(text, keyword)}, key=str.lower)


def topic_hits(text: Any, topics: list[str] | None = None) -> list[str]:
    return keyword_hits(text, topics or DEFAULT_TOPIC_TERMS)


def program_focus_matches(text: Any, rules: dict[str, list[str]] | None = None) -> list[str]:
    active_rules = rules or PROGRAM_FOCUS_RULES
    matches: list[str] = []
    for focus, terms in active_rules.items():
        if keyword_hits(text, terms):
            matches.append(focus)
    return sorted(matches)


def compact_raw_json(value: Any, *, limit: int = 5000) -> str:
    try:
        text = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        text = json.dumps(str(value), ensure_ascii=True)
    if limit <= 0 or len(text) <= limit:
        return text

    # Keep the field valid JSON when retaining only an excerpt. Cutting the
    # serialized value directly can leave malformed JSON in generated CSVs.
    marker = {"_truncated": True, "preview": ""}
    empty = json.dumps(marker, ensure_ascii=True, separators=(",", ":"))
    if len(empty) > limit:
        return "null"
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        marker["preview"] = text[:middle]
        encoded = json.dumps(marker, ensure_ascii=True, separators=(",", ":"))
        if len(encoded) <= limit:
            low = middle
        else:
            high = middle - 1
    marker["preview"] = text[:low]
    return json.dumps(marker, ensure_ascii=True, separators=(",", ":"))


def compact_raw_subset(row: dict[str, Any], keys: list[str], *, limit: int = 5000) -> str:
    return compact_raw_json({key: row.get(key) for key in keys if key in row}, limit=limit)


def months_until(value: Any, *, today: dt.date | None = None) -> int | None:
    parsed = parse_date(value)
    if not parsed:
        return None
    base = today or dt.date.today()
    return (parsed.year - base.year) * 12 + (parsed.month - base.month)


def int_or_zero(value: Any) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0
