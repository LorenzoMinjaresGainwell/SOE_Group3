from __future__ import annotations

import csv
import io
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
KEYS = {
    "opportunities.csv": ("id",),
    "sources.csv": ("id",),
    "source_runs.csv": ("source", "mode"),
}
TIMESTAMPS = {
    "opportunities.csv": ("last_checked_at", "last_updated_at"),
    "sources.csv": ("last_checked_at",),
    "source_runs.csv": ("finished_at", "last_successful_run"),
}


def repair(path: Path, keys: tuple[str, ...], timestamps: tuple[str, ...]) -> None:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("<<<<<<< Updated upstream"):
        return
    upstream, stashed = text.split("=======", 1)
    upstream = upstream.split("\n", 1)[1]
    stashed = stashed.rsplit(">>>>>>> Stashed changes", 1)[0].lstrip("\r\n")
    blocks = [list(csv.DictReader(io.StringIO(block))) for block in (upstream, stashed)]
    fieldnames = list(csv.DictReader(io.StringIO(upstream)).fieldnames or [])
    merged: dict[tuple[str, ...], dict[str, str]] = {}
    for rows in blocks:
        for row in rows:
            key = tuple(row.get(field, "") for field in keys)
            current = merged.get(key)
            if current is None or newest(row, timestamps) >= newest(current, timestamps):
                merged[key] = {field: row.get(field, "") for field in fieldnames}
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows(merged.values())


def newest(row: dict[str, str], fields: tuple[str, ...]) -> str:
    return max((row.get(field, "") for field in fields), default="")


def main() -> None:
    for filename, keys in KEYS.items():
        repair(ROOT / "data" / filename, keys, TIMESTAMPS[filename])


if __name__ == "__main__":
    main()
