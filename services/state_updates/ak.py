from __future__ import annotations

import datetime as dt
import re
from typing import Any, Callable

from services.state_updates import sort_key, state_update_record
from services.state_updates.common import clean_text, fetch_text, head_last_modified, iso_date_text, parse_links, record_type_for, source_id_from_url, title_from_url, unique_records

PROVIDER_RESOURCES_URL = "https://extranet-sp.dhss.alaska.gov/hcs/medicaidalaska/Provider/Sites/ProviderResources.html"
STATE_PLAN_URL = "https://health.alaska.gov/en/education/medicaid-state-plan/"
AMCCI_URL = "https://health.alaska.gov/en/services/alaska-medicaid-coordinated-care/"
RATE_REVIEW_URL = "https://health.alaska.gov/en/office-of-the-commissioner/office-of-rate-review/"

AK_CONTEXT_TERMS = [
    "alaska medicaid",
    "medicaid",
    "provider",
    "state plan",
    "spa",
    "tribal consultation",
    "behavioral health",
    "rate",
    "rural",
    "hospital",
]

AK_SOURCES = [
    {
        "key": "ak_medicaid_provider_updates",
        "url": PROVIDER_RESOURCES_URL,
        "agency": "Alaska Department of Health / Alaska Medicaid",
        "record_type": "provider_bulletin",
        "terms": ["/provider/updates/"],
        "source_note": "Official Alaska Medicaid provider resources page; only Provider/Updates documents are normalized.",
    },
    {
        "key": "ak_doh_medicaid_state_plan",
        "url": STATE_PLAN_URL,
        "agency": "Alaska Department of Health",
        "record_type": "spa_notice",
        "terms": ["state plan", "spa", "tribal consultation", "behavioral health", "medication assisted treatment"],
        "source_note": "Official Alaska Department of Health Medicaid State Plan page with SPA and consultation documents.",
    },
    {
        "key": "ak_doh_medicaid_coordinated_care",
        "url": AMCCI_URL,
        "agency": "Alaska Department of Health",
        "record_type": "policy_update",
        "terms": ["medicaid", "provider", "state plan", "epsdt", "respite", "funding", "behavioral health", "rate"],
        "source_note": "Official Alaska Medicaid Coordinated Care Initiative page with dated Medicaid/provider documents.",
    },
    {
        "key": "ak_doh_rate_review",
        "url": RATE_REVIEW_URL,
        "agency": "Alaska Department of Health Office of Rate Review",
        "record_type": "provider_bulletin",
        "terms": ["medicaid", "rate", "rural", "fqh", "hospital", "behavioral health"],
        "source_note": "Official Alaska Office of Rate Review page with Medicaid rate and rural facility documents.",
    },
]

GENERIC_TITLES = {"here", "faq", "pdf", "document", "publications", "resources"}


def fetch_updates(
    *,
    keywords: list[str],
    max_records: int,
    progress: Callable[[str], None] | None = None,
) -> list[dict[str, str]]:
    limit = max(1, max_records)
    records: list[dict[str, str]] = []
    scanned = 0

    for source in AK_SOURCES:
        try:
            rows = fetch_source_rows(source)
        except Exception as exc:
            emit(progress, f"AK: {source['key']} failed: {exc}")
            continue
        scanned += len(rows)
        accepted = [row_to_record(row, source, keywords) for row in rows if keep_row(row, source, keywords)]
        records.extend(accepted)
        emit(progress, f"AK {source['key']}: scanned {len(rows)} links, normalized {len(accepted)} records")

    output = unique_records(records)
    emit(progress, f"AK: normalized {len(output)} records from {scanned} scanned links")
    return sorted(output, key=sort_key, reverse=True)[:limit]


