from __future__ import annotations

import datetime as dt
import html
import re
import urllib.parse
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any, Callable

from services.state_http import fetch_url
from services.state_normalization import clean_text, compact_raw_json, iso_date, keyword_hits, parse_date, stable_id, term_matches

USER_AGENT = "Mozilla/5.0 soe-group3-publicpurchase-opportunities/0.1"
PUBLIC_PURCHASE_SOURCE_NOTE = (
    "Public Purchase publicInfo page is public and renders current bid rows by JavaScript; parser removes spans hidden "
    "by publicInfo.js action(...). Bid detail pages return login/401 from CLI, so the public listing is the source URL."
)


@dataclass(frozen=True)
class PublicPurchaseConfig:
    state: str
    source_name: str
    public_info_url: str
    source_key: str
    agency: str = ""
    official_source_url: str = ""
    source_note: str = PUBLIC_PURCHASE_SOURCE_NOTE


@dataclass(frozen=True)
class PublicPurchaseRow:
    bid_id: str
    source_record_id: str
    title: str
    description: str
    detail_url: str
    start_date: str
    end_date: str
    time_left: str
    addendums: str


def fetch_public_purchase_opportunities(
    *,
    config: PublicPurchaseConfig,
    keywords: list[str],
    days_back: int,
    max_records: int,
    progress: Callable[[str], None] | None = None,
) -> list[dict[str, str]]:
    rows = fetch_public_rows(config)
    emit(progress, f"{config.state} PublicPurchase publicInfo: {len(rows)} public rows")

    records: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in rows:
        record = normalize_row(row, config=config, keywords=keywords)
        if not record.get("source_record_id") or record["id"] in seen:
            continue
        if not is_open_or_recent(record["posted_date"], record["due_date"], record["status"], days_back):
            continue
        if keywords and not record["matched_keywords"]:
            continue
        if false_keyword_hit(record) or not useful_keyword_match(record["matched_keywords"].split(";"), record["raw_json"]):
            continue
        seen.add(record["id"])
        records.append(record)

    return sorted(records, key=record_sort_key, reverse=True)[: max(1, max_records)]


def fetch_public_rows(config: PublicPurchaseConfig) -> list[PublicPurchaseRow]:
    result = fetch_url(
        config.public_info_url,
        headers={"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"},
        timeout=60,
        byte_limit=1_000_000,
        user_agent=USER_AGENT,
    )
    result.raise_for_status()
    return parse_public_info_rows(result.body_text(), base_url=config.public_info_url)


def parse_public_info_rows(page_html: str, *, base_url: str) -> list[PublicPurchaseRow]:
    hidden_ids = hidden_span_ids(page_html)
    rows: list[PublicPurchaseRow] = []

    for match in re.finditer(r'tooltip\s*=\s*(?P<body>.*?)\$\("tr_(?P<tr>\d+)"\)\.update\((?P<update>.*?)\);', page_html, re.DOTALL):
        body_html = concat_js_strings(match.group("body"))
        link_match = re.search(r'(?is)<a\b[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', body_html)
        if not link_match:
            continue
        detail_url = urllib.parse.urljoin(base_url, link_match.group(1))
        title = visible_text(link_match.group(2), hidden_ids, limit=500)
        bid_id = first_match(body_html, r"bidView\?bidId=(\d+)")
        source_record_id = source_record_id_from_title(title) or bid_id
        description_match = re.search(r'(?is)<div\b[^>]*class=["\']balloonstyle["\'][^>]*>(.*?)</div>', body_html)
        description = visible_text(description_match.group(1), hidden_ids, limit=4000) if description_match else ""

        update_html = concat_js_strings(match.group("update"))
        cells = [visible_text(cell, hidden_ids, limit=500) for cell in re.findall(r"(?is)<td\b[^>]*>(.*?)</td>", update_html)]
        date_cells = cells[1:] if cells and not cells[0] else cells
        rows.append(
            PublicPurchaseRow(
                bid_id=bid_id,
                source_record_id=source_record_id,
                title=title,
                description=description,
                detail_url=detail_url,
                start_date=date_cells[0] if len(date_cells) > 0 else "",
                end_date=date_cells[1] if len(date_cells) > 1 else "",
                time_left=date_cells[2] if len(date_cells) > 2 else "",
                addendums=date_cells[3] if len(date_cells) > 3 else "",
            )
        )
    return [row for row in rows if row.title and row.source_record_id]


