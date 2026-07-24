from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path


LIST_FIELDS = {"program_focus", "keywords_matched", "risks"}
EXPIRATION_SIGNALS = {"Expiring soon", "Recompete watch"}
FEDERAL_RECORD_CATEGORIES = {
    "Federal Register API": ("policy_regulatory", "Policy & regulatory"),
    "data.medicaid.gov Catalog API": ("medicaid_data", "Medicaid data"),
    "CMS Provider Data Catalog API": ("provider_data", "Provider data"),
}


class CsvStore:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.opportunities_path = data_dir / "opportunities.csv"
        self.state_opportunities_path = data_dir / "state_opportunities.csv"
        self.contracts_path = data_dir / "contracts.csv"
        self.state_contracts_path = data_dir / "state_contracts.csv"
        self.sources_path = data_dir / "sources.csv"
        self.scoring_rules_path = data_dir / "scoring_rules.csv"
        self.status_history_path = data_dir / "status_history.csv"

    def list_opportunities(self) -> list[dict]:
        records = [
            self._normalize_opportunity(row)
            for row in self._read_rows(self.opportunities_path)
            if not self._is_federal_record(row)
        ]
        records.extend(
            self._normalize_state_opportunity(row)
            for row in self._read_rows(self.state_opportunities_path)
        )
        records.extend(
            self._normalize_contract(row, is_state=False)
            for row in self._read_rows(self.contracts_path)
        )
        records.extend(
            self._normalize_contract(row, is_state=True)
            for row in self._read_rows(self.state_contracts_path)
        )
        return sorted(
            records,
            key=lambda row: (row.get("fit_score", 0), row.get("due_date", "")),
            reverse=True,
        )

    def list_federal_records(self) -> list[dict]:
        records = [
            self._normalize_federal_record(row)
            for row in self._read_rows(self.opportunities_path)
            if self._is_federal_record(row)
        ]
        return sorted(
            records,
            key=lambda row: (row.get("posted_date", ""), row.get("fit_score", 0)),
            reverse=True,
        )

    def get_opportunity(self, opportunity_id: str) -> dict | None:
        for opportunity in self.list_opportunities():
            if opportunity["id"] == opportunity_id:
                opportunity["status_history"] = self.get_status_history(opportunity_id)
                return opportunity
        return None

    def get_federal_record(self, record_id: str) -> dict | None:
        for record in self.list_federal_records():
            if record["id"] == record_id:
                return record
        return None

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

    def update_status(self, opportunity_id: str, new_status: str, note: str = "", changed_by: str = "Local User") -> dict | None:
        rows = self._read_rows(self.opportunities_path)
        fieldnames = self._fieldnames(self.opportunities_path)
        updated_row = None
        old_status = ""

        for row in rows:
            if row.get("id") == opportunity_id:
                old_status = row.get("status", "")
                row["status"] = new_status
                row["last_updated_at"] = self._now()
                updated_row = row
                break

        if updated_row is None:
            return None

        self._write_rows(self.opportunities_path, fieldnames, rows)
        self.append_status_history(opportunity_id, old_status, new_status, changed_by, note)
        return self.get_opportunity(opportunity_id)

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

    def _normalize_opportunity(self, row: dict) -> dict:
        normalized = dict(row)
        normalized["fit_score"] = self._to_int(row.get("fit_score"), 0)
        normalized["budget_estimate"] = self._to_int(row.get("budget_estimate"), 0)
        for field in LIST_FIELDS:
            normalized[field] = self._split_list(row.get(field, ""))
        normalized["reviewable"] = True
        normalized["categories"] = self._opportunity_categories(normalized)
        normalized["category_label"] = self._category_label(normalized["categories"])
        return normalized

    def _normalize_state_opportunity(self, row: dict) -> dict:
        keywords = self._split_list(row.get("matched_keywords", ""))
        normalized = {
            "id": f"state-opportunity-{row.get('id', '')}",
            "title": row.get("title", "Untitled state opportunity"),
            "state": row.get("state", ""),
            "agency": row.get("agency", ""),
            "source": row.get("source", ""),
            "source_url": row.get("source_url", ""),
            "document_url": row.get("document_url", ""),
            "document_type": row.get("document_type", "State opportunity"),
            "due_date": row.get("due_date", ""),
            "posted_date": row.get("posted_date", ""),
            "last_checked_at": row.get("last_checked_at", ""),
            "last_updated_at": row.get("last_checked_at", ""),
            "budget_estimate": self._to_int(row.get("amount"), 0),
            "eligibility": "Review Needed",
            "eligibility_reason": "Verify eligibility in the official state procurement notice.",
            "fit_score": self._to_int(row.get("relevance_score"), 0),
            "ai_recommendation": "Review",
            "status": "Unreviewed",
            "program_focus": keywords,
            "keywords_matched": keywords,
            "risks": [],
            "summary": row.get("description") or (
                f"{row.get('document_type', 'Opportunity')} from {row.get('source', 'a state source')}."
            ),
            "reviewable": False,
            "categories": ["state_opportunities"],
            "category_label": "State opportunity",
        }
        if self._looks_like_grant(normalized):
            normalized["categories"].append("grants")
            normalized["category_label"] = "Grant"
        return normalized

    def _normalize_federal_record(self, row: dict) -> dict:
        normalized = self._normalize_opportunity(row)
        category, label = FEDERAL_RECORD_CATEGORIES[row.get("source", "")]
        normalized["record_category"] = category
        normalized["record_category_label"] = label
        normalized["reviewable"] = False
        normalized["categories"] = []
        normalized["category_label"] = label
        return normalized

    def _normalize_contract(self, row: dict, *, is_state: bool) -> dict:
        keywords = self._split_list(row.get("matched_keywords", ""))
        signal = row.get("recompete_signal", "")
        vendor = row.get("vendor_name") or row.get("recipient_name") or "Unknown vendor"
        agency = row.get("agency") or ", ".join(
            part
            for part in [row.get("awarding_agency", ""), row.get("awarding_sub_agency", "")]
            if part
        )
        title = row.get("title") or row.get("description") or row.get("award_id") or "Contract signal"
        categories = ["competitor_signals"]
        if signal in EXPIRATION_SIGNALS:
            categories.append("contract_expirations")
        return {
            "id": f"{'state' if is_state else 'federal'}-contract-{row.get('id', '')}",
            "title": f"{vendor}: {title}",
            "state": row.get("state", "") if is_state else "Federal",
            "agency": agency,
            "source": row.get("source") or "USAspending.gov Awards API",
            "source_url": row.get("source_url", ""),
            "document_url": row.get("document_url") or row.get("source_url", ""),
            "document_type": row.get("document_type") or "Contract award",
            "due_date": row.get("end_date", ""),
            "posted_date": row.get("start_date") or row.get("execution_date", ""),
            "last_checked_at": row.get("last_checked_at", ""),
            "last_updated_at": row.get("last_checked_at", ""),
            "budget_estimate": self._to_int(row.get("amount") or row.get("award_amount"), 0),
            "eligibility": "Market intelligence",
            "eligibility_reason": f"Competitor award monitored for recompete activity: {signal or 'timing unknown'}.",
            "fit_score": self._to_int(row.get("relevance_score"), 0),
            "ai_recommendation": "Monitor",
            "status": "Monitor",
            "program_focus": keywords,
            "keywords_matched": keywords,
            "risks": [signal] if signal else [],
            "summary": " | ".join(
                part
                for part in [
                    f"Vendor: {vendor}",
                    f"Contract: {row.get('contract_number') or row.get('award_id', '')}",
                    f"Recompete: {signal}",
                    row.get("description", ""),
                ]
                if part and not part.endswith(": ")
            ),
            "reviewable": False,
            "categories": categories,
            "category_label": "Contract expiration" if "contract_expirations" in categories else "Competitor signal",
        }

    def _opportunity_categories(self, opportunity: dict) -> list[str]:
        categories: list[str] = []
        if self._looks_like_grant(opportunity):
            categories.append("grants")
        if self._looks_like_contract_signal(opportunity):
            categories.append("competitor_signals")
            if self._looks_like_expiration(opportunity):
                categories.append("contract_expirations")
        elif str(opportunity.get("state", "")).strip().lower() == "federal":
            categories.append("federal_opportunities")
        else:
            categories.append("state_opportunities")
        return categories

    def _looks_like_grant(self, opportunity: dict) -> bool:
        text = " ".join(
            str(opportunity.get(field, ""))
            for field in ("source", "document_type", "title")
        ).lower()
        return "grant" in text or "funding opportunity" in text

    def _looks_like_contract_signal(self, opportunity: dict) -> bool:
        text = " ".join(
            str(opportunity.get(field, ""))
            for field in ("source", "document_type")
        ).lower()
        return "usaspending" in text or "recompete" in text or "contract award" in text

    def _looks_like_expiration(self, opportunity: dict) -> bool:
        text = " ".join(
            str(opportunity.get(field, ""))
            for field in ("document_type", "summary")
        ).lower()
        return "recompete signal" in text or "expiring soon" in text or "recompete watch" in text

    def _category_label(self, categories: list[str]) -> str:
        if "contract_expirations" in categories:
            return "Contract expiration"
        if "competitor_signals" in categories:
            return "Competitor signal"
        if "grants" in categories:
            return "Grant"
        if "federal_opportunities" in categories:
            return "Federal opportunity"
        return "State opportunity"

    def _is_federal_record(self, row: dict) -> bool:
        return row.get("source", "") in FEDERAL_RECORD_CATEGORIES

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
            return int(float(value or default))
        except ValueError:
            return default

    def _now(self) -> str:
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
