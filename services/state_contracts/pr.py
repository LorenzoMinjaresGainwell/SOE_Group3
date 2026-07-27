from __future__ import annotations

import datetime as dt
import re
import time
import unicodedata
import urllib.parse
from typing import Any, Callable

from services.state_http import fetch_url
from services.state_normalization import clean_text, compact_raw_json, keyword_hits, months_until, stable_id, term_matches

ASG_CONTRACTS_URL = "https://www.asg.pr.gov/contratos"
SOURCE_NAME = "Puerto Rico ASG Contratos"
SOURCE_NOTE = (
    "Official ASG public contracts catalog at www.asg.pr.gov/contratos. Category pages expose "
    "contract document cards with contract number, description, start/end dates, file name, and document URL."
)
USER_AGENT = "Mozilla/5.0 soe-group3-pr-asg-contracts/0.1"
MAX_CATEGORY_PAGES = 120
CATEGORY_RE = re.compile(
    r'(?is)<h2\b[^>]*class=["\'][^"\']*\bcard-title\b[^"\']*["\'][^>]*>(.*?)</h2>.*?'
    r'<a\b[^>]*href=["\']([^"\']*/contratos[0-9a-f-]+)["\']'
)
ARTICLE_RE = re.compile(r'(?is)<article\b[^>]*class=["\'][^"\']*\bcontract-doc-card\b[^"\']*["\'][^>]*>(.*?)</article>')
TAG_RE = re.compile(r"(?is)<[^>]+>")
ATTR_RE = re.compile(r"\b([A-Za-z_:][-A-Za-z0-9_:.]*)\s*=\s*([\"'])(.*?)\2", re.DOTALL)
NON_VENDOR_TERMS = {
    "addendum",
    "adquisicion",
    "acuerdo intergubernamental",
    "abril",
    "agosto",
    "cambio de precio",
    "diciembre",
    "enero",
    "enmienda",
    "febrero",
    "instrucciones",
    "julio",
    "junio",
    "lista",
    "marzo",
    "mayo",
    "noviembre",
    "octubre",
    "portada",
    "precio",
    "precios",
    "programa",
    "rfp",
    "seleccion multiple",
    "septiembre",
    "subasta",
    "tabla",
    "uso",
}


def fetch_contracts(
    *,
    vendor_terms: list[str],
    keywords: list[str],
    max_per_vendor: int,
    progress: Callable[[str], None] | None = None,
) -> list[dict[str, str]]:
    categories = fetch_category_links()
    emit(progress, f"PR ASG contracts: {len(categories)} public category pages")

    docs: list[dict[str, str]] = []
    for category in categories[:MAX_CATEGORY_PAGES]:
        category_docs = fetch_category_docs(category)
        docs.extend(category_docs)
        emit(progress, f"PR ASG contracts: {category['title']}: {len(category_docs)} document cards")
        time.sleep(0.05)

    records: list[dict[str, str]] = []
    seen: set[str] = set()
    query_counts: dict[str, int] = {}
    queries = unique_terms(vendor_terms)
    query_limit = max(1, max_per_vendor)

    for doc in docs:
        record = normalize_contract_doc(doc, keywords=keywords)
        if not record:
            continue
        search_text = doc_search_text(doc, record.get("vendor_name", ""))
        query = matching_query(search_text, queries)
        if not query:
            matched_keywords = [item for item in record.get("matched_keywords", "").split(";") if item]
            query = matched_keywords[0] if matched_keywords else ""
        if not query:
            continue
        if query_counts.get(query, 0) >= query_limit:
            continue
        if record["id"] in seen:
            continue
        record["vendor_query"] = query
        seen.add(record["id"])
        query_counts[query] = query_counts.get(query, 0) + 1
        records.append(record)

    emit(progress, f"PR ASG contracts: scanned {len(docs)} document cards, normalized {len(records)} records")
    return sorted(records, key=contract_sort_key, reverse=True)


def fetch_category_links() -> list[dict[str, str]]:
    result = fetch_html(ASG_CONTRACTS_URL, referer="https://www.asg.pr.gov/")
    links: list[dict[str, str]] = []
    seen: set[str] = set()
    for title_html, href in CATEGORY_RE.findall(result):
        title = strip_html(title_html, 300)
        url = urllib.parse.urljoin(ASG_CONTRACTS_URL, href)
        if not title or url in seen:
            continue
        seen.add(url)
        links.append({"title": title, "url": url})
    if not links:
        raise RuntimeError("PR ASG contracts category list returned no public category links")
    return links


