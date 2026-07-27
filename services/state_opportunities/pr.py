from __future__ import annotations

import datetime as dt
import re
import unicodedata
import urllib.parse
from typing import Any, Callable

from services.state_http import fetch_url
from services.state_normalization import clean_text, compact_raw_json, iso_date, keyword_hits, parse_date, stable_id, term_matches

ASG_URL = "https://www.asg.pr.gov/"
BASE_URL = "https://subastas.pr.gov/"
RUS_URL = urllib.parse.urljoin(BASE_URL, "Pages/subastas.aspx")
USER_AGENT = "Mozilla/5.0 soe-group3-pr-rus-opportunities/0.1"
SOURCE_NAME = "Puerto Rico RUS Subastas"
SOURCE_NOTE = (
    "Official ASG Registro Unico de Subasta (RUS) SharePoint public listing; "
    "rows are rendered in Pages/subastas.aspx as HTML list items."
)
MAX_LISTING_BYTES = 5_000_000
MAX_SCAN_ROWS = 5000
CURRENT_STATUSES = {"abierta", "pospuesta"}
TAG_RE = re.compile(r"(?is)<[^>]+>")
LIST_ITEM_RE = re.compile(r'(?is)<li\b[^>]*class=["\'][^"\']*\blistitem\b[^"\']*["\'][^>]*>.*?</li>')
INPUT_RE = re.compile(r"(?is)<input\b[^>]*>")
ATTR_RE = re.compile(r"\b([A-Za-z_:][-A-Za-z0-9_:.]*)\s*=\s*([\"'])(.*?)\2", re.DOTALL)
SPANISH_MONTHS = {
    "enero": 1,
    "febrero": 2,
    "marzo": 3,
    "abril": 4,
    "mayo": 5,
    "junio": 6,
    "julio": 7,
    "agosto": 8,
    "septiembre": 9,
    "setiembre": 9,
    "octubre": 10,
    "noviembre": 11,
    "diciembre": 12,
}


def fetch_opportunities(
    *,
    keywords: list[str],
    days_back: int,
    max_records: int,
    progress: Callable[[str], None] | None = None,
) -> list[dict[str, str]]:
    html, final_url, truncated = fetch_listing_html()
    rows = parse_listing_rows(html)
    current_rows = [row for row in rows if is_current_status(row.get("status", ""))]
    current_rows = current_rows[: min(MAX_SCAN_ROWS, max(len(current_rows), max(1, max_records) * 20))]
    emit(
        progress,
        f"PR RUS subastas: {len(current_rows)} current rows from {len(rows)} public rows"
        + (" (listing truncated)" if truncated else ""),
    )

    records: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in current_rows:
        record = normalize_rus_row(row, final_url=final_url, keywords=keywords)
        if not record.get("source_record_id") or record["id"] in seen:
            continue
        if not is_open_or_recent(record["posted_date"], record["due_date"], days_back):
            continue
        if keywords and not record["matched_keywords"]:
            continue
        if not useful_keyword_match(record["matched_keywords"].split(";"), record["raw_json"]):
            continue
        seen.add(record["id"])
        records.append(record)

    return sorted(records, key=record_sort_key, reverse=True)[: max(1, max_records)]


def fetch_listing_html() -> tuple[str, str, bool]:
    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": ASG_URL,
    }
    result = fetch_url(RUS_URL, headers=headers, timeout=60, byte_limit=MAX_LISTING_BYTES, user_agent=USER_AGENT)
    result.raise_for_status()
    return result.body_text(), result.final_url, result.truncated


