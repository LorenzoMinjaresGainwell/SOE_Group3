from __future__ import annotations

import datetime as dt
import re
import unicodedata
import urllib.parse
from typing import Callable

from services.state_updates import state_update_record
from services.state_updates.common import absolute_url, clean_text, fetch_text, head_last_modified, unique_records

ASES_COMMUNICATIONS_URL = "https://www.ases.pr.gov/comunicaciones?categoria=Cartas%2BNormativas%2By%2BCirculares#Documentos"
SALUD_HOME_URL = "https://www.salud.pr.gov/"
SALUD_BASE_URL = "https://www.salud.pr.gov"

ASES_ITEM_RE = re.compile(
    r"(?is)<div\s+pr-data-year=[\"'](?P<year>20\d{2})[\"']\s+role=[\"']listitem[\"']\s+class=[\"']w-dyn-item[\"']>"
    r"(?P<body>.*?)(?=<div\s+pr-data-year=[\"']20\d{2}[\"']\s+role=[\"']listitem[\"']\s+class=[\"']w-dyn-item[\"']>|$)"
)
SALUD_ARTICLE_RE = re.compile(
    r"(?is)<article>.*?<span\s+class=[\"']g-font-size-12[\"']>(?P<date>.*?)</span>.*?"
    r"<h2\b.*?<a\b[^>]*href=[\"'](?P<href>[^\"']+)[\"'][^>]*>(?P<title>.*?)</a>.*?"
    r"<div\s+class=[\"'][^\"']*\bellipsis-3\b[^\"']*[\"'][^>]*>(?P<summary>.*?)</div>.*?</article>"
)

ALLOWED_ASES_CATEGORIES = {
    "cartas normativas y circulares",
    "ordenes administrativas",
    "school base services",
    "comunicados de prensa",
}
PROCUREMENT_TERMS = [
    "subasta",
    "request for proposal",
    "request for information",
    " rfp",
    " rfi",
    "servicios profesionales",
    "contratacion gubernamental",
]
SALUD_CONTEXT_TERMS = [
    "medicaid",
    "medicare",
    "cms",
    "ases",
    "plan vital",
    "fondos federales",
    "politica publica",
    "politica de salud",
    "health policy",
    "telemedicina",
    "telesalud",
    "salud digital",
    "interoperabilidad",
    "hospital universitario",
    "proveedores",
    "fuerza laboral",
    "salud mental",
    "conductual",
    "rural",
]
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
SPANISH_TOPIC_RULES = [
    ("medicaid", "medicaid", "Medicaid", ["medicaid", "plan vital", "ases", "prmp", "programa medicaid"]),
    ("medicare", "medicare", "Medicare", ["medicare"]),
    ("cms", "cms", "CMS", ["cms", "centers for medicare"]),
    ("claims", "claims", "claims", ["reclamos", "facturacion", "codigos de facturacion", "pago", "tarifas"]),
    ("eligibility", "eligibility", "eligibility", ["elegibilidad", "beneficiarios", "participantes"]),
    ("enrollment", "eligibility", "enrollment", ["inscripcion", "registro"]),
    ("managed_care", "managed_care", "managed care", ["mco", "mcos", "aseguradoras", "manejo de cuidado", "organizaciones de manejo"]),
    ("provider_data", "provider_data", "provider data", ["proveedores", "credencializacion", "directorio de proveedores"]),
    ("quality", "quality", "quality measures", ["calidad", "metricas", "hcip", "medical loss ratio", "mlr"]),
    ("interoperability", "interoperability", "interoperability", ["interoperabilidad", "sistemas de informacion", "salud digital", "expediente medico", "datos"]),
    ("prior_authorization", "interoperability", "prior authorization", ["autorizacion previa", "pre-autorizacion"]),
    ("telehealth", "rural_health", "telehealth", ["telemedicina", "telesalud"]),
    ("behavioral_health", "rural_health", "behavioral health", ["salud mental", "conductual", "sustancias"]),
    ("workforce", "rural_health", "workforce", ["fuerza laboral", "profesionales de salud", "retencion de proveedores"]),
    ("rural_health", "rural_health", "rural health", ["rural"]),
]