def fetch_category_docs(category: dict[str, str]) -> list[dict[str, str]]:
    html = fetch_html(category["url"], referer=ASG_CONTRACTS_URL)
    docs: list[dict[str, str]] = []
    for block in ARTICLE_RE.findall(html):
        doc = parse_doc_card(block)
        if not doc.get("doc_number"):
            continue
        doc["category_title"] = category["title"]
        doc["category_url"] = category["url"]
        docs.append(doc)
    return docs


def fetch_html(url: str, *, referer: str) -> str:
    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "es-PR,es;q=0.9,en;q=0.8",
        "Referer": referer,
    }
    result = fetch_url(url, headers=headers, timeout=60, byte_limit=1_500_000, user_agent=USER_AGENT)
    result.raise_for_status()
    return result.body_text()


def parse_doc_card(block: str) -> dict[str, str]:
    doc_number = strip_html(first_match(block, r'(?is)<h3\b[^>]*class=["\'][^"\']*\bcontract-doc-number\b[^"\']*["\'][^>]*>(.*?)</h3>'), 220)
    description = strip_html(first_match(block, r'(?is)<p\b[^>]*class=["\'][^"\']*\bcontract-doc-description\b[^"\']*["\'][^>]*>(.*?)</p>'), 1000)
    file_name = strip_html(first_match(block, r'(?is)<p\b[^>]*class=["\'][^"\']*\bcontract-doc-file\b[^"\']*["\'][^>]*>.*?<strong>\s*Archivo:\s*</strong>(.*?)</p>'), 300)
    doc_url = first_attr(block, r'(?is)<a\b[^>]*class=["\'][^"\']*\bcontract-doc-open\b[^"\']*["\'][^>]*>', "href")
    text = strip_html(block, 2500)
    start_raw = first_match(text, r"Vigencia:\s*(\d{1,2}/\d{1,2}/\d{4})")
    end_raw = first_match(text, r"Expira:\s*(\d{1,2}/\d{1,2}/\d{4})")
    return {
        "doc_number": doc_number,
        "description": description,
        "file_name": file_name,
        "document_url": clean_text(doc_url, 600),
        "start_date_raw": clean_text(start_raw, 40),
        "end_date_raw": clean_text(end_raw, 40),
        "card_text": text,
    }


def normalize_contract_doc(doc: dict[str, str], *, keywords: list[str]) -> dict[str, str]:
    contract_number = contract_number_for(doc.get("doc_number", ""))
    start_date = pr_date_to_iso(doc.get("start_date_raw", ""))
    end_date = pr_date_to_iso(doc.get("end_date_raw", ""))
    vendor_name = extract_vendor_name(doc)
    if not contract_number or not end_date or not vendor_name:
        return {}

    title = clean_text(f"{doc.get('category_title', '')}: {doc.get('description', '')}", 500)
    search_text = expand_related_terms(doc_search_text(doc, vendor_name))
    matched = keyword_hits(search_text, keywords)
    months = months_until(end_date)
    record_type = contract_record_type(doc)
    source_record_id = clean_text(
        "|".join([doc.get("category_url", ""), doc.get("doc_number", ""), doc.get("file_name", "")]),
        700,
    )
    raw = {
        "source_key": "pr_asg_procurement",
        "source_note": SOURCE_NOTE,
        "category_title": doc.get("category_title", ""),
        "category_url": doc.get("category_url", ""),
        "doc_number": doc.get("doc_number", ""),
        "description": doc.get("description", ""),
        "file_name": doc.get("file_name", ""),
        "start_date_raw": doc.get("start_date_raw", ""),
        "end_date_raw": doc.get("end_date_raw", ""),
        "vendor_name_source": vendor_name,
    }

    return {
        "id": stable_id("PR", source_record_id, prefix="pr-asg-contract"),
        "state": "PR",
        "source": SOURCE_NAME,
        "source_record_id": source_record_id,
        "parent_id": contract_number,
        "contract_record_type": record_type,
        "vendor_name": vendor_name,
        "vendor_query": "",
        "agency": "Puerto Rico Administracion de Servicios Generales",
        "contract_number": contract_number,
        "title": title,
        "amount": "",
        "execution_date": "",
        "start_date": start_date,
        "end_date": end_date,
        "months_to_end": "" if months is None else str(months),
        "recompete_signal": recompete_signal(months),
        "document_type": document_type(doc, record_type),
        "document_url": doc.get("document_url") or doc.get("category_url") or ASG_CONTRACTS_URL,
        "source_url": doc.get("category_url") or ASG_CONTRACTS_URL,
        "matched_keywords": ";".join(matched),
        "relevance_score": str(relevance_score(matched, months, record_type, search_text)),
        "raw_json": compact_raw_json(raw, limit=8000),
        "last_checked_at": now_iso(),
    }


