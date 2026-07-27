from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from services.csv_store import CsvStore
from services.gov_api_client import DEFAULT_KEYWORDS, SOURCE_META, SearchConfig, run_gov_search
from services.state_contracts import STATE_CLIENTS as STATE_CONTRACT_CLIENTS
from services.state_contracts.store import upsert_state_contracts
from services.state_opportunities import STATE_CLIENTS as STATE_OPPORTUNITY_CLIENTS
from services.state_opportunities.store import upsert_state_opportunities
from services.usaspending_client import load_search_parameters, vendor_searches


AUTO_SOURCES = tuple(source for source in SOURCE_META if source != "sam")
VOLATILE_FIELDS = {"last_checked_at", "last_updated_at", "refresh_label", "refresh_changed_fields"}


class AutoRefresh:
    """Run quota-safe API refreshes and preserve comparison results for the UI."""

    def __init__(self, data_dir: Path, cooldown_seconds: int = 6 * 60 * 60):
        self.data_dir = data_dir
        self.store = CsvStore(data_dir)
        self.cooldown_seconds = cooldown_seconds
        self.state_path = data_dir / "auto_refresh_state.json"
        self.changes_path = data_dir / "refresh_changes.json"
        self._lock = threading.Lock()
        self._running = False
        self._status = self._load_state()

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                **self._status,
                "running": self._running,
                "sam_policy": "cached-only; automatic refresh never calls SAM.gov",
                "auto_sources": list(AUTO_SOURCES),
                "cooldown_seconds": self.cooldown_seconds,
            }

    def start(self, *, force: bool = False) -> dict[str, Any]:
        with self._lock:
            if self._running:
                return self._response(started=False, reason="already_running")
            if not force and not self._due():
                return self._response(started=False, reason="cooldown")
            self._running = True
            self._status = {
                **self._status,
                "status": "running",
                "started_at": now_iso(),
                "message": "Refreshing non-SAM APIs and comparing records…",
            }
            self._save_state()

        threading.Thread(target=self._run, name="api-auto-refresh", daemon=True).start()
        return self._response(started=True)

    def changes(self) -> dict[str, dict[str, Any]]:
        payload = read_json(self.changes_path, {})
        return payload if isinstance(payload, dict) else {}

    def _response(self, **extra: Any) -> dict[str, Any]:
        return {
            **self._status,
            "running": self._running,
            "sam_policy": "cached-only; automatic refresh never calls SAM.gov",
            "auto_sources": list(AUTO_SOURCES),
            "cooldown_seconds": self.cooldown_seconds,
            **extra,
        }

    def _run(self) -> None:
        before = snapshot(self.store)
        try:
            result = run_gov_search(
                SearchConfig(
                    mode="continue",
                    sources=list(AUTO_SOURCES),
                    keywords=DEFAULT_KEYWORDS,
                    max_per_source=100,
                    sam_quota_mode="cache-only",
                    sam_live_budget=0,
                    data_dir=self.data_dir,
                )
            )
            state_result = refresh_state_data(self.data_dir)
            result["state_refresh"] = state_result
            changes = compare_snapshots(before, snapshot(self.store))
            write_json(self.changes_path, changes)
            changed = sum(1 for item in changes.values() if item["label"] in {"New", "Updated"})
            status = result.get("status", "ok")
            completed = now_iso()
            with self._lock:
                self._status = {
                    "status": status,
                    "started_at": self._status.get("started_at", ""),
                    "finished_at": completed,
                    "last_successful_refresh": completed if status == "ok" else self._status.get("last_successful_refresh", ""),
                    "message": f"{result.get('message', 'Refresh complete')} {changed} records labeled as new or updated.",
                    "summary": result,
                    "changed_records": changed,
                }
        except Exception as exc:
            with self._lock:
                self._status = {
                    **self._status,
                    "status": "error",
                    "finished_at": now_iso(),
                    "message": f"Automatic refresh failed: {exc}",
                }
        finally:
            with self._lock:
                self._running = False
                self._save_state()

    def _due(self) -> bool:
        value = self._status.get("last_successful_refresh", "")
        if not value:
            return True
        try:
            last = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return True
        return (datetime.now(timezone.utc) - last).total_seconds() >= self.cooldown_seconds

    def _load_state(self) -> dict[str, Any]:
        payload = read_json(self.state_path, {})
        return payload if isinstance(payload, dict) else {}

    def _save_state(self) -> None:
        write_json(self.state_path, self._status)


