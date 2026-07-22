from __future__ import annotations

import datetime as dt
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

PA_PARTY_URL = "https://www.patreasury.gov/openbookpa/e-library/Home/SelParties/"
PA_CONTRACT_URL = "https://www.patreasury.gov/openbookpa/e-library/Home/SearchContractData"
PA_SOURCE_URL = "https://www.patreasury.gov/openbookpa/e-library/"
USER_AGENT = "soe-group3-pa-contracts/0.1"


def fetch_contracts(
    *,
    vendor_terms: list[str],
    keywords: list[str],
    max_per_vendor: int,
    progress: Callable[[str], None] | None = None,
) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    seen_record_ids: set[str] = set()

    for vendor_term in vendor_terms:
        parties = search_parties(vendor_term)
        emit(progress, f"PA: {vendor_term}: {len(parties)} matching parties")
        vendor_count = 0
        for party in parties:
            contracts = fetch_party_contracts(party["id"], max_per_vendor=max_per_vendor)
            emit(progress, f"PA: {party['text']}: {len(contracts)} contracts")
            for contract in contracts:
                row = normalize_contract(contract, vendor_name=party["text"], vendor_query=vendor_term, keywords=keywords)
                if row["id"] in seen_record_ids:
                    continue
                seen_record_ids.add(row["id"])
                records.append(row)
                vendor_count += 1
                if vendor_count >= max_per_vendor:
                    break
            if vendor_count >= max_per_vendor:
                break
    return sorted(records, key=contract_sort_key, reverse=True)


def search_parties(term: str) -> list[dict[str, str]]:
    url = PA_PARTY_URL + "?" + urllib.parse.urlencode({"id": term})
    data = http_json(url)
    parties: list[dict[str, str]] = []
    for row in data if isinstance(data, list) else []:
        party_id = str(row.get("id") or row.get("PartyId") or "")
        party_text = str(row.get("text") or row.get("PartyName") or "").strip()
        if party_id and party_text:
            parties.append({"id": party_id, "text": party_text})
    return parties


def fetch_party_contracts(party_id: str, max_per_vendor: int) -> list[dict[str, Any]]:
    payload = datatables_payload(party_id, length=max(max_per_vendor, 100))
    data = http_json(PA_CONTRACT_URL, payload=payload)
    return data.get("data") or []


def datatables_payload(party_id: str, length: int) -> dict[str, str]:
    payload = {
        "draw": "1",
        "start": "0",
        "length": str(min(length, 500)),
        "search[value]": "",
        "search[regex]": "false",
        "hdnPartyName": "",
        "PartyName": "",
        "AgencyId": "",
        "ContractAmountList": "",
        "AmountTypeText": "",
        "SubjectMatter": "",
        "ContractNumber": "",
        "SelectedPartyIds": party_id,
        "SelectedAgencyIds": "",
        "CaptchaToken": "",
    }
    for index, name in enumerate(["ContractID", "SubjectMatter", "AgencyName", "ContractAmount", "ExecutionDate", "DocName", "PartyName"]):
        payload[f"columns[{index}][data]"] = name
        payload[f"columns[{index}][name]"] = ""
        payload[f"columns[{index}][searchable]"] = "true"
        payload[f"columns[{index}][orderable]"] = "false"
        payload[f"columns[{index}][search][value]"] = ""
        payload[f"columns[{index}][search][regex]"] = "false"
    return payload


def normalize_contract(contract: dict[str, Any], *, vendor_name: str, vendor_query: str, keywords: list[str]) -> dict[str, str]:
    contract_id = str(contract.get("ContractID") or "")
    amount = int(float(contract.get("ContractAmount") or 0))
    parent_id = parent_id_for(contract, contract_id)
    record_type = contract_record_type(contract, contract_id=contract_id, parent_id=parent_id)
    title = clean_text(contract.get("SubjectMatter") or "", 500)
    agency = clean_text(str(contract.get("AgencyName") or "").rstrip(","), 180)
    matched = keyword_hits(" ".join([vendor_name, vendor_query, agency, title, str(contract.get("ContractNumber") or "")]), keywords)
    end_date, months, recompete = normalized_end_date_fields(contract.get("EndDate"))
    score = relevance_score(matched, amount, recompete, title, record_type)

    return {
        "id": f"pa-{contract_id}",
        "state": "PA",
        "source": "PA Treasury OpenBookPA Contracts e-Library",
        "source_record_id": contract_id,
        "parent_id": parent_id,
        "contract_record_type": record_type,
        "vendor_name": clean_text(vendor_name, 180),
        "vendor_query": vendor_query,
        "agency": agency,
        "contract_number": str(contract.get("ContractNumber") or ""),
        "title": title,
        "amount": str(amount),
        "execution_date": iso_date(contract.get("ExecutionDate")),
        "start_date": iso_date(contract.get("BeginDate")),
        "end_date": end_date,
        "months_to_end": "" if months is None else str(months),
        "recompete_signal": recompete,
        "document_type": document_type_label(contract, record_type),
        "document_url": f"https://www.patreasury.gov/openbookpa/e-library/Home/ContractView?id={contract_id}" if contract_id else PA_SOURCE_URL,
        "source_url": PA_SOURCE_URL,
        "matched_keywords": ";".join(matched),
        "relevance_score": str(score),
        "raw_json": json.dumps(contract, ensure_ascii=False, sort_keys=True),
        "last_checked_at": now_iso(),
    }