def contract_number_for(value: str) -> str:
    text = clean_text(value, 220)
    if not text:
        return ""
    return clean_text(text.split(",", 1)[0], 120)


def extract_vendor_name(doc: dict[str, str]) -> str:
    doc_number = clean_text(doc.get("doc_number", ""), 220)
    suffix = clean_text(doc_number.split(",", 1)[1] if "," in doc_number else "", 180)
    paren_vendor = vendor_from_parenthetical(suffix)
    if paren_vendor:
        return paren_vendor
    if suffix and not looks_non_vendor(suffix):
        return clean_vendor(suffix)

    description = doc.get("description", "")
    for pattern in (
        r"\((?:Contrato|Contrato de)\s+([^()]+)\)",
        r"\((?:Tabla\s+Ofertar|Tabla de Oferta):\s+([^()]+)\)",
    ):
        match = re.search(pattern, description, re.IGNORECASE)
        if match and not looks_non_vendor(match.group(1)):
            return clean_vendor(match.group(1))

    file_vendor = vendor_from_file_name(doc.get("file_name", ""))
    if file_vendor:
        return file_vendor
    return ""


def vendor_from_parenthetical(value: str) -> str:
    matches = re.findall(r"\(([^()]+)\)", value)
    for candidate in reversed(matches):
        if not looks_non_vendor(candidate):
            return clean_vendor(candidate)
    return ""


def vendor_from_file_name(file_name: str) -> str:
    name = clean_text(file_name, 300)
    if "." in name:
        name = name.rsplit(".", 1)[0]
    candidate = ""
    if "_-_" in name:
        candidate = name.split("_-_", 1)[1]
    else:
        match = re.search(r"(?i)ENMIENDA[_\s-]*\d+[_\s-]+(.+)$", name)
        if match:
            candidate = match.group(1)
    candidate = re.sub(r"(?i)[_\s-]+ENMIENDA[_\s-]*\d+.*$", "", candidate)
    candidate = re.sub(r"(?i)[_\s-]+(PORTADA|INSTRUCCIONES|TABLA|ADDENDUM).*$", "", candidate)
    if not candidate or looks_non_vendor(candidate):
        return ""
    return clean_vendor(candidate)


def clean_vendor(value: str) -> str:
    text = clean_text(value.replace("_", " ").replace("--", "-"), 180)
    text = re.sub(r"\s+", " ", text).strip(" -.,;:")
    return text


def looks_non_vendor(value: str) -> bool:
    folded = fold_text(value)
    if not folded or len(folded) < 3:
        return True
    if not re.search(r"[a-z]", folded):
        return True
    if any(term in folded for term in NON_VENDOR_TERMS):
        return True
    return False


def contract_record_type(doc: dict[str, str]) -> str:
    text = fold_text(" ".join([doc.get("doc_number", ""), doc.get("description", ""), doc.get("file_name", "")]))
    if "enmienda" in text:
        return "amendment"
    if "tabla ofertar" in text or "tabla de oferta" in text:
        return "dealer_line"
    if "acuerdo intergubernamental" in text or "seleccion multiple" in text:
        return "master_agreement"
    return "parent_contract"


def document_type(doc: dict[str, str], record_type: str) -> str:
    if record_type == "amendment":
        return "ASG Contract Amendment"
    text = fold_text(" ".join([doc.get("doc_number", ""), doc.get("description", ""), doc.get("file_name", "")]))
    if "acuerdo intergubernamental" in text:
        return "ASG Intergovernmental Agreement"
    if "tabla" in text:
        return "ASG Contract Price Table"
    return "ASG Contract Document"


