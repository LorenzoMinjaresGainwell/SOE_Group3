from __future__ import annotations

import datetime as dt
import json
import re
import time
from dataclasses import dataclass
from typing import Any, Callable

from services.state_contracts.keyword_context import useful_keyword_match as keyword_context_match
from services.state_normalization import (
    amount_string,
    clean_text,
    compact_raw_json,
    keyword_hits,
    months_until,
    stable_id,
    term_matches,
)
from services.state_opportunities.vss import (
    VssClient,
    find_carousel_action,
    iter_actions,
    solicitation_id,
    status_label,
    vss_date,
)

SOLICITATION_DS_NAME = "T1SO_SRCH_QRY"
AWARD_HISTORY_DS_NAME = "T1R_COMM_HIS"
VSS_SEARCH_PAUSE_SECONDS = 0.35
VSS_TRANSIENT_RETRY_DELAYS = (2, 8, 20)
PSEUDO_VENDOR_NAMES = {
    "",
    "multiple award vendors",
    "multiple vendors",
    "select vendor",
    "tbd",
    "vendor tbd",
}


@dataclass(frozen=True)
class VssContractConfig:
    state: str
    source_name: str
    base_url: str
    source_key: str = ""
    source_note: str = (
        "Public CGI Advantage VSS guest flow: carousel to Award History and View Published "
        "Solicitations, then awarded solicitation detail JSON."
    )


@dataclass(frozen=True)
class VssSearchResult:
    client: VssClient
    payload: dict[str, Any]
    rows: list[dict[str, Any]]
    detail_action: dict[str, Any] | None


def fetch_vss_awarded_contracts(
    *,
    config: VssContractConfig,
    vendor_terms: list[str],
    keywords: list[str],
    max_per_vendor: int,
    progress: Callable[[str], None] | None = None,
) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    seen_records: set[str] = set()
    seen_sources: set[str] = set()
    limit = max(1, max_per_vendor)

    award_history_available: bool | None = None
    for term in unique_terms(vendor_terms):
        if award_history_available is False:
            emit(progress, f"{config.state} VSS Award History: vendor={term}: public nav unavailable; skipped")
        else:
            try:
                award_rows = retry_vss_operation(lambda: search_award_history_rows(config, term))
            except RuntimeError as exc:
                if "viewAwardHistory" in str(exc):
                    award_history_available = False
                    emit(progress, f"{config.state} VSS Award History: vendor={term}: public nav unavailable; skipped")
                else:
                    emit(progress, f"{config.state} VSS Award History: vendor={term}: skipped after {exc}")
            else:
                award_history_available = True
                emit(progress, f"{config.state} VSS Award History: vendor={term}: scanned {len(award_rows)} public rows; rejected as purchase-history rows without agency/end date")

        try:
            result = retry_vss_operation(lambda: search_published_solicitations(config, term))
        except RuntimeError as exc:
            emit(progress, f"{config.state} VSS Published Solicitations: vendor={term}: skipped after {exc}")
            pause_between_vss_searches()
            continue
        emit(progress, f"{config.state} VSS Published Solicitations: vendor={term}: scanned {len(result.rows)} public rows")
        accepted = add_awarded_records(
            records,
            seen_records=seen_records,
            seen_sources=seen_sources,
            search_result=result,
            config=config,
            vendor_query=term,
            query_type="vendor",
            keywords=keywords,
            limit=limit,
        )
        emit(progress, f"{config.state} VSS Published Solicitations: vendor={term}: normalized {accepted} awarded/current records")
        pause_between_vss_searches()

    for term in unique_terms(keywords):
        try:
            result = retry_vss_operation(lambda: search_published_solicitations(config, term))
        except RuntimeError as exc:
            emit(progress, f"{config.state} VSS Published Solicitations: keyword={term}: skipped after {exc}")
            pause_between_vss_searches()
            continue
        emit(progress, f"{config.state} VSS Published Solicitations: keyword={term}: scanned {len(result.rows)} public rows")
        accepted = add_awarded_records(
            records,
            seen_records=seen_records,
            seen_sources=seen_sources,
            search_result=result,
            config=config,
            vendor_query=term,
            query_type="keyword",
            keywords=keywords,
            limit=limit,
        )
        emit(progress, f"{config.state} VSS Published Solicitations: keyword={term}: normalized {accepted} awarded/current records")
        pause_between_vss_searches()

    return sorted(records, key=contract_sort_key, reverse=True)