def snapshot(store: CsvStore) -> dict[str, dict[str, Any]]:
    records = [*store.list_opportunities(), *store.list_federal_records()]
    return {record["id"]: comparable(record) for record in records if record.get("id")}


def comparable(record: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in record.items()
        if key not in VOLATILE_FIELDS and key not in {"status_history", "fit_breakdown", "analysis"}
    }


def compare_snapshots(
    before: dict[str, dict[str, Any]], after: dict[str, dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    compared: dict[str, dict[str, Any]] = {}
    checked_at = now_iso()
    for record_id, record in after.items():
        source = str(record.get("source", "")).lower()
        if "sam.gov" in source:
            label = "SAM cached"
            changed_fields: list[str] = []
        elif record_id not in before:
            label = "New"
            changed_fields = sorted(record)
        else:
            changed_fields = sorted(
                field
                for field in set(before[record_id]) | set(record)
                if before[record_id].get(field) != record.get(field)
            )
            label = "Updated" if changed_fields else "Current"
        compared[record_id] = {"label": label, "changed_fields": changed_fields, "checked_at": checked_at}
    return compared


def apply_changes(records: list[dict[str, Any]], changes: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    for record in records:
        source = str(record.get("source", "")).lower()
        change = changes.get(record.get("id", ""), {})
        record["refresh_label"] = "SAM cached" if "sam.gov" in source else change.get("label", "")
        record["refresh_changed_fields"] = [] if "sam.gov" in source else change.get("changed_fields", [])
    return records


def refresh_state_data(data_dir: Path) -> dict[str, Any]:
    params = load_search_parameters(data_dir / "search_parameters.json")
    keywords = [str(value) for value in params.get("monitored_keywords") or DEFAULT_KEYWORDS]
    vendors = vendor_searches(params)
    vendor_terms = sorted({query for vendor in vendors for query in vendor.queries})
    opportunity_records: list[dict[str, str]] = []
    contract_records: list[dict[str, str]] = []
    sources: list[dict[str, Any]] = []

    for state, fetcher in sorted(STATE_OPPORTUNITY_CLIENTS.items()):
        try:
            records = fetcher(keywords=keywords, days_back=90, max_records=100, progress=None)
            opportunity_records.extend(records)
            sources.append({"source": f"state_opportunities:{state}", "status": "ok", "records_found": len(records)})
        except Exception as exc:
            sources.append({"source": f"state_opportunities:{state}", "status": "error", "message": str(exc)})

    for state, fetcher in sorted(STATE_CONTRACT_CLIENTS.items()):
        try:
            records = fetcher(vendor_terms=vendor_terms, keywords=keywords, max_per_vendor=25, progress=None)
            contract_records.extend(records)
            sources.append({"source": f"state_contracts:{state}", "status": "ok", "records_found": len(records)})
        except Exception as exc:
            sources.append({"source": f"state_contracts:{state}", "status": "error", "message": str(exc)})

    opportunity_counts = upsert_state_opportunities(data_dir / "state_opportunities.csv", opportunity_records)
    contract_counts = upsert_state_contracts(data_dir / "state_contracts.csv", contract_records)
    return {
        "opportunities": {
            "records_found": len(opportunity_records),
            "added": opportunity_counts[0],
            "updated": opportunity_counts[1],
            "total": opportunity_counts[2],
        },
        "contracts": {
            "records_found": len(contract_records),
            "added": contract_counts[0],
            "updated": contract_counts[1],
            "total": contract_counts[2],
        },
        "sources": sources,
    }


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    temporary.replace(path)


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
