from __future__ import annotations

import csv
import datetime as dt
import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

USASPENDING_AWARDS_URL = "https://api.usaspending.gov/api/v2/search/spending_by_award/"
USASPENDING_SEARCH_FLOOR = dt.date(2007, 10, 1)
USER_AGENT = "soe-group3-usaspending/0.1"

CONTRACT_FIELDS = [
    "id",
    "vendor_name",
    "vendor_query",
    "recipient_name",
    "award_id",
    "generated_internal_id",
    "awarding_agency",
    "awarding_sub_agency",
    "award_amount",
    "start_date",
    "end_date",
    "months_to_end",
    "recompete_signal",
    "naics_code",
    "psc_code",
    "description",
    "matched_keywords",
    "relevance_score",
    "source_url",
    "last_checked_at",
]

AWARD_FIELDS = [
    "Award ID",
    "Recipient Name",
    "Award Amount",
    "Start Date",
    "End Date",
    "Awarding Agency",
    "Awarding Sub Agency",
    "Description",
    "naics_code",
    "psc_code",
]

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


@dataclass
class VendorSearch:
    name: str
    queries: list[str]


@dataclass
class USASpendingConfig:
    vendors: list[VendorSearch]
    keywords: list[str]
    start_date: dt.date
    end_date: dt.date
    award_type_codes: list[str]
    max_per_vendor: int = 100
    page_limit: int = 100
    min_award_amount: int = 0
    recompete_months: int = 36
    near_expiry_months: int = 18
    only_keyword_matches: bool = False


def load_search_parameters(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def config_from_parameters(
    params: dict[str, Any],
    *,
    vendors_override: list[str] | None = None,
    years_back: int | None = None,
    start_date: dt.date | None = None,
    end_date: dt.date | None = None,
    max_per_vendor: int | None = None,
    only_keyword_matches: bool | None = None,
) -> USASpendingConfig:
    today = dt.date.today()
    usaspending = params.get("usaspending") or {}
    years = years_back or int(usaspending.get("years_back") or 20)
    end = end_date or today
    start = max(start_date or (end - dt.timedelta(days=365 * years)), USASPENDING_SEARCH_FLOOR)

    vendors = vendor_searches(params, vendors_override)
    keywords = [str(item) for item in params.get("monitored_keywords") or DEFAULT_KEYWORDS]

    return USASpendingConfig(
        vendors=vendors,
        keywords=keywords,
        start_date=start,
        end_date=end,
        award_type_codes=[str(item) for item in usaspending.get("award_type_codes") or ["A", "B", "C", "D"]],
        max_per_vendor=max_per_vendor or int(usaspending.get("max_per_vendor") or 100),
        page_limit=min(int(usaspending.get("page_limit") or 100), 100),
        min_award_amount=int(usaspending.get("min_award_amount") or 0),
        recompete_months=int(usaspending.get("recompete_months") or 36),
        near_expiry_months=int(usaspending.get("near_expiry_months") or 18),
        only_keyword_matches=bool(usaspending.get("only_keyword_matches") if only_keyword_matches is None else only_keyword_matches),
    )


def vendor_searches(params: dict[str, Any], vendors_override: list[str] | None = None) -> list[VendorSearch]:
    if vendors_override:
        return [VendorSearch(name=name, queries=[name]) for name in vendors_override if name]

    searches: list[VendorSearch] = []
    for item in params.get("vendors") or []:
        if isinstance(item, str):
            searches.append(VendorSearch(name=item, queries=[item]))
            continue
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        aliases = [str(alias).strip() for alias in item.get("aliases") or [] if str(alias).strip()]
        queries = sorted({name, *aliases}, key=str.lower)
        searches.append(VendorSearch(name=name, queries=queries))
    return searches


def fetch_vendor_contracts(config: USASpendingConfig, progress: Callable[[str], None] | None = None) -> list[dict[str, str]]:
    contracts: list[dict[str, str]] = []
    seen: set[str] = set()

    for vendor in config.vendors:
        vendor_count = 0
        emit(progress, f"vendor {vendor.name}: {len(vendor.queries)} queries")
        for query in vendor.queries:
            page = 1
            while vendor_count < config.max_per_vendor:
                payload = build_awards_payload(config, query, page)
                data = http_json(USASPENDING_AWARDS_URL, payload)
                results = data.get("results") or []
                if not results:
                    break

                for item in results:
                    contract = normalize_award(item, vendor.name, query, config)
                    if int(contract["award_amount"] or 0) < config.min_award_amount:
                        continue
                    if config.only_keyword_matches and not contract["matched_keywords"]:
                        continue
                    key = contract["generated_internal_id"] or contract["award_id"]
                    if not key or key in seen:
                        continue
                    seen.add(key)
                    contracts.append(contract)
                    vendor_count += 1
                    if vendor_count >= config.max_per_vendor:
                        break

                if not data.get("page_metadata", {}).get("hasNext"):
                    break
                page += 1
        emit(progress, f"vendor {vendor.name}: {vendor_count} contracts")

    return sorted(
        contracts,
        key=lambda row: (int(row["relevance_score"]), int(row["award_amount"]), row["end_date"]),
        reverse=True,
    )


def build_awards_payload(config: USASpendingConfig, recipient_query: str, page: int) -> dict[str, Any]:
    return {
        "filters": {
            "time_period": [{"start_date": config.start_date.isoformat(), "end_date": config.end_date.isoformat()}],
            "award_type_codes": config.award_type_codes,
            "recipient_search_text": [recipient_query],
        },
        "fields": AWARD_FIELDS,
        "page": page,
        "limit": config.page_limit,
        "sort": "Award Amount",
        "order": "desc",
        "subawards": False,
    }


def normalize_award(item: dict[str, Any], vendor_name: str, vendor_query: str, config: USASpendingConfig) -> dict[str, str]:
    generated_id = str(item.get("generated_internal_id") or "")
    award_id = str(item.get("Award ID") or "")
    amount = int(float(item.get("Award Amount") or 0))
    description = clean_text(item.get("Description") or "", 700)
    matched = keyword_hits(
        " ".join(
            str(item.get(field) or "")
            for field in ["Recipient Name", "Awarding Agency", "Awarding Sub Agency", "Description", "naics_code", "psc_code"]
        ),
        config.keywords,
    )
    months_to_end = months_until(item.get("End Date"))
    recompete = recompete_signal(months_to_end, config.recompete_months, config.near_expiry_months)
    score = relevance_score(matched, amount, recompete)
    row_id = stable_id(generated_id or award_id or f"{vendor_name}-{description}")

    return {
        "id": row_id,
        "vendor_name": vendor_name,
        "vendor_query": vendor_query,
        "recipient_name": clean_text(item.get("Recipient Name") or "", 180),
        "award_id": award_id,
        "generated_internal_id": generated_id,
        "awarding_agency": clean_text(item.get("Awarding Agency") or "", 180),
        "awarding_sub_agency": clean_text(item.get("Awarding Sub Agency") or "", 180),
        "award_amount": str(amount),
        "start_date": iso_date(item.get("Start Date")),
        "end_date": iso_date(item.get("End Date")),
        "months_to_end": "" if months_to_end is None else str(months_to_end),
        "recompete_signal": recompete,
        "naics_code": str(item.get("naics_code") or ""),
        "psc_code": str(item.get("psc_code") or ""),
        "description": description,
        "matched_keywords": ";".join(matched),
        "relevance_score": str(score),
        "source_url": "https://www.usaspending.gov/search/",
        "last_checked_at": now_iso(),
    }


def upsert_contracts(path: Path, contracts: list[dict[str, str]]) -> tuple[int, int, int]:
    existing = read_csv(path)
    by_key = {contract_key(row): row for row in existing if contract_key(row)}
    added = 0
    updated = 0

    for contract in contracts:
        key = contract_key(contract)
        old = by_key.get(key)
        if old is None:
            by_key[key] = contract
            added += 1
            continue
        merged = dict(old)
        changed = False
        for field in CONTRACT_FIELDS:
            value = contract.get(field, "")
            if merged.get(field, "") != value:
                merged[field] = value
                changed = True
        if changed:
            updated += 1
        by_key[key] = merged

    rows = sorted(
        by_key.values(),
        key=lambda row: (int_or_zero(row.get("relevance_score")), int_or_zero(row.get("award_amount")), row.get("end_date", "")),
        reverse=True,
    )
    write_csv(path, CONTRACT_FIELDS, rows)
    return added, updated, len(rows)


def contract_key(row: dict[str, str]) -> str:
    return row.get("generated_internal_id") or row.get("award_id") or row.get("id", "")


def http_json(url: str, payload: dict[str, Any], timeout: int = 60) -> Any:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Accept": "application/json", "Content-Type": "application/json", "User-Agent": USER_AGENT},
    )
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read(800).decode("utf-8", "replace")
            raise RuntimeError(f"HTTP {exc.code} from {url}: {body}") from exc
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
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def keyword_hits(text: str, keywords: list[str]) -> list[str]:
    lower = text.lower()
    return sorted({keyword for keyword in keywords if keyword and keyword.lower() in lower}, key=str.lower)