def pause_between_vss_searches() -> None:
    time.sleep(VSS_SEARCH_PAUSE_SECONDS)


def retry_vss_operation(operation: Callable[[], Any]) -> Any:
    last_error: RuntimeError | None = None
    for attempt in range(len(VSS_TRANSIENT_RETRY_DELAYS) + 1):
        try:
            return operation()
        except RuntimeError as exc:
            last_error = exc
            if not transient_vss_error(exc) or attempt >= len(VSS_TRANSIENT_RETRY_DELAYS):
                raise
            time.sleep(VSS_TRANSIENT_RETRY_DELAYS[attempt])
    raise RuntimeError(str(last_error) if last_error else "VSS operation failed")


def transient_vss_error(exc: RuntimeError) -> bool:
    text = str(exc)
    return "initial response variable not found" in text or "VSS request failed" in text


def add_awarded_records(
    records: list[dict[str, str]],
    *,
    seen_records: set[str],
    seen_sources: set[str],
    search_result: VssSearchResult,
    config: VssContractConfig,
    vendor_query: str,
    query_type: str,
    keywords: list[str],
    limit: int,
) -> int:
    accepted = 0
    detail_action = search_result.detail_action
    if not detail_action:
        return 0

    for index, row in enumerate(search_result.rows):
        source_record_id = solicitation_id(row)
        if not source_record_id or source_record_id in seen_sources:
            continue
        if not candidate_awarded_row(row, vendor_query=vendor_query, query_type=query_type):
            continue

        try:
            detail = retry_vss_operation(lambda: fetch_detail_for_row(config, row, search_result=search_result if index == 0 else None))
        except RuntimeError as exc:
            if not transient_vss_error(exc):
                raise
            continue
        if not detail:
            continue
        normalized = normalize_detail_records(
            detail,
            source_row=row,
            config=config,
            vendor_query=vendor_query,
            query_type=query_type,
            keywords=keywords,
        )
        if normalized:
            seen_sources.add(source_record_id)
        for record in normalized:
            if record["id"] in seen_records:
                continue
            seen_records.add(record["id"])
            records.append(record)
            accepted += 1
            if accepted >= limit:
                return accepted
    return accepted


def search_award_history_rows(config: VssContractConfig, term: str) -> list[dict[str, Any]]:
    page = open_public_action(config, action_name="viewAwardHistory")
    action = find_search_action(page.payload, AWARD_HISTORY_DS_NAME)
    if not action:
        return []
    response = post_query(page.client, action, AWARD_HISTORY_DS_NAME, {"COMM_CD": "", "QRY_SRCH_STRING": wildcard(term)})
    return ds_rows(response, AWARD_HISTORY_DS_NAME)


def search_published_solicitations(config: VssContractConfig, term: str) -> VssSearchResult:
    page = open_public_action(config, action_name="solicitations")
    action = find_search_action(page.payload, SOLICITATION_DS_NAME)
    if not action:
        return VssSearchResult(page.client, page.payload, [], None)
    response = post_query(
        page.client,
        action,
        SOLICITATION_DS_NAME,
        {"SO_CAT_CD": "", "SO_STA": "", "QRY_SRCH_STRING": wildcard(term)},
    )
    return VssSearchResult(
        page.client,
        response,
        ds_rows(response, SOLICITATION_DS_NAME),
        find_detail_action(response),
    )


def open_public_action(config: VssContractConfig, *, action_name: str) -> VssSearchResult:
    client = VssClient(config.base_url)
    initial = client.initial_payload()
    carousel_action = find_carousel_action(initial)
    if not carousel_action:
        raise RuntimeError("VSS carousel action not found in initial public payload")
    carousel = client.post_action(carousel_action)
    action = find_action_by_name(carousel, action_name)
    if not action:
        raise RuntimeError(f"VSS public action not found: {action_name}")
    payload = client.post_action(action)
    return VssSearchResult(client, payload, [], None)


