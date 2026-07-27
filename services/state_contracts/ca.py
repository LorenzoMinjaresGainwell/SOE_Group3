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

from services.state_normalization import clean_text, compact_raw_json, iso_date, keyword_hits, months_until, stable_id, term_matches
from services.state_opportunities.peoplesoft import build_inflight_form

LPA_SEARCH_URL = "https://caleprocure.ca.gov/pages/LPASearch/lpa-search.aspx"
LPA_TARGET_URL = "https://caleprocure.ca.gov/nlx3/psc/psfpd1/SUPPLIER/ERP/c/ZZ_PO.ZZ_CNT_SRC_CMP_BKP.GBL"
LPA_TEMPLATE_PATH = "/pages/LPASearch/lpa-search.aspx"
LPA_SOURCE_NAME = "Cal eProcure State Leveraged Procurement Agreements"
LPA_SEARCH_ACTION = "ZZ_CTR_SRC2_WRK_SEARCH_BTN"
LPA_VIEW_100_ACTION = "ZZ_CTR_SRC_VW$hviewall$0"
LPA_NEXT_ACTION = "ZZ_CTR_SRC_VW$hdown$0"
MAX_SCAN_ROWS = 6000
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"

LPA_TARGET_CONTENT: list[dict[str, Any]] = [
    {"Lbl": "grid", "Src": "[id='ZZ_CTR_SRC_VW$scroll$0']", "Data": "text"},
    {
        "Lbl": "resultIds",
        "Src": "[id^='ftrZZ_CTR_SRC_VW$0_row']",
        "Data": "id",
        "Children": [{"Lbl": "contractId", "Src": "[id^='CNTRCT_ID$']", "Data": "text id name"}],
    },
    {
        "Lbl": "rows",
        "Src": "tr[id^='trZZ_CTR_SRC_VW$0_row']",
        "Data": "id",
        "Children": [
            {"Lbl": "description", "Src": "[id^='DESCR2$']", "Data": "text id name"},
            {"Lbl": "contractType", "Src": "[id^='ZZ_CNTRCT_TYPE$']", "Data": "text id name"},
            {"Lbl": "acquisitionType", "Src": "[id^='ZZ_CTR_SRC_VW_ZZ_ACQ_TYPE$']", "Data": "text id name"},
            {"Lbl": "supplierName", "Src": "[id^='NAME11$']", "Data": "text id name"},
            {"Lbl": "supplierId", "Src": "[id^='VENDOR_ID1$']", "Data": "text id name"},
            {"Lbl": "certificationType", "Src": "[id^='DESCR100$']", "Data": "text id name"},
            {"Lbl": "beginDate", "Src": "[id^='CNTRCT_BEGIN_DT$']", "Data": "text id name"},
            {"Lbl": "expireDate", "Src": "[id^='CNTRCT_EXPIRE_DT1$']", "Data": "text id name"},
            {"Lbl": "buyer", "Src": "[id^='OPRDEFNDESC1$']", "Data": "text id name"},
            {"Lbl": "view", "Src": "a[id^='LINK$']", "Data": "text id name href onclick"},
        ],
    },
]


@dataclass(frozen=True)
class LpaRow:
    contract_id: str
    title: str
    contract_type: str
    acquisition_type: str
    supplier_name: str
    supplier_id: str
    certification_type: str
    begin_date: str
    expire_date: str
    buyer: str
    view_action: str
    page_counter: str
    raw: dict[str, Any]