def http_json(url: str, payload: dict[str, str] | None = None, timeout: int = 45) -> Any:
    data = None if payload is None else urllib.parse.urlencode(payload).encode("utf-8")
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Referer": PA_SOURCE_URL,
    }
    if payload is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"
        headers["X-Requested-With"] = "XMLHttpRequest"
    request = urllib.request.Request(url, data=data, headers=headers)
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read(600).decode("utf-8", "replace")
            raise RuntimeError(f"HTTP {exc.code} from {url}: {body}") from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = exc
            time.sleep(1 + attempt)
    raise RuntimeError(f"PA request failed: {last_error}")


def keyword_hits(text: str, keywords: list[str]) -> list[str]:
    lower = text.lower()
    return sorted({keyword for keyword in keywords if keyword and keyword.lower() in lower}, key=str.lower)


def normalized_end_date_fields(value: Any) -> tuple[str, int | None, str]:
    end_date = iso_date(value)
    if is_placeholder_end_date(end_date):
        return "", None, "Open-ended/placeholder end date"
    months = months_until(end_date)
    return end_date, months, recompete_signal(months)


def is_placeholder_end_date(value: Any) -> bool:
    parsed = parse_date(value)
    return bool(parsed and parsed.year >= 2090 and parsed.month == 12 and parsed.day == 31)


def parent_id_for(contract: dict[str, Any], contract_id: str) -> str:
    return clean_id(contract.get("ParentID")) or contract_id


def contract_record_type(contract: dict[str, Any], *, contract_id: str, parent_id: str) -> str:
    doc_name = str(contract.get("DocName") or "").lower()
    contract_number = str(contract.get("ContractNumber") or "").upper()
    if parent_id and parent_id != contract_id:
        return "amendment"
    if "amend" in doc_name or re.search(r"\b(AMND|AMENDMENT|FA)\b", contract_number):
        return "amendment"
    return "parent_contract"


def document_type_label(contract: dict[str, Any], record_type: str) -> str:
    if record_type == "parent_contract":
        return "Parent Contract"
    doc_name = clean_text(contract.get("DocName") or "Amendment", 80)
    return doc_name if "amend" in doc_name.lower() else f"Amendment ({doc_name})"


def contract_sort_key(row: dict[str, str]) -> tuple[int, int, str, int]:
    return (
        int(row.get("relevance_score") or 0),
        1 if row.get("contract_record_type") == "parent_contract" else 0,
        row.get("end_date", ""),
        int(row.get("amount") or 0),
    )


def relevance_score(keywords: list[str], amount: int, recompete: str, title: str, record_type: str) -> int:
    score = min(45, len(keywords) * 8)
    lower = title.lower()
    if "medicaid" in lower or "mmis" in lower:
        score += 25
    if "itq" in lower or "information technology" in lower:
        score += 12
    if amount >= 1_000_000:
        score += 12
    elif amount >= 100_000:
        score += 6
    if recompete == "Expiring soon":
        score += 25
    elif recompete == "Recompete watch":
        score += 18
    elif recompete == "Open-ended/placeholder end date":
        score += 8
    if record_type == "parent_contract":
        score += 18
    elif record_type == "amendment":
        score -= 15
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


def months_until(value: Any) -> int | None:
    parsed = parse_date(value)
    if not parsed:
        return None
    today = dt.date.today()
    return (parsed.year - today.year) * 12 + (parsed.month - today.month)


def parse_date(value: Any) -> dt.date | None:
    if not value:
        return None
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y"):
        try:
            return dt.datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    return None


def iso_date(value: Any) -> str:
    parsed = parse_date(value)
    return parsed.isoformat() if parsed else ""


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def clean_id(value: Any) -> str:
    if value is None or value == "":
        return ""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value).strip()
    return str(int(number)) if number.is_integer() else str(value).strip()


def clean_text(value: Any, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "..."


def emit(progress: Callable[[str], None] | None, message: str) -> None:
    if progress:
        progress(message)
