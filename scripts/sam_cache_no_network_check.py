#!/usr/bin/env python3
"""Assert SAM cache/quota integration avoids accidental live network use."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.gov_api_client import SearchConfig, fetch_sam  # noqa: E402
from services.sam_contract_awards_client import SAMContractAwardsConfig, fetch_sam_contract_awards  # noqa: E402
from services.sam_entity_client import SamEntityConfig, SamEntityError, fetch_entities_by_name  # noqa: E402

DUMMY_SAM_KEY = "TEST_SAM_KEY_DO_NOT_PRINT"


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="sam-cache-check-") as tmp_name:
        tmp = Path(tmp_name)
        data_dir = tmp / "data"
        cache_dir = data_dir / "raw" / "sam"
        ledger_path = cache_dir / "call_ledger.ndjson"
        params_path = data_dir / "search_parameters.json"
        data_dir.mkdir(parents=True, exist_ok=True)
        params_path.write_text(json.dumps({"monitored_keywords": ["medicaid"], "vendors": []}), encoding="utf-8")

        cache_only_callers = run_cache_only_checks(data_dir, cache_dir, ledger_path, params_path)
        run_simulated_429_check(tmp, params_path)

    print(
        "ok: cache-only made no urllib calls; simulated 429 preserved CSV; "
        "blocked ledger callers=" + ",".join(sorted(cache_only_callers))
    )
    return 0


def run_cache_only_checks(data_dir: Path, cache_dir: Path, ledger_path: Path, params_path: Path) -> set[str]:
    calls: list[str] = []
    original_urlopen = urllib.request.urlopen

    def blocked_urlopen(request: Any, *args: Any, **kwargs: Any) -> Any:
        calls.append("blocked")
        raise AssertionError("urlopen must not be called in cache-only SAM checks")

    urllib.request.urlopen = blocked_urlopen
    try:
        fetch_sam(
            SearchConfig(
                sources=["sam"],
                keywords=["medicaid"],
                max_per_source=1,
                data_dir=data_dir,
                sam_quota_mode="cache-only",
                sam_live_budget=0,
                sam_cache_dir=cache_dir,
                sam_ledger_path=ledger_path,
            )
        )

        try:
            fetch_entities_by_name(
                SamEntityConfig(
                    max_results=1,
                    retry_count=0,
                    sam_quota_mode="cache-only",
                    sam_live_budget=0,
                    sam_cache_dir=cache_dir,
                    sam_ledger_path=ledger_path,
                ),
                "Gainwell",
            )
        except SamEntityError as exc:
            if not exc.blocked:
                raise

        fetch_sam_contract_awards(
            SAMContractAwardsConfig(
                params_path=params_path,
                env_file=data_dir / "missing.env",
                max_searches=2,
                max_per_search=1,
                page_limit=1,
                max_pages=1,
                sam_quota_mode="cache-only",
                sam_live_budget=0,
                sam_cache_dir=cache_dir,
                sam_ledger_path=ledger_path,
            )
        )
    finally:
        urllib.request.urlopen = original_urlopen

    if calls:
        raise AssertionError("cache-only path attempted urlopen")
    callers = blocked_callers(ledger_path)
    expected = {"sam_opportunities", "sam_entities", "sam_contract_awards"}
    missing = expected - callers
    if missing:
        raise AssertionError(f"missing blocked ledger callers: {sorted(missing)}")
    return callers & expected


def run_simulated_429_check(tmp: Path, params_path: Path) -> None:
    out_path = tmp / "sam_contract_awards.csv"
    env_path = tmp / "sam.env"
    cache_dir = tmp / "live-raw-sam"
    ledger_path = cache_dir / "call_ledger.ndjson"
    out_path.write_text("sentinel\n", encoding="utf-8")
    env_path.write_text(f"SAM_API_KEY={DUMMY_SAM_KEY}\n", encoding="utf-8")

    calls = 0
    original_urlopen = urllib.request.urlopen
    old_argv = sys.argv[:]
    old_key = os.environ.get("SAM_API_KEY")

    def fake_429(request: Any, *args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        raise urllib.error.HTTPError(
            "https://api.sam.gov/contract-awards/v1/search?api_key=REDACTED",
            429,
            "Too Many Requests",
            {},
            io.BytesIO(b'{"error":"quota"}'),
        )

    module = load_script_module(ROOT / "scripts" / "sam_contract_awards.py")
    urllib.request.urlopen = fake_429
    os.environ["SAM_API_KEY"] = DUMMY_SAM_KEY
    sys.argv = [
        "sam_contract_awards.py",
        "--params",
        str(params_path),
        "--out",
        str(out_path),
        "--env-file",
        str(env_path),
        "--max-searches",
        "2",
        "--max-per-search",
        "1",
        "--page-limit",
        "1",
        "--sam-quota-mode",
        "live",
        "--sam-live-budget",
        "1",
        "--sam-cache-dir",
        str(cache_dir),
        "--sam-ledger-path",
        str(ledger_path),
    ]
    try:
        rc = module.main()
    finally:
        urllib.request.urlopen = original_urlopen
        sys.argv = old_argv
        if old_key is None:
            os.environ.pop("SAM_API_KEY", None)
        else:
            os.environ["SAM_API_KEY"] = old_key

    if rc != 1:
        raise AssertionError(f"simulated 429 returned {rc}, expected 1")
    if calls != 1:
        raise AssertionError(f"simulated 429 made {calls} request attempts, expected 1")
    if out_path.read_text(encoding="utf-8") != "sentinel\n":
        raise AssertionError("simulated 429 changed existing CSV")


def load_script_module(path: Path) -> Any:
    spec = importlib.util.spec_from_file_location("sam_contract_awards_script", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load sam_contract_awards.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def blocked_callers(path: Path) -> set[str]:
    callers: set[str] = set()
    if not path.exists():
        return callers
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("status") == "blocked":
            callers.add(str(row.get("caller") or ""))
    return callers


if __name__ == "__main__":
    raise SystemExit(main())
