from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import Any, Callable

from services.state_contracts.tabular import limit_records, normalize_awarded_record, unique_terms
from services.state_http import fetch_url
from services.state_normalization import clean_text

PAGE_URL = "https://purchasing.idaho.gov/statewide-contracts/"
AJAX_URL = "https://purchasing.idaho.gov/wp-admin/admin-ajax.php"
USER_AGENT = "soe-group3-id-statewide-contracts/0.1"
SOURCE_NOTE = (
    "Official Idaho Division of Purchasing active statewide-contract search and detail pages. "
    "Only cards and details explicitly marked Active are accepted; detail pages expose vendor, "
    "contract numbers, effective date, expiration date, and contract description."
)
MAX_TERMS = 40
MAX_RESULTS_PER_TERM = 50


class ContractCardParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.depth = 0
        self.current: dict[str, Any] | None = None
        self.targets: list[str] = []
        self.rows: list[dict[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        classes = set((values.get("class") or "").split())
        if tag == "div" and "contract-card" in classes and self.current is None:
            self.current = {"vendor_parts": [], "detail_parts": [], "description_parts": [], "portfolio_parts": [], "url": ""}
            self.depth = 1
        elif self.current is not None and tag == "div":
            self.depth += 1

        target = self.targets[-1] if self.targets else ""
        if self.current is not None:
            if tag == "a" and "contract-title-link" in classes:
                target = "vendor_parts"
                self.current["url"] = clean_text(values.get("href"), 500)
            elif "contract-details" in classes:
                target = "detail_parts"
            elif "description" in classes:
                target = "description_parts"
            elif "portfolio-pill" in classes:
                target = "portfolio_parts"
        self.targets.append(target)

    def handle_data(self, data: str) -> None:
        if self.current is not None and self.targets and self.targets[-1]:
            self.current[self.targets[-1]].append(data)

    def handle_endtag(self, tag: str) -> None:
        if self.targets:
            self.targets.pop()
        if self.current is not None and tag == "div":
            self.depth -= 1
            if self.depth == 0:
                row = {
                    "vendor_name": clean_text(" ".join(self.current["vendor_parts"]), 180),
                    "details": clean_text(" ".join(self.current["detail_parts"]), 500),
                    "description": clean_text(" ".join(self.current["description_parts"]), 2000),
                    "portfolio": clean_text(" | ".join(self.current["portfolio_parts"]), 1000),
                    "url": self.current["url"],
                }
                if row["vendor_name"] and row["url"]:
                    self.rows.append(row)
                self.current = None


class DetailTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        value = clean_text(data, 2000)
        if value:
            self.parts.append(value)


def fetch_contracts(
    *, vendor_terms: list[str], keywords: list[str], max_per_vendor: int,
    progress: Callable[[str], None] | None = None,
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    per_term = min(MAX_RESULTS_PER_TERM, max(1, max_per_vendor * 3))
    for term in unique_terms(vendor_terms + keywords)[:MAX_TERMS]:
        cards = fetch_cards(term, per_term)
        detailed = 0
        for card in cards[:per_term]:
            detail = fetch_detail(card["url"])
            row = {**card, **detail}
            if is_active(row):
                rows.append(row)
                detailed += 1
        if progress:
            progress(f"ID active statewide contracts {term!r}: scanned {len(cards[:per_term])}, accepted {detailed}")
    records = [normalize_row(row, vendor_terms=vendor_terms, keywords=keywords) for row in rows]
    return limit_records([record for record in records if record], max_per_vendor, vendor_terms)


def fetch_cards(term: str, limit: int) -> list[dict[str, str]]:
    result = fetch_url(
        AJAX_URL, method="POST",
        data={"action": "statewide_contracts_filter", "paged": "1", "s": term,
              "contract_status": "active", "posts_per_page": str(min(MAX_RESULTS_PER_TERM, max(1, limit)))},
        headers={"Accept": "text/html", "Referer": PAGE_URL}, timeout=20,
        byte_limit=1_000_000, user_agent=USER_AGENT,
    )
    result.raise_for_status()
    if result.truncated:
        raise RuntimeError("ID contract search exceeded byte limit")
    parser = ContractCardParser()
    parser.feed(result.body_text())
    return parser.rows[: min(MAX_RESULTS_PER_TERM, max(1, limit))]


def fetch_detail(url: str) -> dict[str, str]:
    if not url.startswith("https://purchasing.idaho.gov/statewide-contract/"):
        return {}
    result = fetch_url(url, headers={"Accept": "text/html", "Referer": PAGE_URL}, timeout=20,
                       byte_limit=500_000, user_agent=USER_AGENT)
    result.raise_for_status()
    if result.truncated:
        return {}
    parser = DetailTextParser()
    parser.feed(result.body_text())
    text = " | ".join(parser.parts).replace(": |", ":")
    return {
        "luma_contract_number": labeled(text, "Luma Contract Number"),
        "dop_contract_number": labeled(text, "DOP Contract Number"),
        "effective_date": labeled(text, "Effective Date"),
        "expiration_date": labeled(text, "Expiration Date"),
        "detail_status": labeled(text, "Status"),
    }


def labeled(text: str, label: str) -> str:
    match = re.search(rf"(?:^|\|)\s*{re.escape(label)}\s*:\s*([^|]+)", text, re.IGNORECASE)
    return clean_text(match.group(1), 180) if match else ""


def is_active(row: dict[str, Any]) -> bool:
    list_status = clean_text(row.get("details"), 500).split("|", 1)[0].strip().lower()
    detail_status = clean_text(row.get("detail_status"), 100).lower()
    return list_status == "active" and detail_status == "active"


def normalize_row(row: dict[str, Any], *, vendor_terms: list[str], keywords: list[str]) -> dict[str, str]:
    if not is_active(row):
        return {}
    number = clean_text(row.get("dop_contract_number") or row.get("luma_contract_number"), 180)
    vendor = clean_text(row.get("vendor_name"), 180)
    title = clean_text(row.get("portfolio") or row.get("description") or number, 500)
    if not number or not vendor or not title:
        return {}
    return normalize_awarded_record(
        row, state="ID", source="Idaho Division of Purchasing Active Statewide Contracts",
        source_key="id_dop_active_statewide_contracts", source_note=SOURCE_NOTE, source_url=PAGE_URL,
        vendor_terms=vendor_terms, keywords=keywords, contract_number=number,
        source_record_id=f"{number}-{vendor}", title=title, vendor_name=vendor,
        agency="Idaho Division of Purchasing", start_date=row.get("effective_date"),
        end_date=row.get("expiration_date"), document_url=row.get("url"),
        document_type="Idaho Active Statewide Contract", contract_record_type="master_agreement",
    )
