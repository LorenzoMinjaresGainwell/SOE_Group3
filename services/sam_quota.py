from __future__ import annotations

import datetime as dt
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from services.sam_cache import DEFAULT_SAM_LEDGER_PATH, RawSAMCache, redacted_request

DEFAULT_PERSONAL_DAILY_LIMIT = 10
DEFAULT_MODE = "cache-only"
LIVE_STATUSES = {"live_ok", "live_error", "http_error", "rate_limited", "timeout"}


class SAMQuotaError(RuntimeError):
    pass


class SAMLiveCallBlocked(SAMQuotaError):
    pass


class SAMQuotaExceeded(SAMQuotaError):
    pass


@dataclass(frozen=True)
class SAMQuotaPolicy:
    mode: str = DEFAULT_MODE
    daily_budget: int = 0
    max_daily_limit: int = DEFAULT_PERSONAL_DAILY_LIMIT
    ledger_path: Path = DEFAULT_SAM_LEDGER_PATH

    @property
    def live_enabled(self) -> bool:
        return self.mode == "live" and self.daily_budget > 0


@dataclass(frozen=True)
class SAMQuotaDecision:
    allowed: bool
    cache_key: str
    endpoint: str
    redacted_params: dict[str, Any]
    used_today: int
    remaining_today: int
    reason: str


class SAMQuotaGuard:
    """Hard gate for every live SAM.gov request.

    Defaults are intentionally cache-only with a zero-call daily budget. A caller
    must opt into live mode and set a small budget before any API request runs.
    """

    def __init__(self, policy: SAMQuotaPolicy | None = None, cache: RawSAMCache | None = None) -> None:
        self.policy = policy or SAMQuotaPolicy()
        self.cache = cache or RawSAMCache(ledger_path=self.policy.ledger_path)

    def require_live_call(
        self,
        method: str,
        url: str,
        params: dict[str, Any] | None = None,
        body: Any = None,
        caller: str = "",
    ) -> SAMQuotaDecision:
        decision = self.decision(method, url, params, body)
        if decision.allowed:
            return decision
        self.cache.log(
            method,
            url,
            params,
            body,
            cache_hit=False,
            status="blocked",
            caller=caller,
            note=decision.reason,
        )
        if self.policy.mode != "live":
            raise SAMLiveCallBlocked(decision.reason)
        raise SAMQuotaExceeded(decision.reason)

    def decision(
        self,
        method: str,
        url: str,
        params: dict[str, Any] | None = None,
        body: Any = None,
        today: dt.date | None = None,
    ) -> SAMQuotaDecision:
        request = redacted_request(method, url, params, body)
        used = live_calls_used_today(self.policy.ledger_path, today=today)
        budget = min(max(self.policy.daily_budget, 0), self.policy.max_daily_limit)
        remaining = max(budget - used, 0)
        if self.policy.mode != "live":
            return SAMQuotaDecision(False, request.cache_key, request.endpoint, request.params, used, remaining, "SAM quota mode is cache-only")
        if budget <= 0:
            return SAMQuotaDecision(False, request.cache_key, request.endpoint, request.params, used, remaining, "SAM live budget is 0")
        if used >= budget:
            return SAMQuotaDecision(False, request.cache_key, request.endpoint, request.params, used, remaining, "SAM live budget exhausted")
        return SAMQuotaDecision(True, request.cache_key, request.endpoint, request.params, used, remaining - 1, "live call allowed")

    def log_cache_hit(
        self,
        method: str,
        url: str,
        params: dict[str, Any] | None = None,
        body: Any = None,
        *,
        record_count: int = 0,
        caller: str = "",
    ) -> None:
        self.cache.log(method, url, params, body, cache_hit=True, status="cache_hit", record_count=record_count, caller=caller)

    def log_live_result(
        self,
        method: str,
        url: str,
        params: dict[str, Any] | None = None,
        body: Any = None,
        *,
        status: str,
        record_count: int = 0,
        caller: str = "",
        note: str = "",
    ) -> None:
        self.cache.log(method, url, params, body, cache_hit=False, status=status, record_count=record_count, caller=caller, note=note)



def policy_from_env(env: dict[str, str] | None = None, ledger_path: Path | None = None) -> SAMQuotaPolicy:
    values = env or os.environ
    mode = (values.get("SAM_QUOTA_MODE") or DEFAULT_MODE).strip().lower()
    if mode not in {"cache-only", "live"}:
        mode = DEFAULT_MODE
    daily_budget = int_or_zero(values.get("SAM_LIVE_DAILY_BUDGET"))
    max_limit = int_or_default(values.get("SAM_PERSONAL_DAILY_LIMIT"), DEFAULT_PERSONAL_DAILY_LIMIT)
    return SAMQuotaPolicy(
        mode=mode,
        daily_budget=max(0, daily_budget),
        max_daily_limit=max(0, max_limit),
        ledger_path=ledger_path or DEFAULT_SAM_LEDGER_PATH,
    )



def explicit_live_policy(daily_budget: int, ledger_path: Path | None = None, max_daily_limit: int = DEFAULT_PERSONAL_DAILY_LIMIT) -> SAMQuotaPolicy:
    return SAMQuotaPolicy(
        mode="live",
        daily_budget=max(0, daily_budget),
        max_daily_limit=max_daily_limit,
        ledger_path=ledger_path or DEFAULT_SAM_LEDGER_PATH,
    )


def policy_from_settings(
    mode: str = DEFAULT_MODE,
    daily_budget: int = 0,
    ledger_path: Path | None = None,
    max_daily_limit: int = DEFAULT_PERSONAL_DAILY_LIMIT,
) -> SAMQuotaPolicy:
    normalized_mode = (mode or DEFAULT_MODE).strip().lower()
    if normalized_mode not in {"cache-only", "live"}:
        normalized_mode = DEFAULT_MODE
    return SAMQuotaPolicy(
        mode=normalized_mode,
        daily_budget=max(0, int_or_zero(daily_budget)),
        max_daily_limit=max(0, int_or_default(max_daily_limit, DEFAULT_PERSONAL_DAILY_LIMIT)),
        ledger_path=ledger_path or DEFAULT_SAM_LEDGER_PATH,
    )



def live_calls_used_today(path: Path = DEFAULT_SAM_LEDGER_PATH, today: dt.date | None = None) -> int:
    if not path.exists():
        return 0
    target = today or dt.datetime.now(dt.timezone.utc).date()
    count = 0
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if bool(row.get("cache_hit")):
                continue
            status = str(row.get("status") or "")
            if status not in LIVE_STATUSES:
                continue
            timestamp = str(row.get("timestamp") or "")
            if entry_date(timestamp) == target:
                count += 1
    return count



def entry_date(timestamp: str) -> dt.date | None:
    if not timestamp:
        return None
    normalized = timestamp.replace("Z", "+00:00")
    try:
        return dt.datetime.fromisoformat(normalized).astimezone(dt.timezone.utc).date()
    except ValueError:
        return None



def int_or_zero(value: Any) -> int:
    return int_or_default(value, 0)



def int_or_default(value: Any, default: int) -> int:
    try:
        return int(str(value or "").strip())
    except ValueError:
        return default
