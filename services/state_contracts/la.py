from __future__ import annotations

import http.cookiejar
import urllib.parse
import urllib.request
from typing import Any, Callable

from services.state_contracts.html_table import parse_table_rows
from services.state_contracts.tabular import limit_records, normalize_awarded_record, unique_terms
from services.state_http import read_limited
from services.state_normalization import clean_text

SOURCE_URL = "https://wwwcfprd.doa.louisiana.gov/osp/lapac/eCat/dsp_eCatSearchLagov.cfm"
SOURCE_NOTE = "Official Louisiana Office of State Procurement LaGov state-contract catalog; current contract results expose contract number, vendor and effective date range."
USER_AGENT = "soe-group3-la-state-contracts/0.1"
MAX_QUERIES = 12
MAX_BYTES = 2_000_000


def fetch_contracts(*, vendor_terms: list[str], keywords: list[str], max_per_vendor: int, progress: Callable[[str], None] | None = None) -> list[dict[str, str]]:
    if max_per_vendor <= 0:
        return []
    queries = [(term, "vendor") for term in unique_terms(vendor_terms)] + [(term, "description") for term in unique_terms(keywords)]
    records: list[dict[str, str]] = []
    for term, field in queries[:MAX_QUERIES]:
        for row in fetch_rows(term, field=field):
            record = normalize_row(row, vendor_terms=vendor_terms, keywords=keywords)
            if record:
                records.append(record)
    if progress:
        progress(f"LA LaGov contracts: searched {min(len(queries), MAX_QUERIES)} bounded terms; matched {len(records)} current contracts")
    return limit_records(records, max_per_vendor, vendor_terms)


def fetch_rows(term: str, *, field: str) -> list[dict[str, Any]]:
    # The public ColdFusion application requires its ordinary anonymous session cookie.
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    headers = {"User-Agent": USER_AGENT, "Accept": "text/html,*/*"}
    with opener.open(urllib.request.Request(SOURCE_URL, headers=headers), timeout=20) as response:
        read_limited(response, 100_000)
    names = ["Contract", "Description", "TNumberDescription", "TNumber", "VendorName", "LineNumber", "Family", "Class", "LineItemDescription", "CatalogGUID", "SuppPartNo", "Region", "CatItemDescription"]
    form = {name: "" for name in names}
    form.update({"isSearch": "TRUE", "searchType": "LaGOV", "Tab": "2", "Coop": "0", "SEBD": "0", "VSE": "0", "SEHI": "0", "DVSE": "0", "Emergency": "0", "searchForLaGOV": "Contracts", "btnSearch": "Find It"})
    form["VendorName" if field == "vendor" else "Description"] = term
    request = urllib.request.Request(SOURCE_URL, data=urllib.parse.urlencode(form).encode(), headers={**headers, "Content-Type": "application/x-www-form-urlencoded"})
    with opener.open(request, timeout=30) as response:
        body, _ = read_limited(response, MAX_BYTES)
    return parse_results(body.decode("utf-8", "replace"))


def parse_results(markup: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for cells in parse_table_rows(markup, SOURCE_URL):
        if len(cells) != 6 or clean_text(cells[0]["text"]).lower() == "contract #":
            continue
        number, title, vendor, t_number, dates, _ = (cell["text"] for cell in cells)
        if not cells[0]["links"] or " - " not in dates:
            continue
        start_date, end_date = (part.strip() for part in dates.split(" - ", 1))
        rows.append({"contract_number": number, "title": title, "vendor": vendor, "t_number": t_number, "start_date": start_date, "end_date": end_date, "document_url": cells[0]["links"][0]})
    return rows


def normalize_row(row: dict[str, Any], *, vendor_terms: list[str], keywords: list[str]) -> dict[str, str]:
    return normalize_awarded_record(
        row, state="LA", source="Louisiana LaGov Contract Catalog", source_key="la_lagov_contracts",
        source_note=SOURCE_NOTE, source_url=SOURCE_URL, vendor_terms=vendor_terms, keywords=keywords,
        contract_number=row.get("contract_number"), title=row.get("title"), vendor_name=row.get("vendor"),
        agency="Louisiana Office of State Procurement", start_date=row.get("start_date"), end_date=row.get("end_date"),
        document_url=row.get("document_url"), document_type="Louisiana State Contract", contract_record_type="master_agreement",
    )