def fetch_updates(
    *,
    keywords: list[str],
    max_records: int,
    progress: Callable[[str], None] | None = None,
) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    scanned = 0

    ases_rows = fetch_ases_rows()
    scanned += len(ases_rows)
    for row in ases_rows[: max(max_records, 120)]:
        if is_procurement_text(" ".join([row.get("title", ""), row.get("href", "")])):
            continue
        record = ases_record(row, keywords)
        if record:
            records.append(record)
    emit(progress, f"PR: scanned {len(ases_rows)} ASES communications document rows")

    salud_rows = fetch_salud_news_rows()
    scanned += len(salud_rows)
    for row in salud_rows:
        if is_relevant_salud_news(" ".join([row.get("title", ""), row.get("summary", "")]), keywords):
            records.append(salud_record(row, keywords))
    emit(progress, f"PR: scanned {len(salud_rows)} Departamento de Salud news rows")

    output = unique_records(records)
    emit(progress, f"PR: normalized {len(output)} records from {scanned} scanned rows")
    return output[:max_records]


def fetch_ases_rows() -> list[dict[str, str]]:
    markup = fetch_text(ASES_COMMUNICATIONS_URL, timeout=30, byte_limit=800_000)
    rows: list[dict[str, str]] = []
    for match in ASES_ITEM_RE.finditer(markup):
        body = match.group("body")
        link_match = re.search(r"(?is)<a\b[^>]*fs-cmsfilter-field=[\"']Titulo[\"'][^>]*href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", body)
        category_match = re.search(r"(?is)fs-cmsfilter-field=[\"']Categoria[\"'][^>]*>(.*?)</div>", body)
        if not link_match or not category_match:
            continue
        category = clean_text(category_match.group(1))
        if fold_text(category) not in ALLOWED_ASES_CATEGORIES:
            continue
        href = clean_text(link_match.group(1))
        title = clean_text(link_match.group(2))
        if not href or not title:
            continue
        rows.append({"year": match.group("year"), "category": category, "title": title, "href": href})
    return rows


def fetch_salud_news_rows() -> list[dict[str, str]]:
    markup = fetch_text(SALUD_HOME_URL, timeout=30, byte_limit=300_000)
    rows: list[dict[str, str]] = []
    for match in SALUD_ARTICLE_RE.finditer(markup):
        title = clean_text(match.group("title"))
        posted_date = spanish_date_to_iso(match.group("date"))
        if not title or not posted_date:
            continue
        rows.append(
            {
                "title": title,
                "posted_date": posted_date,
                "summary": clean_text(match.group("summary")),
                "href": absolute_url(SALUD_HOME_URL, match.group("href")),
                "date_text": clean_text(match.group("date")),
            }
        )
    return rows


def ases_record(row: dict[str, str], keywords: list[str]) -> dict[str, str] | None:
    href = row.get("href", "")
    updated_date = head_last_modified(href, timeout=12) or date_from_title_code(row.get("title", ""))
    if not updated_date:
        return None
    title = row.get("title", "")
    category = row.get("category", "")
    record = state_update_record(
        state="PR",
        source="pr_ases_communications",
        source_record_id=source_id_from_url(href),
        record_type=ases_record_type(title, category),
        title=title,
        agency="Puerto Rico Administracion de Seguros de Salud (ASES)",
        summary=f"Categoria: {category}; Ano: {row.get('year', '')}",
        updated_date=updated_date,
        document_url=href,
        source_url=ASES_COMMUNICATIONS_URL,
        keywords=keywords,
        raw={"source_page": ASES_COMMUNICATIONS_URL, "category": category, "year": row.get("year", "")},
    )
    return apply_spanish_topic_mappings(record, " ".join([title, category, href, "medicaid plan vital ases"]), keywords)


def salud_record(row: dict[str, str], keywords: list[str]) -> dict[str, str]:
    title = row.get("title", "")
    summary = row.get("summary", "")
    record = state_update_record(
        state="PR",
        source="pr_salud_news",
        source_record_id=source_id_from_url(row.get("href", "")),
        record_type="policy_update",
        title=title,
        agency="Puerto Rico Departamento de Salud",
        summary=summary,
        posted_date=row.get("posted_date", ""),
        source_url=row.get("href", "") or SALUD_HOME_URL,
        keywords=keywords,
        raw={"source_page": SALUD_HOME_URL, "date_text": row.get("date_text", "")},
    )
    return apply_spanish_topic_mappings(record, " ".join([title, summary]), keywords)