def normalize_row(row: PublicPurchaseRow, *, config: PublicPurchaseConfig, keywords: list[str]) -> dict[str, str]:
    posted_date = public_purchase_date(row.start_date)
    due_date = public_purchase_date(row.end_date)
    status = status_from_due(due_date, row.time_left)
    search_text = expand_related_terms(" ".join([row.source_record_id, row.title, row.description, config.agency, row.addendums]))
    matched = keyword_hits(search_text, keywords)
    raw = {
        "source_key": config.source_key,
        "source_note": config.source_note,
        "official_source_url": config.official_source_url,
        "bid_view_url_login_required": row.detail_url,
        "public_listing": {
            "bid_id": row.bid_id,
            "source_record_id": row.source_record_id,
            "title": row.title,
            "description": row.description,
            "start_date_raw": row.start_date,
            "end_date_raw": row.end_date,
            "time_left": row.time_left,
            "addendums": row.addendums,
        },
    }

    return {
        "id": stable_id(config.state, row.bid_id or row.source_record_id, prefix=f"{config.state.lower()}-publicpurchase-bid"),
        "state": config.state,
        "source": config.source_name,
        "source_record_id": row.source_record_id,
        "title": row.title,
        "agency": config.agency,
        "document_type": document_type(row.title),
        "posted_date": posted_date,
        "due_date": due_date,
        "status": status,
        "amount": "",
        "document_url": config.public_info_url,
        "source_url": config.public_info_url,
        "matched_keywords": ";".join(matched),
        "relevance_score": str(relevance_score(matched, status, search_text, due_date)),
        "raw_json": compact_raw_json(raw, limit=10000),
        "last_checked_at": now_iso(),
    }


def hidden_span_ids(page_html: str) -> set[str]:
    ids: set[str] = set()
    for args in re.findall(r"action\((.*?)\);", page_html, re.DOTALL):
        ids.update(extract_js_strings(args))
    return ids


def concat_js_strings(expression: str) -> str:
    return "".join(extract_js_strings(expression))


def extract_js_strings(expression: str) -> list[str]:
    values: list[str] = []
    for match in re.finditer(r"'((?:\\.|[^'\\])*)'|\"((?:\\.|[^\"\\])*)\"", expression, re.DOTALL):
        raw = match.group(1) if match.group(1) is not None else match.group(2)
        values.append(js_unescape(raw))
    return values


def js_unescape(value: str) -> str:
    return (
        value.replace(r"\'", "'")
        .replace(r'\"', '"')
        .replace(r"\/", "/")
        .replace(r"\n", "\n")
        .replace(r"\t", "\t")
    )


class VisibleTextParser(HTMLParser):
    def __init__(self, hidden_ids: set[str]) -> None:
        super().__init__()
        self.hidden_ids = hidden_ids
        self.skip_depth = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.skip_depth:
            self.skip_depth += 1
            return
        data = {key: value or "" for key, value in attrs}
        if data.get("id") in self.hidden_ids:
            self.skip_depth = 1
            return
        if tag == "br":
            self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        if not self.skip_depth:
            self.parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self.skip_depth:
            self.skip_depth -= 1


def visible_text(fragment: str, hidden_ids: set[str], *, limit: int) -> str:
    parser = VisibleTextParser(hidden_ids)
    parser.feed(html.unescape(fragment))
    return clean_text("".join(parser.parts), limit)


def source_record_id_from_title(title: str) -> str:
    match = re.search(r"\b(?:Bid|RFP|RFQ|IFB|ITB)\s*#\s*([A-Za-z0-9_.-]+)", title, re.IGNORECASE)
    return clean_text(match.group(1), 120) if match else ""


def public_purchase_date(value: str) -> str:
    text = clean_text(value, 120)
    if not text or text.lower() == "no addendums":
        return ""
    normalized = re.sub(r"\s+(?:MDT|MST|CDT|CST|EDT|EST|PDT|PST)\b", "", text, flags=re.IGNORECASE).strip()
    for fmt in ("%b %d, %Y %I:%M:%S %p", "%b %d, %Y %I:%M %p", "%B %d, %Y %I:%M:%S %p", "%B %d, %Y %I:%M %p"):
        try:
            return dt.datetime.strptime(normalized, fmt).date().isoformat()
        except ValueError:
            continue
    return iso_date(text)


