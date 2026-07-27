from __future__ import annotations

import datetime as dt
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable

from services.state_normalization import clean_text, compact_raw_json, iso_date, keyword_hits, parse_date, stable_id

USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"

INFLIGHT_EVENT_TARGET_CONTENT: list[dict[str, Any]] = [
    {"Lbl": "hiddenInput", "Src": "input[type=hidden]", "Data": "id name value"},
    {"Lbl": "genscripts", "Src": "script:contains('ICStateNum')", "Data": "html"},
    {
        "Lbl": "tbl",
        "Src": "[id='l0RESP_INQA_HD_VW$0'],[id='l0RESP_INQA_HD_VW_GR$0']",
        "Data": "id",
        "Children": [
            {
                "Lbl": "tblBodyTr",
                "Src": "tr[id^='trRESP_INQA_HD_VW_GR$0_row'],tr[id^='trRESP_INQA_HD_VW$0_row']",
                "Children": [
                    {
                        "Lbl": "tdEventId",
                        "Src": "a[id^='AUC_ID_COL$'],a[id^='AUC_ID_BUS_UNIT$']",
                        "Data": "text id name href onclick",
                    },
                    {
                        "Lbl": "tdEventName",
                        "Src": "[id^='RESP_INQA1_WK_ZZ_AUC_NAME$'],[id^='RESP_INQA_HD_VW_ZZ_AUC_NAME$']",
                        "Data": "text id name",
                    },
                    {"Lbl": "tdDepName", "Src": "[id^='BUS_UNIT_TBL_FS_DESCR$']", "Data": "text"},
                    {"Lbl": "tdPubDate", "Src": "[id^='AUC_DTTM_FINISH_FR$']", "Data": "text"},
                    {
                        "Lbl": "tdEndDate",
                        "Src": "[id^='RESP_INQA1_WK_AUC_DTTM_FINISH$'],[id^='RESP_INQA_HD_VW_AUC_DTTM_FINISH$']",
                        "Data": "text",
                    },
                    {"Lbl": "tdStatus", "Src": "[id^='ZZ_DERIVED_DESCR']", "Data": "text"},
                ],
            }
        ],
    },
]


@dataclass(frozen=True)
class InFlightPeopleSoftOpportunityConfig:
    state: str
    source_name: str
    source_key: str
    event_search_url: str
    target_url: str
    source_note: str = (
        "Public InFlight NLX facade over PeopleSoft eProcurement event search; initial public results table only."
    )

    @property
    def base_url(self) -> str:
        parts = urllib.parse.urlsplit(self.event_search_url)
        return urllib.parse.urlunsplit((parts.scheme, parts.netloc, "", "", ""))

    @property
    def template_path(self) -> str:
        return urllib.parse.urlsplit(self.event_search_url).path or "/"


class InFlightPeopleSoftClient:
    def __init__(self, config: InFlightPeopleSoftOpportunityConfig) -> None:
        self.config = config
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor())

    def fetch_event_search(self) -> dict[str, Any]:
        self._prime_session()
        return self._post_inflight_json(
            target_url=self.config.target_url,
            target_verb="GET",
            target_content=INFLIGHT_EVENT_TARGET_CONTENT,
            fields={},
        )

    def _prime_session(self) -> None:
        request = urllib.request.Request(self.config.event_search_url, headers=self._headers(accept="text/html,*/*"))
        with self.opener.open(request, timeout=60) as response:
            response.read(1000)
            if getattr(response, "status", response.getcode()) != 200:
                raise RuntimeError(f"PeopleSoft InFlight page returned HTTP {response.getcode()}")

    def _post_inflight_json(
        self,
        *,
        target_url: str,
        target_verb: str,
        target_content: list[dict[str, Any]],
        fields: dict[str, str],
    ) -> dict[str, Any]:
        data = build_inflight_form(
            target_verb=target_verb,
            target_content=target_content,
            template_path=self.config.template_path,
            fields=fields,
        )
        next_url = target_url
        for _attempt in range(5):
            payload, _status = self._post(next_url, data)
            parsed = json.loads(payload.decode("utf-8", "replace"))
            location = parsed.get("IFLocation")
            if not location:
                return parsed
            next_url = urllib.parse.urljoin(self.config.base_url, str(location))
        raise RuntimeError("PeopleSoft InFlight redirect loop did not resolve")

    def _post(self, url: str, data: bytes) -> tuple[bytes, int]:
        headers = self._headers(accept="application/json, text/javascript, */*; q=0.01")
        headers.update(
            {
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "Origin": self.config.base_url,
                "Referer": self.config.event_search_url,
                "X-Requested-With": "XMLHttpRequest",
            }
        )
        request = urllib.request.Request(url, data=data, headers=headers, method="POST")
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                with self.opener.open(request, timeout=90) as response:
                    return response.read(), getattr(response, "status", response.getcode())
            except urllib.error.HTTPError as exc:
                body = exc.read()
                if exc.code == 278:
                    return body, exc.code
                raise RuntimeError(f"PeopleSoft InFlight HTTP {exc.code}: {body[:300]!r}") from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = exc
                time.sleep(1 + attempt)
        raise RuntimeError(f"PeopleSoft InFlight request failed: {last_error}")

    def _headers(self, *, accept: str) -> dict[str, str]:
        return {
            "User-Agent": USER_AGENT,
            "Accept": accept,
            "Accept-Language": "en-US,en;q=0.9",
        }


