from __future__ import annotations

import re
from typing import Callable

from services.state_updates import emit, state_update_record
from services.state_updates.common import absolute_url, clean_text, fetch_text, source_id_from_url

UPDATE_URL = "https://www.health.ny.gov/health_care/medicaid/program/update/main.htm"
AGENCY = "New York State Department of Health"
ISSUE_RE = re.compile(r"(?is)<h2>\s*Current Issue:\s*(?P<issue>[^<]+)</h2>\s*<ul>(?P<body>.*?)</ul>")
LINK_RE = re.compile(r'(?is)<a\s+[^>]*href="(?P<href>[^"]+)"[^>]*>(?P<label>.*?)</a>')
MONTHS = {name.lower(): index for index, name in enumerate(("", "January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December")) if name}


def parse_current_issue(markup: str) -> dict[str, str] | None:
    match = ISSUE_RE.search(markup)
    if not match:
        return None
    issue = clean_text(match.group("issue"))
    parts = issue.split()
    if len(parts) != 2 or parts[0].lower() not in MONTHS or not parts[1].isdigit():
        return None
    links = {clean_text(link.group("label")).lower(): absolute_url(UPDATE_URL, link.group("href")) for link in LINK_RE.finditer(match.group("body"))}
    web_url = next((url for label, url in links.items() if "web version" in label), "")
    pdf_url = next((url for label, url in links.items() if "pdf" in label), "")
    return {"issue": issue, "posted_date": f"{int(parts[1]):04d}-{MONTHS[parts[0].lower()]:02d}-01", "web_url": web_url, "pdf_url": pdf_url}


def fetch_updates(*, keywords: list[str], max_records: int, progress: Callable[[str], None] | None = None) -> list[dict[str, str]]:
    if max_records <= 0:
        return []
    issue = parse_current_issue(fetch_text(UPDATE_URL, timeout=30, byte_limit=500_000))
    if not issue:
        emit(progress, "NY Medicaid Update: current issue was not present")
        return []
    record = state_update_record(
        state="NY", source="ny_doh_medicaid_update", source_record_id=source_id_from_url(issue["web_url"]) or issue["issue"],
        record_type="provider_bulletin", title=f"New York State Medicaid Update — {issue['issue']}", agency=AGENCY,
        summary="Official monthly Medicaid Update for enrolled providers.", posted_date=issue["posted_date"],
        document_url=issue["pdf_url"] or issue["web_url"], source_url=issue["web_url"] or UPDATE_URL, keywords=keywords,
        raw={"index_page": UPDATE_URL, "web_version": issue["web_url"]},
    )
    emit(progress, "NY Medicaid Update: normalized 1 current monthly issue")
    return [record]
