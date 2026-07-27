from __future__ import annotations

import datetime as dt
import hashlib
import json
import urllib.parse
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
DEFAULT_SAM_RAW_CACHE_DIR = DATA_DIR / "raw" / "sam"
DEFAULT_SAM_LEDGER_PATH = DEFAULT_SAM_RAW_CACHE_DIR / "call_ledger.ndjson"
SENSITIVE_NAMES = {"api_key", "apikey", "api-key", "sam_api_key", "key", "token", "authorization"}


@dataclass(frozen=True)
class RedactedRequest:
    method: str
    endpoint: str
    params: dict[str, Any]
    body: Any
    cache_key: str


@dataclass(frozen=True)
class SAMCallLedgerEntry:
    timestamp: str
    endpoint: str
    redacted_params: dict[str, Any]
    cache_hit: bool
    status: str
    record_count: int
    cache_key: str
    method: str = "GET"
    caller: str = ""
    note: str = ""


class RawSAMCache:
    """Disk cache and call ledger for SAM.gov requests.

    The cache key excludes secrets by hashing the redacted request URL/body. This
    lets all SAM callers retry locally without spending personal daily quota.
    """

    def __init__(self, root: Path = DEFAULT_SAM_RAW_CACHE_DIR, ledger_path: Path | None = None) -> None:
        self.root = root
        self.requests_dir = root / "requests"
        self.ledger_path = ledger_path or root / "call_ledger.ndjson"

    def get(self, method: str, url: str, params: dict[str, Any] | None = None, body: Any = None) -> dict[str, Any] | None:
        request = redacted_request(method, url, params, body)
        path = self.cache_path(request.cache_key)
        if not path.exists():
            return None
        with path.open(encoding="utf-8") as handle:
            return json.load(handle)

    def put(
        self,
        method: str,
        url: str,
        params: dict[str, Any] | None,
        body: Any,
        status: int | str,
        response_text: str,
        headers: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        request = redacted_request(method, url, params, body)
        payload = {
            "cache_key": request.cache_key,
            "stored_at": now_iso(),
            "method": request.method,
            "endpoint": request.endpoint,
            "redacted_params": request.params,
            "redacted_body": request.body,
            "response_status": str(status),
            "response_headers": redact_mapping(headers or {}),
            "response_text": response_text,
        }
        self.requests_dir.mkdir(parents=True, exist_ok=True)
        tmp_path = self.cache_path(request.cache_key).with_suffix(".tmp")
        with tmp_path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=True, sort_keys=True)
            handle.write("\n")
        tmp_path.replace(self.cache_path(request.cache_key))
        return payload

    def cache_path(self, cache_key: str) -> Path:
        return self.requests_dir / f"{cache_key}.json"

    def append_ledger(self, entry: SAMCallLedgerEntry) -> None:
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        with self.ledger_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(asdict(entry), ensure_ascii=True, sort_keys=True, separators=(",", ":")))
            handle.write("\n")

    def log(
        self,
        method: str,
        url: str,
        params: dict[str, Any] | None,
        body: Any,
        *,
        cache_hit: bool,
        status: str,
        record_count: int = 0,
        caller: str = "",
        note: str = "",
        timestamp: str | None = None,
    ) -> SAMCallLedgerEntry:
        request = redacted_request(method, url, params, body)
        entry = SAMCallLedgerEntry(
            timestamp=timestamp or now_iso(),
            endpoint=request.endpoint,
            redacted_params=request.params,
            cache_hit=cache_hit,
            status=status,
            record_count=record_count,
            cache_key=request.cache_key,
            method=request.method,
            caller=caller,
            note=note,
        )
        self.append_ledger(entry)
        return entry



def redacted_request(
    method: str,
    url: str,
    params: dict[str, Any] | None = None,
    body: Any = None,
) -> RedactedRequest:
    parsed = urllib.parse.urlsplit(url)
    endpoint = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", "")) or url.split("?", 1)[0]
    query_params = dict(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True))
    merged_params = {**query_params, **(params or {})}
    redacted_params = redact_mapping(merged_params)
    redacted_body = redact_value(body)
    method_name = method.upper()
    key_payload = {
        "method": method_name,
        "endpoint": endpoint,
        "params": stable_json_value(redacted_params),
        "body": stable_json_value(redacted_body),
    }
    cache_key = hashlib.sha256(json.dumps(key_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return RedactedRequest(method=method_name, endpoint=endpoint, params=redacted_params, body=redacted_body, cache_key=cache_key)



def redact_mapping(values: dict[str, Any]) -> dict[str, Any]:
    redacted: dict[str, Any] = {}
    for key in sorted(values):
        if is_sensitive_name(str(key)):
            redacted[str(key)] = "REDACTED"
        else:
            redacted[str(key)] = redact_value(values[key])
    return redacted



def redact_value(value: Any) -> Any:
    if isinstance(value, dict):
        return redact_mapping(value)
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    if isinstance(value, tuple):
        return [redact_value(item) for item in value]
    if isinstance(value, bytes):
        return redact_value(value.decode("utf-8", errors="replace"))
    if isinstance(value, str):
        parsed_json = parse_json(value)
        if parsed_json is not None:
            return redact_value(parsed_json)
        return value
    return value



def stable_json_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): stable_json_value(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [stable_json_value(item) for item in value]
    return value



def is_sensitive_name(name: str) -> bool:
    normalized = name.strip().lower().replace("-", "_")
    return normalized in SENSITIVE_NAMES or normalized.endswith("_api_key") or normalized.endswith("_token")



def parse_json(value: str) -> Any | None:
    stripped = value.strip()
    if not stripped or stripped[0] not in "[{":
        return None
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return None



def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
