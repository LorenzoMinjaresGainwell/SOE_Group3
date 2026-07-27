from __future__ import annotations

import datetime as dt
import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Iterable

from services.state_normalization import clean_text, compact_raw_json, iso_date, keyword_hits, parse_date, stable_id

USER_AGENT = "soe-group3-vss-opportunities/0.1"
SOLICITATION_DS_NAME = "T1SO_SRCH_QRY"


@dataclass(frozen=True)
class VssOpportunityConfig:
    state: str
    source_name: str
    base_url: str
    source_key: str = ""
    source_note: str = (
        "Public CGI Advantage VSS guest flow: initial Advantage4 payload, carousel nav action, "
        "then View Published Solicitations SystemInquiryPage JSON."
    )


class VssClient:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor())
        self.session_info: dict[str, Any] = {}

    def initial_payload(self) -> dict[str, Any]:
        html = self._request_text()
        payload = extract_initial_response(html)
        self._update_session(payload)
        return payload

    def post_action(self, action: dict[str, Any]) -> dict[str, Any]:
        payload = {"action": action, "session_info": self.session_info}
        response = self._request_text(data=json.dumps(payload).encode("utf-8"))
        if not response:
            return {}
        parsed = json.loads(response)
        self._update_session(parsed)
        return parsed

    def _request_text(self, data: bytes | None = None, timeout: int = 60) -> str:
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/plain, */*" if data is not None else "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Referer": self.base_url,
        }
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.base_url, data=data, headers=headers)
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                with self.opener.open(request, timeout=timeout) as response:
                    return response.read().decode("utf-8", "replace")
            except urllib.error.HTTPError as exc:
                body = exc.read(800).decode("utf-8", "replace")
                raise RuntimeError(f"HTTP {exc.code} from VSS endpoint: {body}") from exc
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                last_error = exc
                time.sleep(1 + attempt)
        raise RuntimeError(f"VSS request failed: {last_error}")

    def _update_session(self, payload: dict[str, Any]) -> None:
        session_info = payload.get("session_info")
        if isinstance(session_info, dict):
            self.session_info = session_info


def fetch_vss_published_solicitations(
    *,
    config: VssOpportunityConfig,
    keywords: list[str],
    days_back: int,
    max_records: int,
    progress: Callable[[str], None] | None = None,
) -> list[dict[str, str]]:
    client = VssClient(config.base_url)
    initial = client.initial_payload()
    carousel_action = find_carousel_action(initial)
    if not carousel_action:
        raise RuntimeError("VSS carousel action not found in initial public payload")

    carousel = client.post_action(carousel_action)
    solicitation_action = find_public_action(carousel, title_pattern=r"published\s+solicitations|solicitations")
    if not solicitation_action:
        raise RuntimeError("VSS published solicitations action not found in public carousel payload")

    response = client.post_action(solicitation_action)
    rows = solicitation_rows(response)
    emit(progress, f"{config.state} VSS published solicitations: {len(rows)} public rows")

    records: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in rows:
        record = normalize_solicitation_row(row, config=config, keywords=keywords)
        if not record.get("source_record_id") or record["id"] in seen:
            continue
        if false_keyword_hit(record) or not useful_keyword_match(record["matched_keywords"].split(";"), record["raw_json"]):
            continue
        if not is_open_or_recent(record["posted_date"], record["due_date"], days_back):
            continue
        if keywords and not record["matched_keywords"]:
            continue
        seen.add(record["id"])
        records.append(record)

    return sorted(records, key=record_sort_key, reverse=True)[: max(1, max_records)]


def extract_initial_response(html: str) -> dict[str, Any]:
    match = re.search(r"var\s+moInitialResponse\s*=\s*", html)
    if not match:
        raise RuntimeError("VSS initial response variable not found")
    start = match.end()
    end = find_json_object_end(html, start)
    return json.loads(html[start:end])


def find_json_object_end(text: str, start: int) -> int:
    depth = 0
    quote: str | None = None
    escaped = False
    for index, char in enumerate(text[start:], start=start):
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in {'"', "'"}:
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index + 1
    raise RuntimeError("VSS initial response JSON was not terminated")


def find_carousel_action(payload: dict[str, Any]) -> dict[str, Any] | None:
    for action in iter_actions(payload):
        name = str(action.get("name") or "").lower()
        component = str(action.get("targetComponentType") or "").lower()
        if action.get("protected") is True:
            continue
        if name in {"carousalaction", "carouselaction"} or component == "customcarouselpage":
            return action
    return None


def find_public_action(payload: dict[str, Any], *, title_pattern: str) -> dict[str, Any] | None:
    pattern = re.compile(title_pattern, re.IGNORECASE)
    for action in iter_actions(payload):
        if action.get("protected") is True:
            continue
        text = " ".join(str(action.get(key) or "") for key in ("title", "name", "targetComponentType", "targetQualifiedName"))
        if pattern.search(text):
            return action
    return None


def iter_actions(payload: Any) -> Iterable[dict[str, Any]]:
    if isinstance(payload, dict):
        if payload.get("type") == "nav" or any(key in payload for key in ("actionType", "actionCode", "targetQualifiedName")):
            yield payload
        for value in payload.values():
            yield from iter_actions(value)
    elif isinstance(payload, list):
        for value in payload:
            yield from iter_actions(value)


def solicitation_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    ds_data = ((payload.get("data") or {}).get("ds_data") or {}) if isinstance(payload, dict) else {}
    source = ds_data.get(SOLICITATION_DS_NAME) if isinstance(ds_data, dict) else None
    rows = (source or {}).get("row_data") if isinstance(source, dict) else None
    return [row for row in rows or [] if isinstance(row, dict)]


def normalize_solicitation_row(row: dict[str, Any], *, config: VssOpportunityConfig, keywords: list[str]) -> dict[str, str]:
    source_record_id = solicitation_id(row)
    title = clean_text(row.get("DOC_DSCR") or source_record_id, 500)
    agency = clean_text(row.get("DEPT_NM"), 180)
    buyer = clean_text(row.get("BUYR_NM"), 180)
    doc_type = clean_text(row.get("DOC_CD_CONCAT") or row.get("DOC_CD") or "VSS Solicitation", 120)
    posted_date = vss_date(row.get("PUB_DT") or row.get("AMND_DT"))
    due_date = vss_date(row.get("SO_CLSNG_DT_TM") or row.get("PUB_BID_OP_DT"))
    status = status_label(row.get("SO_STA"))
    search_text = " ".join([source_record_id, title, agency, buyer, doc_type, clean_text(row.get("SO_CAT_CD"), 80)])
    matched = keyword_hits(search_text, keywords)
    raw = dict(row)
    raw["source_key"] = config.source_key
    raw["source_note"] = config.source_note

    return {
        "id": stable_id(config.state, source_record_id, prefix=f"{config.state.lower()}-vss-solicitation"),
        "state": config.state,
        "source": config.source_name,
        "source_record_id": source_record_id,
        "title": title,
        "agency": agency,
        "document_type": document_type_label(doc_type, row.get("DOC_CD")),
        "posted_date": posted_date,
        "due_date": due_date,
        "status": status,
        "amount": "",
        "document_url": config.base_url,
        "source_url": config.base_url,
        "matched_keywords": ";".join(matched),
        "relevance_score": str(relevance_score(matched, status, search_text, due_date)),
        "raw_json": compact_raw_json(raw),
        "last_checked_at": now_iso(),
    }


def solicitation_id(row: dict[str, Any]) -> str:
    doc_ref = clean_text(row.get("DOC_REF"), 240)
    bracketed = re.findall(r"\[([^\]]+)\]", doc_ref)
    if bracketed:
        return bracketed[-1]
    return clean_text(row.get("DOC_CD") or row.get("ADV_ROW_ID") or row.get("DOC_DSCR"), 160)


def document_type_label(doc_type: str, doc_code: Any) -> str:
    text = " ".join([doc_type, clean_text(doc_code, 40)]).lower()
    if "intent to award" in text or "award" in text:
        return "VSS Notice of Intent to Award"
    if "request for proposal" in text or term_matches(text, "RFP"):
        return "VSS Request for Proposal"
    if "request for quote" in text or term_matches(text, "RFQ"):
        return "VSS Request for Quote"
    if "request for information" in text or term_matches(text, "RFI"):
        return "VSS Request for Information"
    return doc_type or "VSS Solicitation"


def status_label(value: Any) -> str:
    code = clean_text(value, 20).upper()
    return {
        "O": "Open",
        "M": "Modified",
        "C": "Closed",
        "A": "Awarded",
        "X": "Cancelled",
    }.get(code, code or "")


def vss_date(value: Any) -> str:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            timestamp = float(value) / (1000 if abs(float(value)) > 10_000_000_000 else 1)
            return dt.datetime.fromtimestamp(timestamp, tz=dt.timezone.utc).date().isoformat()
        except (OverflowError, OSError, ValueError):
            return ""
    text = clean_text(value, 80)
    if re.fullmatch(r"-?\d+(?:\.\d+)?", text):
        return vss_date(float(text))
    return iso_date(text)


def false_keyword_hit(record: dict[str, str]) -> bool:
    text = " ".join([record.get("title", ""), record.get("raw_json", "")])
    return "mmis" in {item.lower() for item in record.get("matched_keywords", "").split(";")} and term_matches(text, "commissary") and not term_matches(text, "MMIS")


def useful_keyword_match(matches: list[str], text: str) -> bool:
    generic_terms = {"claims", "eligibility", "enrollment"}
    matched_terms = {match.lower() for match in matches if match}
    if not matched_terms or not matched_terms <= generic_terms:
        return True
    context_terms = [
        "health and human services",
        "human services",
        "healthcare",
        "health care",
        "medicaid",
        "medicare",
        "medical",
        "health",
        "hospital",
        "behavioral",
        "managed care",
        "provider",
        "chip",
        "mmis",
    ]
    return any(term_matches(text, term) for term in context_terms)


def relevance_score(matches: list[str], status: str, text: str, due_date: str) -> int:
    score = min(50, len(matches) * 10)
    if any(term_matches(text, term) for term in ["Medicaid", "MMIS", "Human Services", "Health Care", "Healthcare"]):
        score += 25
    if any(term_matches(text, term) for term in ["eligibility", "claims", "enrollment", "managed care", "interoperability", "FHIR", "prior authorization", "provider data"]):
        score += 15
    if term_matches(text, "rural health"):
        score += 25
    if any(term_matches(text, term) for term in ["RFP", "RFI", "RFQ", "software", "data", "cloud", "platform"]):
        score += 12
    if status.lower() in {"open", "modified", "posted", "upcoming"}:
        score += 10
    parsed_due = parse_date(due_date)
    if parsed_due and parsed_due >= dt.date.today():
        score += 8
    return min(score, 100)


def is_open_or_recent(posted_date: str, due_date: str, days_back: int) -> bool:
    due = parse_date(due_date)
    if due and due >= dt.date.today():
        return True
    if not posted_date:
        return True
    posted = parse_date(posted_date)
    return not posted or days_back <= 0 or (dt.date.today() - posted).days <= days_back


def record_sort_key(row: dict[str, str]) -> tuple[int, str, str]:
    return (int_or_zero(row.get("relevance_score")), row.get("due_date", ""), row.get("title", ""))


def term_matches(text: Any, term: str) -> bool:
    return bool(keyword_hits(str(text or ""), [term]))


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