def post_query(client: VssClient, action: dict[str, Any], ds_name: str, values: dict[str, str]) -> dict[str, Any]:
    payload = {"action": action, "session_info": client.session_info, "data": {"ds_query_data": {ds_name: values}}}
    response = client._request_text(data=json.dumps(payload).encode("utf-8"))
    if not response:
        return {}
    parsed = json.loads(response)
    client._update_session(parsed)
    return parsed


def fetch_detail_for_row(config: VssContractConfig, row: dict[str, Any], *, search_result: VssSearchResult | None) -> dict[str, Any] | None:
    expected_source_id = solicitation_id(row)
    payload: dict[str, Any] | None = None

    if search_result is not None and search_result.detail_action:
        payload = search_result.client.post_action(search_result.detail_action)
    else:
        doc_id = document_id_from_row(row)
        if not doc_id:
            return None
        exact = search_published_solicitations(config, doc_id)
        if not exact.rows or not exact.detail_action:
            return None
        payload = exact.client.post_action(exact.detail_action)

    header = first_row(payload, "T1SO_DOC_HDR")
    actual_source_id = clean_text(header.get("SRCH_DOC_ID") or "", 120)
    if expected_source_id and actual_source_id and actual_source_id != expected_source_id:
        return None
    return payload


def normalize_detail_records(
    payload: dict[str, Any],
    *,
    source_row: dict[str, Any],
    config: VssContractConfig,
    vendor_query: str,
    query_type: str,
    keywords: list[str],
) -> list[dict[str, str]]:
    header = first_row(payload, "T1SO_DOC_HDR")
    source_record_id = clean_text(header.get("SRCH_DOC_ID") or solicitation_id(source_row), 120)
    if not source_record_id:
        return []
    if clean_text(header.get("SO_STA") or source_row.get("SO_STA"), 20).upper() != "A":
        return []

    agency = clean_text(source_row.get("DEPT_NM") or header.get("ISSNG_OFC") or header.get("RQSTR_OFC"), 180)
    title = clean_text(header.get("DOC_DSCR") or source_row.get("DOC_DSCR"), 500)
    if not agency or not title:
        return []

    award_lines = ds_rows(payload, "T31SR_DOC_COMMLN")
    if not award_lines:
        award_lines = award_lines_from_headers(payload)
    if not award_lines:
        return []

    service_lines = ds_rows(payload, "T6SO_DOC_COMMLN") + ds_rows(payload, "T3SO_DOC_COMMLN")
    intent_lines = ds_rows(payload, "T27SR_DOC_COMMLN")
    total_by_vendor = {vendor_key(row.get("LGL_NM")): row.get("TOT_PRICE") for row in ds_rows(payload, "T30SR_DOC_HDR")}

    records: list[dict[str, str]] = []
    for line in award_lines:
        vendor_name = clean_text(line.get("LGL_NM"), 180)
        if vendor_key(vendor_name) in PSEUDO_VENDOR_NAMES:
            continue
        service_line = matching_line(line, service_lines)
        intent_line = matching_intent_line(line, intent_lines)
        start_date = first_vss_date(service_line, "SVC_STRT_DT", "EFBGN_DT")
        end_date = first_vss_date(service_line, "SVC_END_DT", "EFEND_DT")
        if not end_date:
            continue

        amount = first_amount_string(
            total_by_vendor.get(vendor_key(vendor_name)),
            line.get("AWARD_CNTRC_AM"),
            intent_line.get("EST_SVC_CNTRC_AM_VIEW"),
            service_line.get("CNTRC_AM"),
        )
        execution_date = first_vss_date(line, "AWARD_DT") or first_vss_date(header, "AWARD_DT")
        line_no = clean_text(line.get("DOC_COMMLN_LN_NO") or service_line.get("DOC_COMMLN_LN_NO") or "1", 20)
        description = clean_text(line.get("EXT_DSCR") or service_line.get("EXT_DSCR") or service_line.get("CL_DSCR"), 1000)
        matched_text = " ".join(
            [
                vendor_name,
                vendor_query,
                agency,
                title,
                description,
                clean_text(service_line.get("COMM_DSCR") or service_line.get("COMM_CD"), 300),
                source_record_id,
            ]
        )
        matched = keyword_hits(matched_text, keywords)
        if query_type == "keyword" and not term_matches(matched_text, vendor_query):
            continue
        if query_type == "keyword" and not useful_keyword_match(matched, matched_text):
            continue

        months = months_until(end_date)
        recompete = recompete_signal(months)
        record = {
            "id": stable_id(config.state, source_record_id, vendor_name, line_no, prefix=f"{config.state.lower()}-vss-award"),
            "state": config.state,
            "source": config.source_name,
            "source_record_id": f"{source_record_id}:{line_no}",
            "parent_id": source_record_id,
            "contract_record_type": "award",
            "vendor_name": vendor_name,
            "vendor_query": vendor_query,
            "agency": agency,
            "contract_number": source_record_id,
            "title": title,
            "amount": amount,
            "execution_date": execution_date,
            "start_date": start_date,
            "end_date": end_date,
            "months_to_end": "" if months is None else str(months),
            "recompete_signal": recompete,
            "document_type": document_type_label(header, source_row),
            "document_url": config.base_url,
            "source_url": config.base_url,
            "matched_keywords": ";".join(matched),
            "relevance_score": str(relevance_score(matched, amount, recompete, title, vendor_name)),
            "raw_json": compact_raw_json(
                {
                    "source_key": config.source_key,
                    "source_note": config.source_note,
                    "source_row": source_row,
                    "header": header,
                    "award_line": line,
                    "intent_line": intent_line,
                    "service_line": service_line,
                    "award_headers": ds_rows(payload, "T30SR_DOC_HDR"),
                    "attachments": ds_rows(payload, "T34IN_OBJ_ATT_CTLG"),
                }
            ),
            "last_checked_at": now_iso(),
        }
        records.append(record)
    return records