def relevance_score(keywords: list[str], award_amount: int, recompete: str) -> int:
    score = min(50, len(keywords) * 8)
    if award_amount >= 10_000_000:
        score += 20
    elif award_amount >= 1_000_000:
        score += 12
    elif award_amount >= 250_000:
        score += 6
    if recompete == "Expiring soon":
        score += 25
    elif recompete == "Recompete watch":
        score += 18
    elif recompete == "Expired/past award":
        score += 4
    return min(score, 100)


def recompete_signal(months_to_end: int | None, recompete_months: int, near_expiry_months: int) -> str:
    if months_to_end is None:
        return "Unknown end date"
    if months_to_end < 0:
        return "Expired/past award"
    if months_to_end <= near_expiry_months:
        return "Expiring soon"
    if months_to_end <= recompete_months:
        return "Recompete watch"
    return "Longer-term contract"


def months_until(value: Any) -> int | None:
    parsed = parse_date(value)
    if not parsed:
        return None
    today = dt.date.today()
    return (parsed.year - today.year) * 12 + (parsed.month - today.month)


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


def iso_date(value: Any) -> str:
    parsed = parse_date(value)
    return parsed.isoformat() if parsed else ""


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def clean_text(value: Any, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "..."


def stable_id(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_-]+", "-", value.strip())[:120].strip("-")
    return f"contract-{cleaned or 'record'}"


def int_or_zero(value: Any) -> int:
    try:
        return int(float(value or 0))
    except ValueError:
        return 0


def emit(progress: Callable[[str], None] | None, message: str) -> None:
    if progress:
        progress(message)