def ases_record_type(title: str, category: str) -> str:
    folded = fold_text(" ".join([title, category]))
    if "spa" in folded or "state plan amendment" in folded:
        return "spa_notice"
    if "public notice" in folded or "aviso publico" in folded:
        return "public_comment_notice"
    if "carta normativa" in folded or "carta circular" in folded:
        return "guidance"
    if "orden administrativa" in folded:
        return "policy_update"
    return "policy_update"


def is_relevant_salud_news(text: str, keywords: list[str]) -> bool:
    folded = fold_text(text)
    if any(fold_text(keyword) in folded for keyword in keywords if str(keyword).strip()):
        return True
    return any(term in folded for term in SALUD_CONTEXT_TERMS)


def is_procurement_text(text: str) -> bool:
    folded = fold_text(text)
    return any(term in folded for term in PROCUREMENT_TERMS)


def spanish_date_to_iso(value: str) -> str:
    folded = fold_text(value)
    match = re.search(r"\b(\d{1,2})\s+(?:de\s+)?([a-z]+)\s+(?:de\s+)?(20\d{2})\b", folded)
    if match:
        day = int(match.group(1))
        month = SPANISH_MONTHS.get(match.group(2))
        year = int(match.group(3))
        if month:
            try:
                return dt.date(year, month, day).isoformat()
            except ValueError:
                return ""
    for fmt in ("%m/%d/%Y", "%m-%d-%Y", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(clean_text(value), fmt).date().isoformat()
        except ValueError:
            pass
    return ""


def date_from_title_code(title: str) -> str:
    folded = fold_text(title)
    for match in re.finditer(r"\b(\d{2})\s*-?\s*(\d{2})(\d{2})\b", folded):
        year = 2000 + int(match.group(1))
        month = int(match.group(2))
        day = int(match.group(3))
        try:
            return dt.date(year, month, day).isoformat()
        except ValueError:
            continue
    return ""


def apply_spanish_topic_mappings(row: dict[str, str], text: str, keywords: list[str]) -> dict[str, str]:
    folded = fold_text(text)
    topics = split_semicolon(row.get("topic_keys", ""))
    focuses = split_semicolon(row.get("program_focus", ""))
    matched = split_semicolon(row.get("matched_keywords", ""))
    keyword_by_folded = {fold_text(keyword): str(keyword) for keyword in keywords if str(keyword).strip()}

    for topic, focus, keyword, terms in SPANISH_TOPIC_RULES:
        if not any(term in folded for term in terms):
            continue
        topics.append(topic)
        focuses.append(focus)
        monitored = keyword_by_folded.get(fold_text(keyword))
        if monitored:
            matched.append(monitored)

    row["topic_keys"] = ";".join(unique_sorted(topics))
    row["program_focus"] = ";".join(unique_sorted(focuses))
    row["matched_keywords"] = ";".join(unique_sorted(matched))
    if "rural_health" in row["topic_keys"] or "telehealth" in row["topic_keys"] or "behavioral_health" in row["topic_keys"]:
        row["rht_flag"] = "true"
    if topics:
        try:
            row["importance_score"] = str(min(100, int(row.get("importance_score") or "0") + min(24, len(set(topics)) * 3)))
        except ValueError:
            row["importance_score"] = "25"
    return row


def split_semicolon(value: str) -> list[str]:
    return [item.strip() for item in str(value or "").split(";") if item.strip()]


def unique_sorted(values: list[str]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for value in values:
        item = clean_text(value)
        key = item.lower()
        if not item or key in seen:
            continue
        seen.add(key)
        output.append(item)
    return sorted(output, key=str.lower)


def source_id_from_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(str(url or ""))
    path = urllib.parse.unquote(parsed.path.rstrip("/"))
    name = path.rsplit("/", 1)[-1]
    return clean_text(re.sub(r"\.(pdf|html?)$", "", name, flags=re.I))[:220]


def fold_text(value: object) -> str:
    normalized = unicodedata.normalize("NFKD", clean_text(value))
    return normalized.encode("ascii", "ignore").decode("ascii").lower()


def emit(progress: Callable[[str], None] | None, message: str) -> None:
    if progress:
        progress(message)
