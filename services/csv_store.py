from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path


LIST_FIELDS = {"program_focus", "keywords_matched", "risks"}


class CsvStore:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.opportunities_path = data_dir / "opportunities.csv"
        self.sources_path = data_dir / "sources.csv"
        self.scoring_rules_path = data_dir / "scoring_rules.csv"
        self.status_history_path = data_dir / "status_history.csv"

    def list_opportunities(self) -> list[dict]:
        return [self._normalize_opportunity(row) for row in self._read_rows(self.opportunities_path)]

    def get_opportunity(self, opportunity_id: str) -> dict | None:
        for opportunity in self.list_opportunities():
            if opportunity["id"] == opportunity_id:
                opportunity["status_history"] = self.get_status_history(opportunity_id)
                return opportunity
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
        return normalized

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