def build_inflight_form(
    *,
    target_verb: str,
    target_content: list[dict[str, Any]],
    template_path: str,
    fields: dict[str, str],
) -> bytes:
    parts = [
        "IF-TargetVerb=" + urllib.parse.quote_plus(target_verb),
        "IF-TargetContent=" + urllib.parse.quote(json.dumps(target_content, separators=(",", ":")), safe=""),
        "IF-Template=" + urllib.parse.quote(template_path, safe="/"),
        "IF-IgnoreContent=",
    ]
    parts.extend(f"{urllib.parse.quote_plus(key)}={urllib.parse.quote_plus(value)}" for key, value in fields.items())
    return "&".join(parts).encode("utf-8")


def fetch_inflight_peoplesoft_event_opportunities(
    *,
    config: InFlightPeopleSoftOpportunityConfig,
    keywords: list[str],
    days_back: int,
    max_records: int,
    progress: Callable[[str], None] | None = None,
) -> list[dict[str, str]]:
    client = InFlightPeopleSoftClient(config)
    payload = client.fetch_event_search()
    rows = event_rows(payload)
    emit(progress, f"{config.state} PeopleSoft/InFlight public events: {len(rows)} public rows")

    records: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in rows:
        record = normalize_event_row(row, config=config, keywords=keywords)
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


def event_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    capture = payload.get("CaptureResults") if isinstance(payload, dict) else {}
    tables = (capture or {}).get("tbl") if isinstance(capture, dict) else []
    rows: list[dict[str, Any]] = []
    for table in tables or []:
        children = table.get("Children") if isinstance(table, dict) else {}
        table_rows = (children or {}).get("tblBodyTr") if isinstance(children, dict) else []
        rows.extend(row for row in table_rows or [] if isinstance(row, dict))
    return rows


def normalize_event_row(
    row: dict[str, Any],
    *,
    config: InFlightPeopleSoftOpportunityConfig,
    keywords: list[str],
) -> dict[str, str]:
    event_id_props = child_properties(row, "tdEventId")
    title_props = child_properties(row, "tdEventName")
    agency_props = child_properties(row, "tdDepName")
    posted_props = child_properties(row, "tdPubDate")
    due_props = child_properties(row, "tdEndDate")
    status_props = child_properties(row, "tdStatus")

    source_record_id = clean_text(event_id_props.get("text"), 160)
    title = clean_text(title_props.get("text") or source_record_id, 500)
    agency = clean_text(agency_props.get("text"), 180)
    posted_date = iso_date(posted_props.get("text"))
    due_date = iso_date(due_props.get("text"))
    status = clean_text(status_props.get("text") or "Posted", 80)
    search_text = " ".join([source_record_id, title, agency, status])
    matched = keyword_hits(search_text, keywords)
    raw = {
        "source_key": config.source_key,
        "source_note": config.source_note,
        "event_id": event_id_props,
        "title": title_props,
        "agency": agency_props,
        "posted_date": posted_props,
        "due_date": due_props,
        "status": status_props,
    }

    return {
        "id": stable_id(config.state, source_record_id, prefix=f"{config.state.lower()}-peoplesoft-event"),
        "state": config.state,
        "source": config.source_name,
        "source_record_id": source_record_id,
        "title": title,
        "agency": agency,
        "document_type": document_type(source_record_id, title),
        "posted_date": posted_date,
        "due_date": due_date,
        "status": status,
        "amount": "",
        "document_url": config.event_search_url,
        "source_url": config.event_search_url,
        "matched_keywords": ";".join(matched),
        "relevance_score": str(relevance_score(matched, status, search_text, due_date)),
        "raw_json": compact_raw_json(raw),
        "last_checked_at": now_iso(),
    }


def child_properties(row: dict[str, Any], label: str) -> dict[str, Any]:
    children = row.get("Children") if isinstance(row, dict) else {}
    values = (children or {}).get(label) if isinstance(children, dict) else []
    if not values:
        return {}
    first = values[0]
    props = first.get("Properties") if isinstance(first, dict) else {}
    return props if isinstance(props, dict) else {}


def document_type(source_record_id: str, title: str) -> str:
    text = " ".join([source_record_id, title]).lower()
    if "request for information" in text or term_matches(text, "RFI"):
        return "PeopleSoft Request for Information"
    if "request for proposal" in text or term_matches(text, "RFP"):
        return "PeopleSoft Request for Proposal"
    if "request for quote" in text or term_matches(text, "RFQ"):
        return "PeopleSoft Request for Quote"
    if "invitation for bid" in text or term_matches(text, "IFB"):
        return "PeopleSoft Invitation for Bid"
    return "PeopleSoft Event"


def false_keyword_hit(record: dict[str, str]) -> bool:
    text = " ".join([record.get("title", ""), record.get("raw_json", "")])
    return "mmis" in {item.lower() for item in record.get("matched_keywords", "").split(";")} and term_matches(text, "commissary") and not term_matches(text, "MMIS")


def useful_keyword_match(matches: list[str], text: str) -> bool:
    generic_terms = {"claims", "eligibility", "enrollment"}
    matched_terms = {match.lower() for match in matches if match}
    if not matched_terms or not matched_terms <= generic_terms:
        return True
    context_terms = [
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
    if any(term_matches(text, term) for term in ["RFP", "RFI", "RFQ", "IFB", "software", "data", "cloud", "platform", "services"]):
        score += 12
    if status.lower() in {"posted", "open", "modified", "upcoming"}:
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