def first_amount_string(*values: Any) -> str:
    zero_amount = ""
    for value in values:
        amount = amount_string(value)
        if amount and int_or_zero(amount) != 0:
            return amount
        if amount and not zero_amount:
            zero_amount = amount
    return zero_amount


def award_lines_from_headers(payload: dict[str, Any]) -> list[dict[str, Any]]:
    lines: list[dict[str, Any]] = []
    for index, row in enumerate(ds_rows(payload, "T30SR_DOC_HDR"), start=1):
        lines.append({"LGL_NM": row.get("LGL_NM"), "AWARD_CNTRC_AM": row.get("TOT_PRICE"), "DOC_COMMLN_LN_NO": str(index)})
    return lines


def matching_line(award_line: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    line_no = clean_text(award_line.get("DOC_COMMLN_LN_NO"), 20)
    group_no = clean_text(award_line.get("DOC_COMMGP_LN_NO"), 20)
    for row in rows:
        if line_no and line_no == clean_text(row.get("DOC_COMMLN_LN_NO"), 20):
            if not group_no or group_no == clean_text(row.get("DOC_COMMGP_LN_NO"), 20):
                return row
    for row in rows:
        if first_vss_date(row, "SVC_END_DT", "EFEND_DT"):
            return row
    return rows[0] if rows else {}


def matching_intent_line(award_line: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    vendor = vendor_key(award_line.get("LGL_NM"))
    line_no = clean_text(award_line.get("DOC_COMMLN_LN_NO"), 20)
    for row in rows:
        if vendor and vendor == vendor_key(row.get("LGL_NM")) and (not line_no or line_no == clean_text(row.get("DOC_COMMLN_LN_NO"), 20)):
            return row
    return {}


def candidate_awarded_row(row: dict[str, Any], *, vendor_query: str, query_type: str) -> bool:
    if clean_text(row.get("SO_STA"), 20).upper() != "A":
        return False
    doc_code = clean_text(row.get("DOC_CD"), 20).upper()
    if doc_code.startswith("NIA"):
        return False
    if query_type == "keyword":
        row_text = " ".join(str(row.get(key) or "") for key in ("DOC_DSCR", "DEPT_NM", "DOC_REF", "DOC_CD_CONCAT", "SO_CAT_CD"))
        return term_matches(row_text, vendor_query)
    return True


def useful_keyword_match(matches: list[str], text: str) -> bool:
    return keyword_context_match(matches, text)


def document_id_from_row(row: dict[str, Any]) -> str:
    doc_ref = clean_text(row.get("DOC_REF"), 300)
    match = re.search(r"\[[^,\]]+,[^,\]]+,([^,\]]+),", doc_ref)
    if match:
        return match.group(1)
    source_id = solicitation_id(row)
    parts = source_id.split("-")
    return parts[2] if len(parts) >= 3 else ""


def find_action_by_name(payload: dict[str, Any], name: str) -> dict[str, Any] | None:
    for action in iter_actions(payload):
        if action.get("protected") is True:
            continue
        if action.get("name") == name:
            return action
    return None


def find_search_action(payload: dict[str, Any], ds_name: str) -> dict[str, Any] | None:
    for action in iter_actions(payload):
        if action.get("protected") is True:
            continue
        if action.get("actionType") == "searchAction" and action.get("dataSource") == ds_name:
            return action
    return None


def find_detail_action(payload: dict[str, Any]) -> dict[str, Any] | None:
    for action in iter_actions(payload):
        if action.get("protected") is True:
            continue
        if action.get("name") == "DOC_REF_Detail" and action.get("actionType") == "transitionAction":
            return action
    return None


def ds_rows(payload: dict[str, Any], ds_name: str) -> list[dict[str, Any]]:
    ds_data = ((payload.get("data") or {}).get("ds_data") or {}) if isinstance(payload, dict) else {}
    rows = ((ds_data.get(ds_name) or {}).get("row_data") or []) if isinstance(ds_data, dict) else []
    return [row for row in rows if isinstance(row, dict)]


def first_row(payload: dict[str, Any], ds_name: str) -> dict[str, Any]:
    rows = ds_rows(payload, ds_name)
    return rows[0] if rows else {}


def first_vss_date(row: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = row.get(key)
        parsed = vss_date(value)
        if parsed:
            return parsed
    return ""


def document_type_label(header: dict[str, Any], source_row: dict[str, Any]) -> str:
    doc_code = clean_text(header.get("DOC_CD") or source_row.get("DOC_CD"), 40)
    doc_type = clean_text(source_row.get("DOC_CD_CONCAT") or doc_code, 120)
    status = status_label(header.get("SO_STA") or source_row.get("SO_STA"))
    return clean_text(f"VSS Awarded Solicitation ({doc_type}; {status})", 160)


def relevance_score(keywords: list[str], amount: str, recompete: str, title: str, vendor_name: str) -> int:
    score = min(45, len(keywords) * 8)
    text = " ".join([title, vendor_name])
    if any(term_matches(text, term) for term in ["Medicaid", "MMIS", "Medicare", "CMS"]):
        score += 25
    if any(term_matches(text, term) for term in ["eligibility", "claims", "enrollment", "managed care", "interoperability", "provider data"]):
        score += 12
    amount_int = int_or_zero(amount)
    if amount_int >= 1_000_000:
        score += 12
    elif amount_int >= 100_000:
        score += 6
    if recompete == "Expiring soon":
        score += 25
    elif recompete == "Recompete watch":
        score += 18
    elif recompete == "Longer-term contract":
        score += 8
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


def contract_sort_key(row: dict[str, str]) -> tuple[int, str, str]:
    return (int_or_zero(row.get("relevance_score")), row.get("end_date", ""), row.get("title", ""))


def unique_terms(terms: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for term in terms:
        cleaned = clean_text(term, 120)
        key = cleaned.lower()
        if cleaned and key not in seen:
            seen.add(key)
            result.append(cleaned)
    return result


def wildcard(term: str) -> str:
    cleaned = clean_text(term, 120).strip("*")
    return f"*{cleaned}*" if cleaned else "*"


def vendor_key(value: Any) -> str:
    return clean_text(value, 180).lower()


def int_or_zero(value: Any) -> int:
    try:
        return int(float(str(value or "0").replace(",", "")))
    except (TypeError, ValueError):
        return 0


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def emit(progress: Callable[[str], None] | None, message: str) -> None:
    if progress:
        progress(message)
