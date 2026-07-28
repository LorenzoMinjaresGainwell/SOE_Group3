from __future__ import annotations

import copy
import csv
import math
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from services.competitor_intelligence import CompetitorIntelligence, load_profiles
from services.priority_scoring import PriorityScorer


LIST_FIELDS = {"program_focus", "topic_keys", "keywords_matched", "risks"}
UPDATE_EXCLUDED_TYPES = {"opportunity", "grant", "award", "contract"}
EXPIRATION_SIGNALS = {"expired", "near_expiry", "Expiring soon", "Recompete watch"}


class CsvStore:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.state_opportunities_path = data_dir / "state_opportunities.csv"
        self.federal_opportunities_path = data_dir / "federal_opportunities.csv"
        self.federal_grants_path = data_dir / "federal_grants.csv"
        self.state_contracts_path = data_dir / "state_contracts.csv"
        self.federal_contracts_path = data_dir / "federal_contract_lifecycle.csv"
        self.state_updates_path = data_dir / "state_policy_updates.csv"
        self.federal_updates_path = data_dir / "federal_updates_catalog.csv"
        self.sources_path = data_dir / "sources.csv"
        self.scoring_rules_path = data_dir / "scoring_rules.csv"
        self.status_history_path = data_dir / "status_history.csv"
        self.user_state_path = data_dir / "opportunity_user_state.csv"
        default_config_dir = Path(__file__).resolve().parents[1] / "data"
        scoring_config_dir = data_dir if any(
            (data_dir / name).exists() for name in ("capability_rules.csv", "strategic_jurisdictions.csv")
        ) else default_config_dir
        competitor_config = data_dir / "competitor_aliases.csv"
        if not competitor_config.exists():
            competitor_config = default_config_dir / "competitor_aliases.csv"
        self.priority_scorer = PriorityScorer(scoring_config_dir)
        self.competitor_intelligence = CompetitorIntelligence(load_profiles(competitor_config))
        self._family_cache: tuple[tuple, dict[str, list[dict]]] | None = None

    def list_opportunities(self, *, today: date | None = None) -> list[dict]:
        today = today or datetime.now(timezone.utc).date()
        records = [
            *(self._normalize_state_opportunity(row) for row in self._read_rows(self.state_opportunities_path)),
            *(self._normalize_federal_opportunity(row) for row in self._read_rows(self.federal_opportunities_path)),
            *(self._normalize_federal_grant(row) for row in self._read_rows(self.federal_grants_path)),
        ]
        user_states = {row.get("opportunity_id", ""): row for row in self._read_rows(self.user_state_path)}
        for record in records:
            user_state = user_states.get(record["id"], {})
            if user_state.get("status"):
                record["status"] = user_state["status"]
            record["pinned"] = self._to_bool(user_state.get("pinned"))
        records = self._score_records(self._dedupe(records), today)
        return sorted(
            records,
            key=lambda row: (
                row["pinned"], row.get("amount", 0), row["importance_score"], row.get("due_date", "")
            ),
            reverse=True,
        )

    def list_contracts(self, *, today: date | None = None) -> list[dict]:
        today = today or datetime.now(timezone.utc).date()
        state_opportunities = self._read_rows(self.state_opportunities_path)
        federal_opportunities = self._read_rows(self.federal_opportunities_path)
        opportunity_rows = [*state_opportunities, *federal_opportunities]
        state_contracts = self._read_rows(self.state_contracts_path)
        federal_contracts = self._read_rows(self.federal_contracts_path)
        records = [
            *(
                self._normalize_state_contract(row, today)
                for row in state_contracts
                if not self._is_represented_opportunity_contract(row, opportunity_rows)
            ),
            *(
                self._normalize_federal_contract(row, today)
                for row in federal_contracts
                if not self._is_represented_opportunity_contract(row, opportunity_rows)
            ),
        ]
        return sorted(
            self._score_records(self._dedupe(records), today),
            key=lambda row: (row["importance_score"], row.get("end_date", "")),
            reverse=True,
        )

    def list_updates(self, *, today: date | None = None) -> list[dict]:
        today = today or datetime.now(timezone.utc).date()
        records = [
            *(self._normalize_state_update(row) for row in self._read_rows(self.state_updates_path)),
            *(
                self._normalize_federal_update(row)
                for row in self._read_rows(self.federal_updates_path)
                if self._is_relevant_federal_update(row)
            ),
        ]
        return sorted(
            self._score_records(self._dedupe(records), today),
            key=lambda row: (row["importance_score"], row.get("updated_date") or row.get("posted_date", "")),
            reverse=True,
        )

    def list_federal_records(self, *, today: date | None = None) -> list[dict]:
        """Compatibility explorer backed only by relevant federal update catalog rows."""
        return [record for record in self.list_updates(today=today) if record["scope"] == "federal"]

    def get_opportunity(self, opportunity_id: str, *, today: date | None = None) -> dict | None:
        opportunity = self._find(self.list_opportunities(today=today), opportunity_id)
        if opportunity:
            opportunity["status_history"] = self.get_status_history(opportunity_id)
        return opportunity

    def get_contract(self, contract_id: str, *, today: date | None = None) -> dict | None:
        return self._find(self.list_contracts(today=today), contract_id)

    def get_update(self, update_id: str, *, today: date | None = None) -> dict | None:
        return self._find(self.list_updates(today=today), update_id)

    def get_federal_record(self, record_id: str, *, today: date | None = None) -> dict | None:
        return self._find(self.list_federal_records(today=today), record_id)

    def rht_overview(self, *, today: date | None = None, limit: int = 20) -> dict:
        today = today or datetime.now(timezone.utc).date()
        limit = max(1, min(limit, 50))
        families = self._all_family_records(today)
        signals = [record for records in families.values() for record in records if record["rht_strength"] != "none"]
        jurisdictions: dict[str, dict] = {}
        for record in signals:
            jurisdiction = record.get("state") or "Unknown"
            bucket = jurisdictions.setdefault(jurisdiction, {"count": 0, "record_ids": []})
            bucket["count"] += 1
            if len(bucket["record_ids"]) < limit:
                bucket["record_ids"].append(record["id"])
        top_by_family = {
            family: sorted(
                (record for record in records if record["rht_strength"] != "none"),
                key=lambda row: (row["priority_score"], row.get("updated_date") or row.get("posted_date", "")),
                reverse=True,
            )[:limit]
            for family, records in families.items()
        }
        return {
            "as_of": today.isoformat(),
            "counts": {
                "all_records": sum(len(records) for records in families.values()),
                "rht_records": len(signals),
                "by_family": {
                    family: {
                        "all": len(records),
                        "rht": sum(record["rht_strength"] != "none" for record in records),
                    }
                    for family, records in families.items()
                },
                "by_strength": {
                    strength: sum(record["rht_strength"] == strength for record in signals)
                    for strength in ("explicit", "direct", "related", "generic")
                },
            },
            "jurisdictions": dict(sorted(jurisdictions.items(), key=lambda item: (-item[1]["count"], item[0]))),
            "top_records": [
                self._record_reference(record)
                for family in ("opportunities", "contracts", "updates")
                for record in top_by_family[family]
            ],
            "top_record_limit": limit,
            "top_record_limit_scope": "per_family",
        }

    def competitor_profiles(
        self, *, today: date | None = None, query: str = "", limit: int = 20
    ) -> dict:
        today = today or datetime.now(timezone.utc).date()
        limit = max(1, min(limit, 50))
        records = [record for rows in self._all_family_records(today).values() for record in rows]
        matched = self.competitor_intelligence.search_records(records)
        profiles = []
        for profile in self.competitor_intelligence.profiles:
            organization_records = [
                record for record in matched
                if any(mention["organization_key"] == profile.key for mention in record["organization_mentions"])
            ]
            summary = self.competitor_intelligence.summarize(organization_records, as_of=today)
            active_count = summary["record_count"] - summary["end_windows"]["expired"]
            top = sorted(organization_records, key=lambda row: row.get("priority_score", 0), reverse=True)
            profiles.append({
                "organization_key": profile.key,
                "organization_name": profile.canonical_name,
                "organization_type": profile.organization_type,
                "recommended_action": "Retain" if profile.organization_type == "gainwell" else "Compete",
                "summary": {
                    "record_count": summary["record_count"],
                    "active_count": active_count,
                    "total_value": summary["total_value"],
                    "end_windows": summary["end_windows"],
                    "jurisdictions": summary["by_jurisdiction"],
                },
                "top_records": [self._record_reference(record) for record in top[:limit]],
            })
        search_matches = self.competitor_intelligence.custom_search(records, query) if query.strip() else []
        return {
            "as_of": today.isoformat(),
            "profiles": profiles,
            "search": {
                "query": query,
                "count": len(search_matches),
                "records": [self._record_reference(record) for record in search_matches[:limit]],
                "result_limit": limit,
            },
        }

    def _all_family_records(self, today: date) -> dict[str, list[dict]]:
        paths = (
            self.state_opportunities_path, self.federal_opportunities_path, self.federal_grants_path,
            self.state_contracts_path, self.federal_contracts_path, self.state_updates_path,
            self.federal_updates_path, self.user_state_path,
        )
        cache_key = (today.isoformat(), tuple(
            (str(path), path.stat().st_mtime_ns, path.stat().st_size) if path.exists() else (str(path), None, None)
            for path in paths
        ))
        if self._family_cache and self._family_cache[0] == cache_key:
            return copy.deepcopy(self._family_cache[1])
        families = {
            "opportunities": self.list_opportunities(today=today),
            "contracts": self.list_contracts(today=today),
            "updates": self.list_updates(today=today),
        }
        self._family_cache = (cache_key, copy.deepcopy(families))
        return families

    def _record_reference(self, record: dict) -> dict:
        return {
            "id": record.get("id", ""),
            "source_record_id": record.get("source_record_id", ""),
            "family": record.get("family", ""),
            "title": record.get("title", ""),
            "state": record.get("state", ""),
            "agency": record.get("agency", ""),
            "priority_score": record.get("priority_score", 0),
            "priority_label": record.get("priority_label", "Low"),
            "recommended_action": record.get("recommended_action", ""),
            "rht_strength": record.get("rht_strength", "none"),
        }

    def list_sources(self) -> list[dict]:
        return [self._normalize_source(row) for row in self._read_rows(self.sources_path)]

    def list_scoring_rules(self) -> list[dict]:
        rows = self._read_rows(self.scoring_rules_path)
        for row in rows:
            row["weight"] = self._to_int(row.get("weight"), 0)
        return rows

    def get_status_history(self, opportunity_id: str) -> list[dict]:
        return [
            self._normalize_history(row)
            for row in self._read_rows(self.status_history_path)
            if row.get("opportunity_id") == opportunity_id
        ]

    def update_status(
        self, opportunity_id: str, new_status: str, note: str = "", changed_by: str = "Local User",
        *, today: date | None = None,
    ) -> dict | None:
        today = today or datetime.now(timezone.utc).date()
        opportunity = self.get_opportunity(opportunity_id, today=today)
        if opportunity is None:
            return None

        old_status = opportunity.get("status", "")
        self._update_user_state(opportunity_id, status=new_status)
        if old_status != new_status:
            self.append_status_history(opportunity_id, old_status, new_status, changed_by, note)
        return self.get_opportunity(opportunity_id, today=today)

    def update_pinned(self, opportunity_id: str, pinned: bool, *, today: date | None = None) -> dict | None:
        today = today or datetime.now(timezone.utc).date()
        if self.get_opportunity(opportunity_id, today=today) is None:
            return None
        self._update_user_state(opportunity_id, pinned=pinned)
        return self.get_opportunity(opportunity_id, today=today)

    def _update_user_state(
        self,
        opportunity_id: str,
        *,
        status: str | None = None,
        pinned: bool | None = None,
    ) -> None:
        fieldnames = ["opportunity_id", "status", "pinned", "updated_at"]
        rows = self._read_rows(self.user_state_path)
        user_state = next(
            (row for row in rows if row.get("opportunity_id") == opportunity_id),
            None,
        )
        if user_state is None:
            user_state = {
                "opportunity_id": opportunity_id,
                "status": "",
                "pinned": "false",
                "updated_at": "",
            }
            rows.append(user_state)
        if status is not None:
            user_state["status"] = status
        if pinned is not None:
            user_state["pinned"] = "true" if pinned else "false"
        user_state["updated_at"] = self._now()
        self._write_rows(self.user_state_path, fieldnames, rows)

    def append_status_history(self, opportunity_id: str, from_status: str, to_status: str, changed_by: str, note: str) -> None:
        fieldnames = ["opportunity_id", "from_status", "to_status", "changed_at", "changed_by", "note"]
        path_exists = self.status_history_path.exists()
        with self.status_history_path.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            if not path_exists or self.status_history_path.stat().st_size == 0:
                writer.writeheader()
            writer.writerow(
                {
                    "opportunity_id": opportunity_id,
                    "from_status": from_status,
                    "to_status": to_status,
                    "changed_at": self._now(),
                    "changed_by": changed_by,
                    "note": note or "Status changed in local dashboard.",
                }
            )

    def _read_rows(self, path: Path) -> list[dict]:
        if not path.exists():
            return []
        with path.open(newline="", encoding="utf-8") as handle:
            return list(csv.DictReader(handle))

    def _write_rows(self, path: Path, fieldnames: list[str], rows: list[dict]) -> None:
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

    def _fieldnames(self, path: Path) -> list[str]:
        with path.open(newline="", encoding="utf-8") as handle:
            reader = csv.reader(handle)
            return next(reader)

    def _envelope(
        self,
        row: dict,
        *,
        record_id: str,
        family: str,
        scope: str,
        title: str,
        source: str,
        record_type: str,
        importance: str | None,
    ) -> dict:
        return {
            "id": record_id,
            "family": family,
            "scope": scope,
            "title": title or f"Untitled {family[:-1]}",
            "state": row.get("state", "") if scope == "state" else "Federal",
            "agency": row.get("agency", ""),
            "source": source,
            "source_url": row.get("source_url", ""),
            "document_url": row.get("document_url", ""),
            "record_type": record_type,
            "document_type": record_type,
            "summary": row.get("summary", ""),
            "posted_date": row.get("posted_date", ""),
            "updated_date": row.get("updated_date", ""),
            "due_date": row.get("due_date", ""),
            "effective_date": row.get("effective_date", ""),
            "last_checked_at": row.get("last_checked_at", ""),
            "last_updated_at": row.get("updated_date") or row.get("last_checked_at", ""),
            "importance_score": self._to_int(importance, 0),
            "program_focus": self._split_list(row.get("program_focus", "")),
            "topic_keys": self._split_list(row.get("topic_keys", "")),
        }

    def _normalize_state_opportunity(self, row: dict) -> dict:
        record = self._envelope(
            row,
            record_id=f"state-opportunity-{row.get('id', '')}",
            family="opportunities",
            scope="state",
            title=row.get("title", ""),
            source=row.get("source", ""),
            record_type=row.get("document_type") or "opportunity",
            importance=row.get("relevance_score"),
        )
        keywords = self._split_list(row.get("matched_keywords", ""))
        amount = self._to_int(row.get("amount"), 0)
        record.update({
            "opportunity_type": "state_procurement",
            "source_record_id": row.get("source_record_id", ""),
            "solicitation_number": row.get("source_record_id", ""),
            "status": "Unreviewed",
            "source_status": row.get("status", ""),
            "lifecycle_status": row.get("status") or "unknown",
            "amount": amount,
            "budget_estimate": amount,
            "fit_score": record["importance_score"],
            "eligibility": "Review Needed",
            "eligibility_reason": "Verify eligibility in the official state procurement notice.",
            "keywords_matched": keywords,
            "risks": [],
            "reviewable": True,
            "pinned": False,
            "categories": ["state_opportunities"],
            "category_label": "State opportunity",
        })
        return record

    def _normalize_federal_opportunity(self, row: dict) -> dict:
        record = self._envelope(
            row,
            record_id=row.get("opportunity_id", ""),
            family="opportunities",
            scope="federal",
            title=row.get("title", ""),
            source=row.get("source_key") or "sam_opportunities",
            record_type=row.get("record_type") or row.get("notice_type") or "opportunity",
            importance=row.get("importance_score"),
        )
        amount = self._to_int(row.get("award_amount"), 0)
        record.update({
            "opportunity_type": "federal_procurement",
            "source_record_id": row.get("sam_notice_id", ""),
            "solicitation_number": row.get("solicitation_number", ""),
            "notice_type": row.get("notice_type", ""),
            "status": "Unreviewed",
            "lifecycle_status": row.get("lifecycle_status") or "unknown",
            "amount": amount,
            "budget_estimate": amount,
            "fit_score": record["importance_score"],
            "eligibility": "Review Needed",
            "eligibility_reason": "Verify eligibility and set-aside terms in SAM.gov.",
            "set_aside": row.get("set_aside", ""),
            "keywords_matched": record["topic_keys"],
            "risks": [],
            "reviewable": True,
            "pinned": False,
            "categories": ["federal_opportunities"],
            "category_label": "Federal opportunity",
        })
        return record

    def _normalize_federal_grant(self, row: dict) -> dict:
        grant_row = dict(row)
        grant_row["due_date"] = row.get("close_date", "")
        record = self._envelope(
            grant_row,
            record_id=row.get("grant_id", ""),
            family="opportunities",
            scope="federal",
            title=row.get("opportunity_title", ""),
            source="grants",
            record_type="grant",
            importance=row.get("importance_score"),
        )
        amount = self._to_int(row.get("estimated_total_program_funding") or row.get("award_ceiling"), 0)
        record.update({
            "opportunity_type": "grant",
            "source_record_id": row.get("opportunity_number", ""),
            "solicitation_number": row.get("opportunity_number", ""),
            "status": "Unreviewed",
            "lifecycle_status": "unknown",
            "amount": amount,
            "budget_estimate": amount,
            "fit_score": record["importance_score"],
            "award_ceiling": self._to_int(row.get("award_ceiling"), 0),
            "award_floor": self._to_int(row.get("award_floor"), 0),
            "expected_awards": self._to_int(row.get("expected_awards"), 0),
            "eligibility": row.get("eligibility", ""),
            "eligibility_reason": row.get("eligibility", ""),
            "keywords_matched": record["topic_keys"],
            "risks": [],
            "reviewable": True,
            "pinned": False,
            "categories": ["grants", "federal_opportunities"],
            "category_label": "Grant",
        })
        return record

    def _normalize_state_contract(self, row: dict, today: date) -> dict:
        record = self._envelope(
            row,
            record_id=f"state-contract-{row.get('id', '')}",
            family="contracts",
            scope="state",
            title=row.get("title", ""),
            source=row.get("source", ""),
            record_type=row.get("contract_record_type") or row.get("document_type") or "contract",
            importance=row.get("relevance_score"),
        )
        return self._contract_fields(record, row, row.get("recompete_signal") or "unknown", today)

    def _normalize_federal_contract(self, row: dict, today: date) -> dict:
        contract_row = dict(row)
        source_urls = self._valid_urls(row.get("source_urls", ""))
        contract_row["source_url"] = source_urls[0] if source_urls else ""
        contract_row["document_url"] = source_urls[0] if source_urls else ""
        record = self._envelope(
            contract_row,
            record_id=row.get("contract_id", ""),
            family="contracts",
            scope="federal",
            title=row.get("title", ""),
            source=row.get("source_keys") or "federal_contract_lifecycle",
            record_type=row.get("contract_vehicle") or "contract",
            importance=row.get("importance_score"),
        )
        record["source_urls"] = source_urls
        return self._contract_fields(record, row, row.get("lifecycle_status") or "unknown", today)

    def _contract_fields(self, record: dict, row: dict, lifecycle_status: str, today: date) -> dict:
        days = self._optional_int(row.get("days_until_end"))
        signal = row.get("recompete_signal", "")
        expired = self._is_expired_contract(row, lifecycle_status, signal, days, today)
        record.update({
            "vendor_name": row.get("vendor_name", ""),
            "contract_number": row.get("contract_number") or row.get("piid", ""),
            "lifecycle_status": "expired" if expired else lifecycle_status,
            "status": "expired" if expired else lifecycle_status,
            "start_date": row.get("start_date") or row.get("period_start_date", ""),
            "end_date": row.get("end_date") or row.get("period_end_date", ""),
            "potential_end_date": row.get("potential_end_date", ""),
            "days_until_end": days,
            "months_to_end": self._optional_int(row.get("months_to_end")),
            "expired": expired,
            "recompete_signal": signal,
            "recompete_window_start": row.get("recompete_window_start", ""),
            "amount": self._to_int(row.get("amount") or row.get("award_amount"), 0),
            "current_total_value": self._to_int(row.get("current_total_value"), 0),
            "potential_total_value": self._to_int(row.get("potential_total_value"), 0),
            "competitor": self._to_bool(row.get("competitor_flag")),
            "competitor_flag": self._to_bool(row.get("competitor_flag")),
            "gwt_relation": row.get("gwt_relation", ""),
            "known_bid_status": row.get("known_bid_status", ""),
            "matched_keywords": self._split_list(row.get("matched_keywords", "")),
        })
        return record

    def _normalize_state_update(self, row: dict) -> dict:
        record = self._envelope(
            row,
            record_id=f"state-update-{row.get('id', '')}",
            family="updates",
            scope="state",
            title=row.get("title", ""),
            source=row.get("source", ""),
            record_type=row.get("record_type") or "policy_update",
            importance=row.get("importance_score"),
        )
        return self._update_fields(record, row)

    def _normalize_federal_update(self, row: dict) -> dict:
        record = self._envelope(
            row,
            record_id=row.get("update_id", ""),
            family="updates",
            scope="federal",
            title=row.get("title", ""),
            source=row.get("source_key", ""),
            record_type=row.get("record_type") or "update",
            importance=row.get("importance_score"),
        )
        category, label = {
            "federal_register": ("policy_regulatory", "Policy & regulatory"),
            "regulations": ("policy_regulatory", "Policy & regulatory"),
            "medicaid_data": ("medicaid_data", "Medicaid data"),
            "cms_data": ("cms_data", "CMS data"),
        }.get(row.get("source_key", ""), ("federal_update", "Federal update"))
        record.update({
            "update_id": row.get("update_id", ""),
            "source_key": row.get("source_key", ""),
            "source_record_id": row.get("source_record_id", ""),
            "score_evidence_json": row.get("score_evidence_json", ""),
            "record_category": category,
            "record_category_label": label,
            "fit_score": record["importance_score"],
            "reviewable": False,
        })
        return self._update_fields(record, row)

    def _update_fields(self, record: dict, row: dict) -> dict:
        record.update({
            "update_type": record["record_type"],
            "comment_required_flag": self._to_bool(row.get("comment_required_flag")),
            "action_required_by": row.get("action_required_by", ""),
            "rht_flag": self._to_bool(row.get("rht_flag")),
            "docket_id": row.get("docket_id", ""),
            "regulation_id": row.get("regulation_id", ""),
            "matched_keywords": self._split_list(row.get("matched_keywords", "")) or record["topic_keys"],
            "predictive_value_usd": self._to_int(row.get("predictive_value_usd"), 0),
        })
        return record

    def _is_relevant_federal_update(self, row: dict) -> bool:
        record_type = row.get("record_type", "").strip().lower()
        is_constituent_record = (
            record_type in UPDATE_EXCLUDED_TYPES
            or record_type in {"contract_award", "solicitation", "funding_opportunity"}
            or any(row.get(field, "").strip() for field in ("opportunity_id", "grant_id", "contract_id"))
        )
        relevance = row.get("importance_score") or row.get("relevance_score")
        return not is_constituent_record and self._to_int(relevance, 0) > 0

    def _is_represented_opportunity_contract(self, row: dict, opportunity_rows: list[dict]) -> bool:
        contract_type = (row.get("contract_vehicle") or row.get("contract_record_type") or "").strip().lower()
        lifecycle = row.get("lifecycle_status", "").strip().lower()
        if contract_type not in {"opportunity_notice", "award_notice"} and lifecycle != "opportunity":
            return False

        contract_ids = self._identity_values(
            row.get("source_record_ids", ""), row.get("source_record_id", ""),
            row.get("solicitation_number", ""), row.get("parent_id", ""),
        )
        contract_title = row.get("title", "").strip().casefold()
        contract_agency = row.get("agency", "").strip().casefold()
        for opportunity in opportunity_rows:
            opportunity_ids = self._identity_values(
                opportunity.get("id", ""), opportunity.get("opportunity_id", ""),
                opportunity.get("sam_notice_id", ""), opportunity.get("source_record_id", ""),
                opportunity.get("solicitation_number", ""),
            )
            if contract_ids & opportunity_ids:
                return True
            if (
                contract_title
                and contract_title == opportunity.get("title", "").strip().casefold()
                and contract_agency
                and contract_agency == opportunity.get("agency", "").strip().casefold()
            ):
                return True
        return False

    def _identity_values(self, *values: str) -> set[str]:
        identities: set[str] = set()
        for value in values:
            for item in self._split_list(value or ""):
                normalized = item.strip().casefold()
                if not normalized:
                    continue
                identities.add(normalized)
                for prefix in ("sam_opportunities-", "state-opportunity-"):
                    if normalized.startswith(prefix):
                        identities.add(normalized.removeprefix(prefix))
        return identities

    def _is_expired_contract(
        self, row: dict, lifecycle_status: str, signal: str, days_until_end: int | None, today: date
    ) -> bool:
        status_text = f"{lifecycle_status} {signal} {row.get('status', '')}".casefold()
        if "expired" in status_text or "past award" in status_text:
            return True
        if days_until_end is not None and days_until_end < 0:
            return True
        end_value = row.get("potential_end_date") or row.get("end_date") or row.get("period_end_date")
        try:
            return bool(end_value and date.fromisoformat(end_value[:10]) < today)
        except (TypeError, ValueError):
            return False

    def _valid_urls(self, value: str) -> list[str]:
        urls = []
        for item in self._split_list(value or ""):
            parsed = urlparse(item)
            if parsed.scheme in {"http", "https"} and parsed.netloc:
                urls.append(item)
        return urls

    def _normalize_source(self, row: dict) -> dict:
        normalized = dict(row)
        normalized["opportunities_found"] = self._to_int(row.get("opportunities_found"), 0)
        return normalized

    def _normalize_history(self, row: dict) -> dict:
        return {
            "from": row.get("from_status", ""),
            "to": row.get("to_status", ""),
            "changed_at": row.get("changed_at", ""),
            "changed_by": row.get("changed_by", ""),
            "note": row.get("note", ""),
        }

    def _split_list(self, value: str) -> list[str]:
        return [item.strip() for item in value.split(";") if item.strip()]

    def _to_int(self, value: str | None, default: int) -> int:
        try:
            number = float(value if value not in (None, "") else default)
            return int(number) if math.isfinite(number) else default
        except (TypeError, ValueError, OverflowError):
            return default

    def _to_bool(self, value: str | None) -> bool:
        return str(value or "").strip().lower() in {"1", "true", "yes", "on"}

    def _optional_int(self, value: str | None) -> int | None:
        if value is None or not str(value).strip():
            return None
        try:
            number = float(value)
            return int(number) if math.isfinite(number) else None
        except (TypeError, ValueError, OverflowError):
            return None

    def _score_records(self, records: list[dict], today: date) -> list[dict]:
        for record in records:
            result = self.priority_scorer.score(record, record["family"], model="B", today=today)
            record.update({
                "legacy_score": record.get("importance_score", 0),
                "priority_score": result["score"],
                "priority_label": self._priority_label(result["score"]),
                "confidence": result["confidence"],
                "recommended_action": result["action"],
                "score_breakdown": result["dimensions"],
                "scoring_model": "B",
                "rht_strength": result["rht_strength"],
                "scored_as_of": result["scored_as_of"],
            })
        return records

    def _priority_label(self, score: float) -> str:
        return "High" if score >= 70 else "Medium" if score >= 45 else "Low"

    def _dedupe(self, records: list[dict]) -> list[dict]:
        unique: dict[str, dict] = {}
        for record in records:
            record_id = record.get("id", "")
            if record_id and record_id not in unique:
                unique[record_id] = record
        return list(unique.values())

    def _find(self, records: list[dict], record_id: str) -> dict | None:
        return next((record for record in records if record.get("id") == record_id), None)

    def _now(self) -> str:
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
