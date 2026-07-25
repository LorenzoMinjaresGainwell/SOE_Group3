from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

DEFAULT_USER_AGENT = "soe-group3-state-probe/0.1"
DEFAULT_TIMEOUT = 30
DEFAULT_BYTE_LIMIT = 1_000_000
SENSITIVE_QUERY_PARTS = (
    "api_key",
    "apikey",
    "access_key",
    "subscription_key",
    "token",
    "secret",
    "password",
    "passwd",
    "signature",
    "client_secret",
    "authorization",
    "auth",
    "key",
)
URL_PATTERN = re.compile(r"https?://[^\s<>'\"]+")
SECRET_ASSIGNMENT_PATTERN = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|token|secret|password|client[_-]?secret|signature)=([^\s&]+)"
)


@dataclass(frozen=True)
class HttpResult:
    requested_url: str
    final_url: str
    status_code: int | None
    content_type: str
    body: bytes
    truncated: bool
    error: str = ""
    elapsed_ms: int = 0

    @property
    def ok(self) -> bool:
        return self.error == "" and self.status_code is not None and 200 <= self.status_code < 400

    @property
    def redacted_url(self) -> str:
        return redact_url(self.requested_url)

    @property
    def redacted_final_url(self) -> str:
        return redact_url(self.final_url)

    def body_text(self, *, limit: int | None = None) -> str:
        text = self.body.decode("utf-8", "replace")
        return text if limit is None else text[:limit]

    def json_data(self) -> Any:
        return json.loads(self.body_text())

    def metadata(self) -> dict[str, Any]:
        return {
            "url": self.redacted_url,
            "final_url": self.redacted_final_url,
            "status_code": self.status_code,
            "content_type": self.content_type,
            "bytes_read": len(self.body),
            "truncated": self.truncated,
            "elapsed_ms": self.elapsed_ms,
            "error": redact_message(self.error),
        }

    def raise_for_status(self) -> None:
        if self.ok:
            return
        status = "no status" if self.status_code is None else str(self.status_code)
        error = f": {redact_message(self.error)}" if self.error else ""
        raise RuntimeError(f"HTTP request failed ({status}) for {self.redacted_url}{error}")


def fetch_url(
    url: str,
    *,
    method: str = "GET",
    data: bytes | str | dict[str, Any] | None = None,
    json_data: Any | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    byte_limit: int = DEFAULT_BYTE_LIMIT,
    user_agent: str = DEFAULT_USER_AGENT,
) -> HttpResult:
    body, request_headers = build_request_body(data=data, json_data=json_data, headers=headers)
    if not any(name.lower() == "user-agent" for name in request_headers):
        request_headers["User-Agent"] = user_agent
    if not any(name.lower() == "accept" for name in request_headers):
        request_headers["Accept"] = "*/*"

    request = urllib.request.Request(url, data=body, headers=request_headers, method=method.upper())
    start = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            content, truncated = read_limited(response, byte_limit)
            return HttpResult(
                requested_url=url,
                final_url=response.geturl(),
                status_code=getattr(response, "status", response.getcode()),
                content_type=response.headers.get("Content-Type", ""),
                body=content,
                truncated=truncated,
                elapsed_ms=elapsed_ms(start),
            )
    except urllib.error.HTTPError as exc:
        content, truncated = read_limited(exc, byte_limit)
        return HttpResult(
            requested_url=url,
            final_url=exc.geturl(),
            status_code=exc.code,
            content_type=exc.headers.get("Content-Type", "") if exc.headers else "",
            body=content,
            truncated=truncated,
            error=f"HTTP {exc.code}",
            elapsed_ms=elapsed_ms(start),
        )
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return HttpResult(
            requested_url=url,
            final_url=url,
            status_code=None,
            content_type="",
            body=b"",
            truncated=False,
            error=redact_message(str(exc)),
            elapsed_ms=elapsed_ms(start),
        )


def fetch_json(url: str, **kwargs: Any) -> Any:
    headers = dict(kwargs.pop("headers", {}) or {})
    headers.setdefault("Accept", "application/json, text/javascript, */*; q=0.01")
    result = fetch_url(url, headers=headers, **kwargs)
    result.raise_for_status()
    return result.json_data()


def build_request_body(
    *,
    data: bytes | str | dict[str, Any] | None,
    json_data: Any | None,
    headers: dict[str, str] | None,
) -> tuple[bytes | None, dict[str, str]]:
    request_headers = dict(headers or {})
    if json_data is not None:
        request_headers.setdefault("Content-Type", "application/json")
        return json.dumps(json_data).encode("utf-8"), request_headers
    if data is None:
        return None, request_headers
    if isinstance(data, bytes):
        return data, request_headers
    if isinstance(data, str):
        return data.encode("utf-8"), request_headers
    request_headers.setdefault("Content-Type", "application/x-www-form-urlencoded; charset=UTF-8")
    return urllib.parse.urlencode(data).encode("utf-8"), request_headers


def read_limited(response: Any, byte_limit: int) -> tuple[bytes, bool]:
    limit = max(0, int(byte_limit))
    content = response.read(limit + 1)
    if len(content) > limit:
        return content[:limit], True
    return content, False


def elapsed_ms(start: float) -> int:
    return int((time.monotonic() - start) * 1000)


def redact_url(url: str) -> str:
    try:
        parts = urllib.parse.urlsplit(str(url))
    except ValueError:
        return redact_message_without_urls(str(url))

    netloc = redact_userinfo(parts.netloc)
    query_pairs = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    safe_pairs = [(key, "REDACTED" if is_sensitive_key(key) else value) for key, value in query_pairs]
    safe_query = urllib.parse.urlencode(safe_pairs, doseq=True)
    fragment = "REDACTED" if any(part in parts.fragment.lower() for part in SENSITIVE_QUERY_PARTS) else parts.fragment
    return urllib.parse.urlunsplit((parts.scheme, netloc, parts.path, safe_query, fragment))


def redact_userinfo(netloc: str) -> str:
    if "@" not in netloc:
        return netloc
    userinfo, host = netloc.rsplit("@", 1)
    if ":" in userinfo:
        username = userinfo.split(":", 1)[0]
        return f"{username}:REDACTED@{host}"
    return f"REDACTED@{host}"


def is_sensitive_key(key: str) -> bool:
    normalized = key.lower().replace("-", "_")
    return any(part in normalized for part in SENSITIVE_QUERY_PARTS)


def redact_message(message: str) -> str:
    with_safe_urls = URL_PATTERN.sub(lambda match: redact_url(match.group(0)), str(message))
    return redact_message_without_urls(with_safe_urls)


def redact_message_without_urls(message: str) -> str:
    return SECRET_ASSIGNMENT_PATTERN.sub(lambda match: f"{match.group(1)}=REDACTED", str(message))