def doc_search_text(doc: dict[str, str], vendor_name: str) -> str:
    return " ".join(
        clean_text(part, 2000)
        for part in [
            vendor_name,
            doc.get("category_title", ""),
            doc.get("doc_number", ""),
            doc.get("description", ""),
            doc.get("file_name", ""),
            doc.get("card_text", ""),
        ]
        if part
    )


def expand_related_terms(text: str) -> str:
    folded = fold_text(text)
    expanded = text
    if folded_has_any(folded, ["administracion de seguros de salud", "ases", "plan vital", "mi salud"]):
        expanded += " Medicaid managed care eligibility enrollment claims"
    if folded_has_any(folded, ["salud mental", "conductual"]):
        expanded += " behavioral health"
    if folded_has_any(folded, ["telemedicina", "telesalud"]):
        expanded += " telehealth"
    if folded_has_any(folded, ["interoperabilidad", "fhir", "autorizacion previa", "datos de proveedor"]):
        expanded += " interoperability FHIR prior authorization provider data"
    return expanded


def matching_query(text: str, terms: list[str]) -> str:
    for term in terms:
        if term_matches(text, term):
            return term
    return ""


def unique_terms(terms: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for term in terms:
        cleaned = clean_text(term, 100)
        key = cleaned.lower()
        if cleaned and key not in seen:
            seen.add(key)
            result.append(cleaned)
    return result


def relevance_score(matches: list[str], months_to_end: int | None, record_type: str, text: str) -> int:
    score = min(45, len(matches) * 8)
    folded = fold_text(text)
    if folded_has_any(folded, ["medicaid", "ases", "plan vital", "administracion de seguros de salud"]):
        score += 28
    if folded_has_any(folded, ["salud mental", "conductual", "behavioral health"]):
        score += 16
    if folded_has_any(folded, ["interoperabilidad", "fhir", "prior authorization", "provider data"]):
        score += 12
    if months_to_end is not None:
        if 0 <= months_to_end <= 18:
            score += 25
        elif 18 < months_to_end <= 36:
            score += 18
        elif months_to_end > 36:
            score += 6
    if record_type == "parent_contract":
        score += 12
    elif record_type == "master_agreement":
        score += 16
    elif record_type == "amendment":
        score -= 8
    return max(0, min(score, 100))


def recompete_signal(months_to_end: int | None) -> str:
    if months_to_end is None:
        return "Unknown end date"
    if months_to_end < 0:
        return "Expired/past award"
    if months_to_end <= 18:
        return "Expiring soon"
    if months_to_end <= 36:
        return "Recompete watch"
    return "Longer-term contract"


def contract_sort_key(row: dict[str, str]) -> tuple[int, int, str, str]:
    return (
        int_or_zero(row.get("relevance_score")),
        1 if row.get("contract_record_type") in {"parent_contract", "master_agreement"} else 0,
        row.get("end_date", ""),
        row.get("vendor_name", ""),
    )


def pr_date_to_iso(value: Any) -> str:
    text = clean_text(value)
    match = re.search(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b", text)
    if not match:
        return ""
    day, month, year = (int(match.group(1)), int(match.group(2)), int(match.group(3)))
    try:
        return dt.date(year, month, day).isoformat()
    except ValueError:
        return ""


def first_match(text: str, pattern: str) -> str:
    match = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
    return match.group(1) if match else ""


def first_attr(text: str, tag_pattern: str, attr_name: str) -> str:
    match = re.search(tag_pattern, text, re.IGNORECASE | re.DOTALL)
    if not match:
        return ""
    attrs = {name.lower(): clean_text(value, 1000) for name, _quote, value in ATTR_RE.findall(match.group(0))}
    return attrs.get(attr_name.lower(), "")


def strip_html(value: Any, limit: int = 1000) -> str:
    return clean_text(TAG_RE.sub(" ", str(value or "")), limit)


def fold_text(value: Any) -> str:
    text = clean_text(value)
    normalized = unicodedata.normalize("NFKD", text)
    return normalized.encode("ascii", "ignore").decode("ascii").lower()


def folded_has_any(folded_text: str, terms: list[str]) -> bool:
    for term in terms:
        parts = [re.escape(part) for part in re.split(r"\s+", fold_text(term)) if part]
        if not parts:
            continue
        pattern = r"(?<![a-z0-9])" + r"\s+".join(parts) + r"(?![a-z0-9])"
        if re.search(pattern, folded_text):
            return True
    return False


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