class CalEProcureLpaClient:
    def __init__(self) -> None:
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor())

    def fetch_rows(self, *, max_scan_rows: int, progress: Callable[[str], None] | None = None) -> list[LpaRow]:
        self._prime_session()
        self._post_inflight_json(action=None)
        payload = self._post_inflight_json(action=LPA_SEARCH_ACTION)
        view_payload = self._post_inflight_json(action=LPA_VIEW_100_ACTION)
        if extract_lpa_rows(view_payload):
            payload = view_payload

        rows: list[LpaRow] = []
        seen: set[str] = set()
        while len(rows) < max_scan_rows:
            page_rows = extract_lpa_rows(payload)
            counter = extract_counter(payload)
            for row in page_rows:
                if row.contract_id in seen:
                    continue
                seen.add(row.contract_id)
                rows.append(row)
                if len(rows) >= max_scan_rows:
                    break

            if counter and (len(rows) == len(page_rows) or len(rows) % 1000 < len(page_rows)):
                emit(progress, f"CA Cal eProcure LPAs: scanned {len(rows)} of {counter[2]} public current rows")
            if not should_fetch_next(counter, page_rows, rows, max_scan_rows):
                break
            payload = self._post_inflight_json(action=LPA_NEXT_ACTION)

        return rows

    def _prime_session(self) -> None:
        request = urllib.request.Request(LPA_SEARCH_URL, headers=self._headers(accept="text/html,*/*"))
        with self.opener.open(request, timeout=60) as response:
            response.read(1000)
            if getattr(response, "status", response.getcode()) != 200:
                raise RuntimeError(f"Cal eProcure LPA page returned HTTP {response.getcode()}")

    def _post_inflight_json(self, *, action: str | None) -> dict[str, Any]:
        url = LPA_TARGET_URL
        if action:
            url += "?ICAction=" + urllib.parse.quote(action, safe="$")
        data = build_inflight_form(
            target_verb="GET",
            target_content=LPA_TARGET_CONTENT,
            template_path=LPA_TEMPLATE_PATH,
            fields={},
        )
        next_url = url
        for _attempt in range(5):
            payload, _status = self._post(next_url, data)
            parsed = json.loads(payload.decode("utf-8", "replace"))
            location = parsed.get("IFLocation")
            if not location:
                return parsed
            next_url = urllib.parse.urljoin("https://caleprocure.ca.gov", str(location))
        raise RuntimeError("Cal eProcure LPA InFlight redirect loop did not resolve")

    def _post(self, url: str, data: bytes) -> tuple[bytes, int]:
        headers = self._headers(accept="application/json, text/javascript, */*; q=0.01")
        headers.update(
            {
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "Origin": "https://caleprocure.ca.gov",
                "Referer": LPA_SEARCH_URL,
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
                raise RuntimeError(f"Cal eProcure LPA HTTP {exc.code}: {body[:300]!r}") from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = exc
                time.sleep(1 + attempt)
        raise RuntimeError(f"Cal eProcure LPA request failed: {last_error}")

    def _headers(self, *, accept: str) -> dict[str, str]:
        return {
            "User-Agent": USER_AGENT,
            "Accept": accept,
            "Accept-Language": "en-US,en;q=0.9",
        }


def fetch_contracts(
    *,
    vendor_terms: list[str],
    keywords: list[str],
    max_per_vendor: int,
    progress: Callable[[str], None] | None = None,
) -> list[dict[str, str]]:
    terms = unique_terms(vendor_terms)
    rows = CalEProcureLpaClient().fetch_rows(max_scan_rows=MAX_SCAN_ROWS, progress=progress)
    emit(progress, f"CA Cal eProcure LPAs: scanned {len(rows)} public current contract rows")

    records: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in rows:
        record = normalize_lpa_row(row, vendor_terms=terms, keywords=keywords)
        if not record or record["id"] in seen:
            continue
        seen.add(record["id"])
        records.append(record)

    limit = max(1, max_per_vendor) * max(1, len(terms))
    return sorted(records, key=contract_sort_key, reverse=True)[:limit]


def extract_lpa_rows(payload: dict[str, Any]) -> list[LpaRow]:
    capture = payload.get("CaptureResults") if isinstance(payload, dict) else {}
    if not isinstance(capture, dict):
        return []
    row_items = [row for row in capture.get("rows") or [] if isinstance(row, dict)]
    id_items = [row for row in capture.get("resultIds") or [] if isinstance(row, dict)]
    counter_text = counter_text_from_capture(capture)

    rows: list[LpaRow] = []
    for index, row in enumerate(row_items):
        contract_id = contract_id_for_index(id_items, index)
        raw = {"row": row, "result_id": id_items[index] if index < len(id_items) else {}, "counter": counter_text}
        rows.append(
            LpaRow(
                contract_id=contract_id,
                title=child_text(row, "description", 500),
                contract_type=child_text(row, "contractType", 120),
                acquisition_type=child_text(row, "acquisitionType", 120),
                supplier_name=child_text(row, "supplierName", 180),
                supplier_id=child_text(row, "supplierId", 80),
                certification_type=child_text(row, "certificationType", 120),
                begin_date=child_text(row, "beginDate", 40),
                expire_date=child_text(row, "expireDate", 40),
                buyer=child_text(row, "buyer", 120),
                view_action=child_property(row, "view", "name"),
                page_counter=counter_text,
                raw=raw,
            )
        )
    return rows


def normalize_lpa_row(row: LpaRow, *, vendor_terms: list[str], keywords: list[str]) -> dict[str, str] | None:
    contract_id = clean_text(row.contract_id, 120)
    title = clean_text(row.title, 500)
    vendor_name = clean_text(row.supplier_name, 180)
    end_date = iso_date(row.expire_date)
    if not contract_id or not title or not vendor_name or not end_date:
        return None

    search_text = " ".join(
        [
            contract_id,
            title,
            row.contract_type,
            row.acquisition_type,
            vendor_name,
            row.supplier_id,
            row.certification_type,
            row.buyer,
        ]
    )
    vendor_hits = keyword_hits(vendor_name, vendor_terms)
    matched = keyword_hits(search_text, keywords)
    if not vendor_hits and not useful_keyword_match(matched, search_text):
        return None

    months = months_until(end_date)
    recompete = recompete_signal(months)
    record_type = contract_record_type(row.contract_type)
    raw = {
        "source_key": "ca_caleprocure_lpa",
        "source_note": "Cal eProcure public LPA search current contract rows; portal-side criteria are not relied on.",
        "row": row.raw,
        "supplier_id": row.supplier_id,
        "certification_type": row.certification_type,
        "buyer": row.buyer,
        "view_action": row.view_action,
    }

    return {
        "id": stable_id("CA", contract_id, vendor_name, prefix="ca-caleprocure-lpa"),
        "state": "CA",
        "source": LPA_SOURCE_NAME,
        "source_record_id": contract_id,
        "parent_id": contract_id,
        "contract_record_type": record_type,
        "vendor_name": vendor_name,
        "vendor_query": ";".join(vendor_hits) if vendor_hits else ";".join(matched),
        "agency": "California Department of General Services",
        "contract_number": contract_id,
        "title": title,
        "amount": "0",
        "execution_date": "",
        "start_date": iso_date(row.begin_date),
        "end_date": end_date,
        "months_to_end": "" if months is None else str(months),
        "recompete_signal": recompete,
        "document_type": document_type(row),
        "document_url": LPA_SEARCH_URL,
        "source_url": LPA_SEARCH_URL,
        "matched_keywords": ";".join(matched),
        "relevance_score": str(relevance_score(vendor_hits, matched, recompete, search_text, record_type)),
        "raw_json": compact_raw_json(raw),
        "last_checked_at": now_iso(),
    }


def contract_id_for_index(id_items: list[dict[str, Any]], index: int) -> str:
    if index >= len(id_items):
        return ""
    children = id_items[index].get("Children") if isinstance(id_items[index], dict) else {}
    values = (children or {}).get("contractId") if isinstance(children, dict) else []
    if not values:
        return ""
    props = values[0].get("Properties") if isinstance(values[0], dict) else {}
    return clean_text((props or {}).get("text"), 120)


def child_text(row: dict[str, Any], label: str, limit: int) -> str:
    return clean_text(child_property(row, label, "text"), limit)


def child_property(row: dict[str, Any], label: str, prop_name: str) -> str:
    children = row.get("Children") if isinstance(row, dict) else {}
    values = (children or {}).get(label) if isinstance(children, dict) else []
    if not values:
        return ""
    props = values[0].get("Properties") if isinstance(values[0], dict) else {}
    return str((props or {}).get(prop_name) or "")


def extract_counter(payload: dict[str, Any]) -> tuple[int, int, int] | None:
    capture = payload.get("CaptureResults") if isinstance(payload, dict) else {}
    if not isinstance(capture, dict):
        return None
    match = re.search(r"\b(\d+)-(\d+) of (\d+)\b", counter_text_from_capture(capture))
    if not match:
        return None
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def counter_text_from_capture(capture: dict[str, Any]) -> str:
    grids = capture.get("grid") if isinstance(capture, dict) else []
    if not grids:
        return ""
    props = grids[0].get("Properties") if isinstance(grids[0], dict) else {}
    return clean_text((props or {}).get("text"), 300)


def should_fetch_next(counter: tuple[int, int, int] | None, page_rows: list[LpaRow], rows: list[LpaRow], max_scan_rows: int) -> bool:
    if not page_rows or len(rows) >= max_scan_rows:
        return False
    if not counter:
        return False
    _start, end, total = counter
    return end < total


def useful_keyword_match(matches: list[str], text: str) -> bool:
    if not matches:
        return False
    generic_terms = {"claims", "eligibility", "enrollment", "workforce"}
    matched_terms = {match.lower() for match in matches if match}
    if not matched_terms <= generic_terms:
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


def contract_record_type(contract_type: str) -> str:
    lower = contract_type.lower()
    if "cooperative" in lower:
        return "cooperative_contract"
    if any(term in lower for term in ["cmas", "master", "software license", "state price", "statewide"]):
        return "master_agreement"
    return "parent_contract"


def document_type(row: LpaRow) -> str:
    parts = [row.contract_type, row.acquisition_type]
    text = " - ".join(clean_text(part, 80) for part in parts if clean_text(part))
    return text or "State Leveraged Procurement Agreement"


def relevance_score(vendor_hits: list[str], matches: list[str], recompete: str, text: str, record_type: str) -> int:
    score = min(45, len(matches) * 8)
    if vendor_hits:
        score += 35
    if any(term_matches(text, term) for term in ["Medicaid", "MMIS", "Medicare", "managed care", "provider data"]):
        score += 25
    if any(term_matches(text, term) for term in ["information technology", "IT Services", "software", "cloud", "SaaS"]):
        score += 12
    if recompete == "Expiring soon":
        score += 25
    elif recompete == "Recompete watch":
        score += 18
    elif recompete == "Open-ended/placeholder end date":
        score += 8
    if record_type in {"master_agreement", "cooperative_contract"}:
        score += 8
    return max(0, min(score, 100))


def recompete_signal(months_to_end: int | None) -> str:
    if months_to_end is None:
        return "Unknown end date"
    if months_to_end < 0:
        return "Expired/past award"
    if months_to_end > 600:
        return "Open-ended/placeholder end date"
    if months_to_end <= 18:
        return "Expiring soon"
    if months_to_end <= 36:
        return "Recompete watch"
    return "Longer-term contract"


def contract_sort_key(row: dict[str, str]) -> tuple[int, int, str, str]:
    return (
        int(row.get("relevance_score") or 0),
        1 if row.get("vendor_query") else 0,
        row.get("end_date", ""),
        row.get("title", ""),
    )


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


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def emit(progress: Callable[[str], None] | None, message: str) -> None:
    if progress:
        progress(message)