def parse_listing_rows(html: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for block in LIST_ITEM_RE.findall(html):
        row = parse_list_item(block)
        if row.get("source_record_id") or row.get("item_id"):
            rows.append(row)
    return rows


def parse_list_item(block: str) -> dict[str, str]:
    detail_href = first_attr(block, r'<a\b[^>]*class=["\'][^"\']*\btitleLink\b[^"\']*["\'][^>]*>', "href")
    detail_url = urllib.parse.urljoin(RUS_URL, detail_href) if detail_href else ""
    item_id = query_value(detail_url, "itemID") or first_match(block, r'<li\b[^>]*id=["\'][^"\']+_(\d+)["\']')
    number = strip_html(first_match(block, r'<span\b[^>]*class=["\']number["\'][^>]*>\s*\((.*?)\)\s*</span>'), 120)
    title = strip_html(first_match(block, r'<span\b[^>]*class=["\']title["\'][^>]*>(.*?)</span>'), 500)
    agency = labeled_div(block, "agency", "Agencia")
    status = input_value(block, "status") or labeled_div(block, "agency", "Estatus")
    row = {
        "item_id": item_id,
        "detail_url": detail_url,
        "source_record_id": number or item_id,
        "title": title or number or item_id,
        "agency": agency,
        "status": status,
        "classification": input_value(block, "clasification"),
        "keyword": input_value(block, "keyword"),
        "localization": labeled_div(block, "localization", "Localizacion"),
        "fecha_apertura": labeled_div(block, "fechaApertura", "Fecha apertura"),
        "fecha_pre_subasta": labeled_div(block, "fechaPreSubasta", "Fecha pre-subasta"),
        "fecha_pliegos": labeled_div(block, "fechaPliegos", "Fecha pliegos"),
        "publish_date": input_value(block, "publishDate"),
        "row_text": strip_html(block, 3000),
    }
    return {key: clean_text(value, 3000) for key, value in row.items()}


def normalize_rus_row(row: dict[str, str], *, final_url: str, keywords: list[str]) -> dict[str, str]:
    source_record_id = clean_text(row.get("source_record_id"), 160)
    title = clean_text(row.get("title") or source_record_id, 500)
    agency = clean_text(row.get("agency"), 180)
    status = clean_text(row.get("status") or "Abierta", 80)
    posted_date = iso_date(row.get("publish_date"))
    due_date = spanish_date_to_iso(row.get("fecha_apertura"))
    detail_url = clean_text(row.get("detail_url"), 500) or RUS_URL
    search_text = expand_related_terms(
        " ".join(
            [
                source_record_id,
                title,
                agency,
                row.get("classification", ""),
                row.get("localization", ""),
                row.get("status", ""),
                row.get("row_text", ""),
            ]
        )
    )
    matched = keyword_hits(search_text, keywords)
    raw = {
        "source_key": "pr_asg_procurement",
        "source_note": SOURCE_NOTE,
        "item_id": row.get("item_id", ""),
        "detail_url": detail_url,
        "classification": row.get("classification", ""),
        "keyword": row.get("keyword", ""),
        "localization": row.get("localization", ""),
        "fecha_apertura_raw": row.get("fecha_apertura", ""),
        "fecha_pre_subasta_raw": row.get("fecha_pre_subasta", ""),
        "fecha_pliegos_raw": row.get("fecha_pliegos", ""),
        "publish_date_raw": row.get("publish_date", ""),
    }

    return {
        "id": stable_id("PR", source_record_id, prefix="pr-rus-subasta"),
        "state": "PR",
        "source": SOURCE_NAME,
        "source_record_id": source_record_id,
        "title": title,
        "agency": agency,
        "document_type": document_type(source_record_id, title, row.get("classification", "")),
        "posted_date": posted_date,
        "due_date": due_date,
        "status": status,
        "amount": "",
        "document_url": detail_url,
        "source_url": final_url or RUS_URL,
        "matched_keywords": ";".join(matched),
        "relevance_score": str(relevance_score(matched, status, search_text, due_date)),
        "raw_json": compact_raw_json(raw),
        "last_checked_at": now_iso(),
    }


def labeled_div(block: str, css_class: str, label: str) -> str:
    folded_label = fold_text(label).rstrip(":")
    for match in re.finditer(rf'(?is)<div\b[^>]*class=["\'][^"\']*\b{re.escape(css_class)}\b[^"\']*["\'][^>]*>(.*?)</div>', block):
        text = strip_html(match.group(1), 1000)
        folded = fold_text(text)
        prefix = folded_label + ":"
        if folded.startswith(prefix):
            return clean_text(text[len(prefix) :], 1000)
        if folded_label in folded:
            return clean_text(re.sub(rf"(?is)^\s*{re.escape(label)}\s*:\s*", "", text), 1000)
    return ""


def input_value(block: str, css_class: str) -> str:
    for match in INPUT_RE.finditer(block):
        attrs = attrs_dict(match.group(0))
        classes = set((attrs.get("class") or "").split())
        if css_class in classes:
            return clean_text(attrs.get("value"), 1000)
    return ""


def attrs_dict(tag: str) -> dict[str, str]:
    return {name.lower(): clean_text(value) for name, _quote, value in ATTR_RE.findall(tag)}


def first_attr(text: str, tag_pattern: str, attr_name: str) -> str:
    match = re.search(tag_pattern, text, re.IGNORECASE | re.DOTALL)
    if not match:
        return ""
    return attrs_dict(match.group(0)).get(attr_name.lower(), "")


def first_match(text: str, pattern: str) -> str:
    match = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
    return match.group(1) if match else ""


def strip_html(value: Any, limit: int = 1000) -> str:
    return clean_text(TAG_RE.sub(" ", str(value or "")), limit)


def query_value(url: str, key: str) -> str:
    if not url:
        return ""
    parsed = urllib.parse.urlsplit(url)
    values = urllib.parse.parse_qs(parsed.query).get(key)
    return clean_text(values[0], 80) if values else ""


def spanish_date_to_iso(value: Any) -> str:
    folded = fold_text(value)
    match = re.search(r"\b(\d{1,2})\s+de\s+([a-z]+)\s+de\s+(\d{4})\b", folded)
    if match:
        day = int(match.group(1))
        month = SPANISH_MONTHS.get(match.group(2))
        year = int(match.group(3))
        if month:
            try:
                return dt.date(year, month, day).isoformat()
            except ValueError:
                return ""
    return iso_date(value)


def fold_text(value: Any) -> str:
    text = clean_text(value)
    normalized = unicodedata.normalize("NFKD", text)
    return normalized.encode("ascii", "ignore").decode("ascii").lower()


def is_current_status(status: str) -> bool:
    return fold_text(status) in CURRENT_STATUSES


def document_type(source_record_id: str, title: str, classification: str) -> str:
    text = fold_text(" ".join([source_record_id, title, classification]))
    if re.search(r"\brfi\b", text) or "request for information" in text:
        return "RUS Request for Information"
    if re.search(r"\brfp\b", text) or "request for proposal" in text:
        return "RUS Request for Proposals"
    if re.search(r"\brfq\b", text) or "request for qualification" in text or "request for quote" in text:
        return "RUS Request for Qualifications"
    if "subasta informal" in text:
        return "RUS Informal Bid"
    if "subasta formal" in text or "formal" in text:
        return "RUS Formal Bid"
    return "RUS Subasta"


def expand_related_terms(text: str) -> str:
    folded = fold_text(text)
    expanded = text
    if folded_has_any(folded, ["departamento de salud", "centro cardiovascular", "hospital", "salud"]):
        expanded += " health healthcare quality"
    if folded_has_any(folded, ["administracion de seguros de salud", "ases", "plan vital", "mi salud"]):
        expanded += " Medicaid managed care eligibility enrollment claims"
    if folded_has_any(folded, ["servicios de salud mental", "salud mental", "conductual"]):
        expanded += " behavioral health"
    if folded_has_any(folded, ["tecnologia", "software", "datos", "informacion", "plataforma", "sistema informatico", "sistemas de informacion"]):
        expanded += " software data platform interoperability provider data"
    if folded_has_any(folded, ["telemedicina", "telesalud"]):
        expanded += " telehealth"
    return expanded


def folded_has_any(folded_text: str, terms: list[str]) -> bool:
    for term in terms:
        parts = [re.escape(part) for part in re.split(r"\s+", fold_text(term)) if part]
        if not parts:
            continue
        pattern = r"(?<![a-z0-9])" + r"\s+".join(parts) + r"(?![a-z0-9])"
        if re.search(pattern, folded_text):
            return True
    return False


def useful_keyword_match(matches: list[str], text: str) -> bool:
    generic_terms = {"claims", "eligibility", "enrollment", "quality", "workforce"}
    matched_terms = {match.lower() for match in matches if match}
    if not matched_terms or not matched_terms <= generic_terms:
        return True
    folded = fold_text(text)
    context_terms = [
        "departamento de salud",
        "administracion de seguros de salud",
        "ases",
        "plan vital",
        "mi salud",
        "centro cardiovascular",
        "hospital",
        "salud",
        "medicaid",
        "medicare",
        "health",
        "healthcare",
        "managed care",
        "provider",
        "mmis",
    ]
    return any(term_matches(text, term) for term in context_terms) or folded_has_any(folded, context_terms)


def relevance_score(matches: list[str], status: str, text: str, due_date: str) -> int:
    score = min(50, len([match for match in matches if match]) * 10)
    folded = fold_text(text)
    if folded_has_any(folded, ["medicaid", "ases", "plan vital", "mi salud", "administracion de seguros de salud"]):
        score += 30
    if folded_has_any(folded, ["departamento de salud", "centro cardiovascular", "hospital", "salud"]):
        score += 22
    if any(term_matches(text, term) for term in ["managed care", "eligibility", "claims", "provider data", "behavioral health"]):
        score += 18
    if any(term_matches(text, term) for term in ["interoperability", "FHIR", "prior authorization", "telehealth", "quality measures"]):
        score += 15
    if folded_has_any(folded, ["rfp", "rfq", "rfi", "software", "data", "tecnologia", "servicios profesionales"]):
        score += 10
    if is_current_status(status):
        score += 10
    due = parse_date(due_date)
    if due and due >= dt.date.today():
        score += 5
    return min(score, 100)


def is_open_or_recent(posted_date: str, due_date: str, days_back: int) -> bool:
    due = parse_date(due_date)
    if due and due >= dt.date.today():
        return True
    if days_back <= 0:
        return True
    posted = parse_date(posted_date)
    return not posted or (dt.date.today() - posted).days <= days_back


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
