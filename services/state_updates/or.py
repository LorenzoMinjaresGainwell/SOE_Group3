from __future__ import annotations

import json
import re
import urllib.parse
from typing import Any, Callable

from services.state_http import fetch_url
from services.state_updates import state_update_record

OHP_BASE = "https://www.oregon.gov/oha/HSD/OHP"
MEDICAID_POLICY_BASE = "https://www.oregon.gov/oha/HSD/Medicaid-Policy"
ANNOUNCEMENTS_LIST = "/OHA/HSD/OHP/Announcements"
PROVIDER_MATTERS_LIST = "/oha/HSD/OHP/Lists/ProviderMatters"
STATE_PLANS_LIST = "/oha/HSD/Medicaid-Policy/StatePlans"


def fetch_updates(
    *,
    keywords: list[str],
    max_records: int,
    progress: Callable[[str], None] | None = None,
) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    scanned = 0

    announcements = fetch_sharepoint_items(
        base=OHP_BASE,
        list_url=ANNOUNCEMENTS_LIST,
        orderby="Issue_x0020_Date desc",
        top=min(max(max_records, 50), 150),
    )
    scanned += len(announcements)
    for row in announcements:
        records.append(announcement_record(row, keywords))
    emit(progress, f"OR: scanned {len(announcements)} OHP announcement/public notice rows")

    provider_matters = fetch_sharepoint_items(
        base=OHP_BASE,
        list_url=PROVIDER_MATTERS_LIST,
        orderby="Issue_x0020_Date desc",
        top=min(max(max_records, 50), 150),
    )
    scanned += len(provider_matters)
    for row in provider_matters:
        records.append(provider_matters_record(row, keywords))
    emit(progress, f"OR: scanned {len(provider_matters)} Provider Matters rows")

    state_plans = fetch_sharepoint_items(
        base=MEDICAID_POLICY_BASE,
        list_url=STATE_PLANS_LIST,
        orderby="Date desc",
        top=min(max(max_records // 2, 50), 150),
    )
    scanned += len(state_plans)
    for row in state_plans:
        records.append(state_plan_record(row, keywords))
    emit(progress, f"OR: scanned {len(state_plans)} Medicaid state plan amendment rows")
    emit(progress, f"OR: normalized {len(records)} records from {scanned} scanned rows")
    return records[:max_records]


def fetch_sharepoint_items(*, base: str, list_url: str, orderby: str, top: int) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    next_url = sharepoint_items_url(base=base, list_url=list_url, orderby=orderby, top=min(top, 100))
    while next_url and len(items) < top:
        result = fetch_url(
            next_url,
            timeout=30,
            byte_limit=3_000_000,
            headers={"Accept": "application/json;odata=nometadata", "User-Agent": "Mozilla/5.0"},
        )
        result.raise_for_status()
        data = json.loads(result.body_text())
        batch = list(data.get("value") or [])
        items.extend(batch)
        next_url = data.get("odata.nextLink")
        if not batch:
            break
    return items[:top]


def sharepoint_items_url(*, base: str, list_url: str, orderby: str, top: int) -> str:
    query = urllib.parse.urlencode({"$top": str(top), "$orderby": orderby})
    return f"{base}/_api/web/GetList('{list_url}')/items?{query}"


def announcement_record(row: dict[str, Any], keywords: list[str]) -> dict[str, str]:
    url = url_field(row.get("URL"))
    title = url.get("description") or row.get("Title") or "OHP announcement"
    summary = " ".join(clean_text(part) for part in [row.get("Category"), row.get("Applies_x0020_To"), row.get("Meta_x0020_Description")] if part)
    source_url = url.get("url") or "https://www.oregon.gov/oha/HSD/OHP/Pages/Announcements.aspx"
    return state_update_record(
        state="OR",
        source="or_oha_ohp_announcements",
        source_record_id=f"ohp-announcement:{row.get('ID') or row.get('Id')}",
        record_type=or_record_type(title, summary, default="medicaid_notice"),
        title=str(title),
        agency="Oregon Health Authority / Oregon Health Plan",
        summary=summary,
        posted_date=row.get("Issue_x0020_Date") or "",
        updated_date=row.get("Modified") or "",
        document_url=source_url if source_url.lower().endswith(".pdf") else "",
        source_url=absolute_url(source_url, OHP_BASE),
        keywords=keywords,
        raw=raw_subset(row, ["ID", "Id", "Issue_x0020_Date", "Category", "Applies_x0020_To", "Modified", "URL"]),
    )


def provider_matters_record(row: dict[str, Any], keywords: list[str]) -> dict[str, str]:
    url = url_field(row.get("URL"))
    title = url.get("description") or "Provider Matters update"
    summary = strip_html(row.get("Comments") or "")
    source_url = url.get("url") or "https://www.oregon.gov/oha/HSD/OHP/Pages/Provider-Matters.aspx"
    return state_update_record(
        state="OR",
        source="or_oha_provider_matters",
        source_record_id=f"ohp-provider-matters:{row.get('ID') or row.get('Id')}",
        record_type="provider_bulletin",
        title=str(title),
        agency="Oregon Health Authority / Oregon Health Plan",
        summary=summary,
        posted_date=row.get("Issue_x0020_Date") or "",
        updated_date=row.get("Modified") or "",
        source_url=absolute_url(source_url, OHP_BASE),
        keywords=keywords,
        raw=raw_subset(row, ["ID", "Id", "Issue_x0020_Date", "Modified", "URL"]),
    )


def state_plan_record(row: dict[str, Any], keywords: list[str]) -> dict[str, str]:
    url = url_field(row.get("URL"))
    title = row.get("Title") or url.get("description") or "Oregon Medicaid State Plan Amendment"
    category = str(row.get("Category") or "")
    source_url = url.get("url") or "https://www.oregon.gov/oha/HSD/Medicaid-Policy/Pages/State-Plans.aspx"
    return state_update_record(
        state="OR",
        source="or_oha_state_plan_amendments",
        source_record_id=f"or-spa:{row.get('ID') or row.get('Id')}",
        record_type="spa_notice",
        title=str(title),
        agency="Oregon Health Authority / Medicaid Policy",
        summary=category,
        posted_date=row.get("Date") or row.get("Created") or "",
        updated_date=row.get("Modified") or "",
        document_url=absolute_url(source_url, MEDICAID_POLICY_BASE) if source_url.lower().endswith(".pdf") else "",
        source_url=absolute_url(source_url, MEDICAID_POLICY_BASE),
        keywords=keywords,
        raw=raw_subset(row, ["ID", "Id", "Title", "Date", "Category", "Created", "Modified", "URL"]),
    )


def or_record_type(title: Any, summary: Any, *, default: str) -> str:
    text = f"{title or ''} {summary or ''}".lower()
    if "public comment" in text or "public notice" in text or "notice" in text:
        return "public_comment_notice"
    if "waiver" in text or "1115" in text:
        return "waiver_notice"
    if "state plan amendment" in text or " spa" in f" {text}":
        return "spa_notice"
    if "provider" in text or "provider matters" in text:
        return "provider_bulletin"
    if "grant" in text or "funding" in text or "award" in text:
        return "grant_notice"
    return default


def url_field(value: Any) -> dict[str, str]:
    if isinstance(value, dict):
        return {"description": clean_text(value.get("Description") or value.get("description") or ""), "url": clean_text(value.get("Url") or value.get("url") or "")}
    return {"description": "", "url": clean_text(value)}


def absolute_url(url: str, base: str) -> str:
    text = clean_text(url)
    if not text:
        return ""
    if text.startswith("http://") or text.startswith("https://"):
        return text
    return urllib.parse.urljoin(base.rstrip("/") + "/", text)


def strip_html(value: Any) -> str:
    text = str(value or "")
    text = re.sub(r"(?is)<script.*?</script>|<style.*?</style>", " ", text)
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>|</tr>|</h[1-6]>", " ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    return clean_text(text)


def clean_text(value: Any) -> str:
    text = str(value or "")
    replacements = {"&amp;": "&", "&quot;": '"', "&#39;": "'", "&nbsp;": " ", "&#58;": ":"}
    for old, new in replacements.items():
        text = text.replace(old, new)
    return re.sub(r"\s+", " ", text).strip()


def raw_subset(row: dict[str, Any], keys: list[str]) -> dict[str, Any]:
    return {key: row.get(key) for key in keys if key in row}


def emit(progress: Callable[[str], None] | None, message: str) -> None:
    if progress:
        progress(message)
