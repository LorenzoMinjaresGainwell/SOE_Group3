from __future__ import annotations

import csv
from pathlib import Path

STATE_UPDATE_FIELDS = [
    "id",
    "state",
    "source",
    "source_record_id",
    "record_type",
    "title",
    "agency",
    "program_focus",
    "topic_keys",
    "posted_date",
    "updated_date",
    "due_date",
    "effective_date",
    "comment_required_flag",
    "action_required_by",
    "importance_score",
    "summary",
    "document_url",
    "source_url",
    "matched_keywords",
    "rht_flag",
    "raw_json",
    "last_checked_at",
]


def upsert_state_updates(path: Path, records: list[dict[str, str]]) -> tuple[int, int, int]:
    existing = read_csv(path)
    by_id = {row.get("id", ""): row for row in existing if row.get("id")}
    added = 0
    updated = 0

    for record in records:
        row = {field: record.get(field, "") for field in STATE_UPDATE_FIELDS}
        if not row["id"]:
            raise ValueError("state update record missing id")
        old = by_id.get(row["id"])
        if old is None:
            by_id[row["id"]] = row
            added += 1
            continue
        changed = old != row
        by_id[row["id"]] = row
        if changed:
            updated += 1

    rows = sorted(by_id.values(), key=sort_key, reverse=True)
    write_csv(path, STATE_UPDATE_FIELDS, rows)
    return added, updated, len(rows)


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def sort_key(row: dict[str, str]) -> tuple[int, str, str, str, str]:
    return (
        int_or_zero(row.get("importance_score")),
        row.get("posted_date") or row.get("updated_date") or row.get("effective_date") or "",
        row.get("due_date") or row.get("action_required_by") or "",
        row.get("state", ""),
        row.get("title", ""),
    )


def int_or_zero(value: str | None) -> int:
    try:
        return int(float(value or 0))
    except ValueError:
        return 0
