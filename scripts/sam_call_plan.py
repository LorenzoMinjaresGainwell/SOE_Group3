#!/usr/bin/env python3
"""Print the SAM.gov first-day call plan without calling SAM.gov."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.sam_cache import redacted_request  # noqa: E402
from services.search_taxonomy import load_search_taxonomy  # noqa: E402

OPPORTUNITIES_URL = "https://api.sam.gov/opportunities/v2/search"
AWARDS_URL = "https://api.sam.gov/contract-awards/v1/search"
ENTITIES_URL = "https://api.sam.gov/entity-information/v4/entities"
MAX_FIRST_DAY_CALLS = 10

LOCAL_KEYWORDS = load_search_taxonomy().business_terms
LOCAL_AGENCIES = ["HHS", "CMS", "HRSA", "ACL", "AHRQ", "CDC"]


@dataclass(frozen=True)
class PlannedSAMCall:
    call_id: str
    endpoint: str
    method: str
    params: dict[str, str]
    purpose: str
    local_filter: str
    record_goal: str
    cache_key: str = ""

    def with_cache_key(self) -> "PlannedSAMCall":
        request = redacted_request(self.method, self.endpoint, self.params)
        values = asdict(self)
        values["params"] = {str(key): str(value) for key, value in request.params.items()}
        values["cache_key"] = request.cache_key
        return PlannedSAMCall(**values)


def build_plan(today: dt.date | None = None) -> list[PlannedSAMCall]:
    end = today or dt.datetime.now(dt.timezone.utc).date()
    posted_90 = end - dt.timedelta(days=90)
    posted_180 = end - dt.timedelta(days=180)
    awards_365 = end - dt.timedelta(days=365)
    expiring_end = end + dt.timedelta(days=730)

    calls = [
        opportunity_call("OPP-01", "o", posted_90, end, "Core solicitations; no title keyword loop"),
        opportunity_call("OPP-02", "k", posted_90, end, "Combined synopsis/solicitations; no title keyword loop"),
        opportunity_call("OPP-03", "r", posted_180, end, "Sources sought early signals; no title keyword loop"),
        opportunity_call("OPP-04", "p", posted_180, end, "Pre-solicitation early signals; no title keyword loop"),
        opportunity_call("OPP-05", "a", posted_180, end, "Award notices for incumbent/lifecycle hints; no title keyword loop"),
        PlannedSAMCall(
            call_id="AWD-01",
            endpoint=AWARDS_URL,
            method="GET",
            params={
                "api_key": "SAM_API_KEY",
                "contractingDepartmentCode": "7500",
                "dateSigned": f"{awards_365.isoformat()}:{end.isoformat()}",
                "includeSections": "contractId,coreData,awardDetails,awardeeData",
                "limit": "100",
                "offset": "0",
            },
            purpose="Broad HHS signed awards from last 365 days",
            local_filter=local_filter_text(),
            record_goal="Normalize awardee, amount, dates, agency, PSC/NAICS; discard non-health IT locally",
        ),
        PlannedSAMCall(
            call_id="AWD-02",
            endpoint=AWARDS_URL,
            method="GET",
            params={
                "api_key": "SAM_API_KEY",
                "contractingDepartmentCode": "7500",
                "currentCompletionDate": f"{end.isoformat()}:{expiring_end.isoformat()}",
                "includeSections": "contractId,coreData,awardDetails,awardeeData",
                "limit": "100",
                "offset": "0",
            },
            purpose="Broad HHS awards expiring in next 24 months",
            local_filter=local_filter_text(),
            record_goal="Feed recompete and near-expiry candidates without vendor alias loops",
        ),
        entity_call("ENT-01", "Gainwell", "Seed GWT entity IDs before alias expansion"),
        entity_call("ENT-02", "MAXIMUS", "Seed highest-priority competitor entity IDs"),
        entity_call("ENT-03", "Deloitte", "Seed highest-priority competitor entity IDs"),
    ]
    calls = [call.with_cache_key() for call in calls]
    if len(calls) > MAX_FIRST_DAY_CALLS:
        raise RuntimeError(f"plan has {len(calls)} calls; max is {MAX_FIRST_DAY_CALLS}")
    return calls


def opportunity_call(call_id: str, ptype: str, start: dt.date, end: dt.date, purpose: str) -> PlannedSAMCall:
    return PlannedSAMCall(
        call_id=call_id,
        endpoint=OPPORTUNITIES_URL,
        method="GET",
        params={
            "api_key": "SAM_API_KEY",
            "limit": "1000",
            "postedFrom": start.strftime("%m/%d/%Y"),
            "postedTo": end.strftime("%m/%d/%Y"),
            "ptype": ptype,
        },
        purpose=purpose,
        local_filter=local_filter_text(),
        record_goal="Keep only HHS/CMS/HRSA/ACL/AHRQ/CDC health IT and RHT records after raw cache write",
    )


def entity_call(call_id: str, legal_name: str, purpose: str) -> PlannedSAMCall:
    return PlannedSAMCall(
        call_id=call_id,
        endpoint=ENTITIES_URL,
        method="GET",
        params={
            "api_key": "SAM_API_KEY",
            "includeSections": "entityRegistration,coreData",
            "legalBusinessName": legal_name,
            "size": "10",
        },
        purpose=purpose,
        local_filter="manual confidence review; defer alias fan-out until cached UEI/CAGE seeds exist",
        record_goal="Capture UEI/CAGE/legal name/status once, then refresh by UEI only when budget exists",
    )


def local_filter_text() -> str:
    return "agency in " + ",".join(LOCAL_AGENCIES) + "; keyword in " + ",".join(LOCAL_KEYWORDS)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Print SAM quota-safe call plan; never calls api.sam.gov.")
    parser.add_argument("--date", default="", help="UTC plan date YYYY-MM-DD; defaults today")
    parser.add_argument("--format", choices=["markdown", "json", "ndjson"], default="markdown")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    today = dt.date.fromisoformat(args.date) if args.date else None
    calls = build_plan(today)
    if args.format == "json":
        print(json.dumps([asdict(call) for call in calls], indent=2))
    elif args.format == "ndjson":
        for call in calls:
            print(json.dumps(asdict(call), separators=(",", ":")))
    else:
        print_markdown(calls)
    return 0


def print_markdown(calls: list[PlannedSAMCall]) -> None:
    print("# SAM.gov First-Day Call Plan")
    print()
    print(f"Total planned live calls: {len(calls)} / {MAX_FIRST_DAY_CALLS}")
    print()
    print("| call_id | endpoint | params | purpose | local_filter | cache_key |")
    print("| --- | --- | --- | --- | --- | --- |")
    for call in calls:
        params = json.dumps(call.params, sort_keys=True, separators=(",", ":"))
        print(
            "| "
            + " | ".join(
                [
                    call.call_id,
                    endpoint_name(call.endpoint),
                    code(params),
                    escape_md(call.purpose),
                    escape_md(call.local_filter),
                    code(call.cache_key[:16]),
                ]
            )
            + " |"
        )


def endpoint_name(url: str) -> str:
    if "opportunities" in url:
        return "opportunities"
    if "contract-awards" in url:
        return "contract_awards"
    if "entity-information" in url:
        return "entities"
    return url


def code(value: str) -> str:
    return "`" + value.replace("`", "") + "`"


def escape_md(value: str) -> str:
    return value.replace("|", "\\|")


if __name__ == "__main__":
    raise SystemExit(main())