def fetch_source_rows(source: dict[str, Any]) -> list[dict[str, str]]:
    markup = fetch_text(str(source["url"]), timeout=30, byte_limit=800_000)
    rows: list[dict[str, str]] = []
    for link in parse_links(markup, str(source["url"])):
        url = clean_text(link.href)
        if not is_document_url(url):
            continue
        title = useful_title(link.text, url)
        if not title:
            continue
        text = " ".join([title, url])
        date = date_from_text(text)
        if not date and likely_policy_document(title, url, source):
            date = head_last_modified(url, timeout=10)
        if not date:
            continue
        rows.append({"title": title, "url": url, "date": date})
    return rows


def keep_row(row: dict[str, str], source: dict[str, Any], keywords: list[str]) -> bool:
    row_text = " ".join([row.get("title", ""), row.get("url", "")]).lower()
    if not any(term in row_text for term in source.get("terms", [])):
        return False
    context_text = " ".join([row_text, str(source.get("source_note", ""))])
    return has_keyword_or_context(context_text, keywords, AK_CONTEXT_TERMS)


def row_to_record(row: dict[str, str], source: dict[str, Any], keywords: list[str]) -> dict[str, str]:
    title = row.get("title", "")
    url = row.get("url", "")
    text = " ".join([title, url])
    rtype = record_type_for(text, str(source.get("record_type", "policy_update")))
    effective_date = row.get("date", "") if "effective" in text.lower() else ""
    return state_update_record(
        state="AK",
        source=str(source["key"]),
        source_record_id=source_id_from_url(url) or title,
        record_type=rtype,
        title=title,
        agency=str(source.get("agency", "Alaska Department of Health")),
        summary=f"Official Alaska Medicaid/health policy document from {source['key']}.",
        posted_date=row.get("date", "") if not effective_date else "",
        updated_date=row.get("date", "") if effective_date else "",
        effective_date=effective_date,
        comment_required="comment" in text.lower() or "consultation" in text.lower(),
        document_url=url,
        source_url=str(source["url"]),
        keywords=keywords,
        raw={"source_page": source["url"], "source_note": source.get("source_note", "")},
    )


def is_document_url(url: str) -> bool:
    lower = url.lower()
    return (
        "health.alaska.gov/media/" in lower
        or "extranet-sp.dhss.alaska.gov/hcs/medicaidalaska/provider/updates/" in lower
    )


def likely_policy_document(title: str, url: str, source: dict[str, Any]) -> bool:
    text = " ".join([title, url, str(source.get("source_note", ""))]).lower()
    return any(term in text for term in AK_CONTEXT_TERMS)


def useful_title(value: str, url: str) -> str:
    title = clean_text(value)
    if title.lower() in GENERIC_TITLES or len(title) < 5:
        title = title_from_url(url)
    title = re.sub(r"\s+PDF\s+\d{1,2}/\d{1,2}/\d{4}$", "", title, flags=re.I)
    return clean_text(title)


def has_keyword_or_context(text: str, keywords: list[str], context_terms: list[str]) -> bool:
    lower = clean_text(text).lower()
    if any(str(keyword).strip().lower() in lower for keyword in keywords if str(keyword).strip()):
        return True
    return any(term in lower for term in context_terms)


def date_from_text(value: str) -> str:
    text = clean_text(value)
    parsed = iso_date_text(text)
    if parsed:
        return parsed
    for pattern in (r"(?<!\d)(20\d{2})(\d{2})(\d{2})(?!\d)", r"(?<!\d)(\d{1,2})[._-](\d{1,2})[._-](20\d{2}|\d{2})(?!\d)"):
        match = re.search(pattern, text)
        if not match:
            continue
        if len(match.group(1)) == 4:
            year, month, day = int(match.group(1)), int(match.group(2)), int(match.group(3))
        else:
            month, day = int(match.group(1)), int(match.group(2))
            year = int(match.group(3))
            if year < 100:
                year += 2000
        try:
            return dt.date(year, month, day).isoformat()
        except ValueError:
            continue
    return ""


def emit(progress: Callable[[str], None] | None, message: str) -> None:
    if progress:
        progress(message)