def status_from_due(due_date: str, time_left: str) -> str:
    if time_left and not any(term in time_left.lower() for term in ["expired", "closed"]):
        return "Open"
    due = parse_date(due_date)
    if due and due >= dt.date.today():
        return "Open"
    return "Closed"


def document_type(title: str) -> str:
    text = title.upper()
    if "REQUEST FOR INFORMATION" in text or code_matches(text, "RFI"):
        return "Public Purchase Request for Information"
    if "RFP #" in text or "REQUEST FOR PROPOS" in text or code_matches(text, "RFP"):
        return "Public Purchase Request for Proposal"
    if "REQUEST FOR QUAL" in text or code_matches(text, "RFQ"):
        return "Public Purchase Request for Qualifications"
    if "REQUEST FOR QUOTE" in text:
        return "Public Purchase Request for Quote"
    if "INVITATION TO BID" in text or code_matches(text, "IFB") or code_matches(text, "ITB"):
        return "Public Purchase Invitation to Bid"
    return "Public Purchase Bid Solicitation"


def expand_related_terms(text: str) -> str:
    expanded = text
    if any(term_matches(text, term) for term in ["Department of Health", "Health Department", "Medicaid"]):
        expanded += " Medicaid Medicare managed care eligibility claims provider data"
    if any(term_matches(text, term) for term in ["Department of Family Services", "Human Services"]):
        expanded += " eligibility enrollment human services"
    if any(term_matches(text, term) for term in ["Behavioral Health", "Mental Health"]):
        expanded += " behavioral health managed care"
    if term_matches(text, "RHTP"):
        expanded += " rural health rural health transformation"
    return expanded


def useful_keyword_match(matches: list[str], text: str) -> bool:
    ambiguous_terms = {"claims", "eligibility", "enrollment", "cms", "workforce"}
    matched_terms = {match.lower() for match in matches if match}
    if not matched_terms or not matched_terms <= ambiguous_terms:
        return True
    context_terms = [
        "department of health",
        "department of family services",
        "human services",
        "healthcare",
        "health care",
        "medicaid",
        "medicare",
        "medical",
        "behavioral",
        "managed care",
        "provider",
        "chip",
        "mmis",
    ]
    return any(term_matches(text, term) for term in context_terms)


def false_keyword_hit(record: dict[str, str]) -> bool:
    text = " ".join([record.get("title", ""), record.get("agency", ""), record.get("raw_json", "")])
    return term_matches(text, "commissary") and not term_matches(text, "MMIS")


def relevance_score(matches: list[str], status: str, text: str, due_date: str) -> int:
    score = min(50, len([match for match in matches if match]) * 10)
    if any(term_matches(text, term) for term in ["Medicaid", "MMIS", "Department of Health", "Human Services"]):
        score += 30
    if any(term_matches(text, term) for term in ["eligibility", "claims", "enrollment", "managed care", "provider data"]):
        score += 18
    if any(term_matches(text, term) for term in ["interoperability", "FHIR", "prior authorization", "telehealth", "quality measures"]):
        score += 15
    if any(term_matches(text, term) for term in ["rural health", "rural health transformation", "critical access hospital"]):
        score += 25
    if any(term_matches(text, term) for term in ["RFP", "RFI", "RFQ", "software", "data", "analytics", "contact center"]):
        score += 10
    if status.lower() == "open":
        score += 10
    due = parse_date(due_date)
    if due and due >= dt.date.today():
        score += 5
    return min(score, 100)


def is_open_or_recent(posted_date: str, due_date: str, status: str, days_back: int) -> bool:
    if status.lower() == "open":
        return True
    due = parse_date(due_date)
    if due and due >= dt.date.today():
        return True
    if days_back <= 0:
        return True
    posted = parse_date(posted_date)
    return not posted or (dt.date.today() - posted).days <= days_back


def first_match(text: str, pattern: str) -> str:
    match = re.search(pattern, text, re.IGNORECASE)
    return clean_text(match.group(1), 160) if match else ""


def code_matches(text: str, code: str) -> bool:
    return re.search(rf"(?<![A-Z0-9]){re.escape(code)}(?![A-Z0-9])", text, re.IGNORECASE) is not None


def record_sort_key(row: dict[str, str]) -> tuple[int, str, str]:
    return (int_or_zero(row.get("relevance_score")), row.get("due_date", ""), row.get("posted_date", ""))


def int_or_zero(value: Any) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def emit(progress: Callable[[str], None] | None, message: str) -> None:
    if progress:
        progress(message)
