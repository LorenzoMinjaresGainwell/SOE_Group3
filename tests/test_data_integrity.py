from __future__ import annotations

import csv
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
CORE_CSVS = (
    ROOT / "data" / "opportunities.csv",
    ROOT / "data" / "sources.csv",
    ROOT / "data" / "source_runs.csv",
)
CONFLICT_MARKERS = ("<<<<<<<", "=======", ">>>>>>>")


class DataIntegrityTests(unittest.TestCase):
    def test_core_csvs_have_no_merge_conflict_markers_or_malformed_rows(self):
        for path in CORE_CSVS:
            text = path.read_text(encoding="utf-8")
            self.assertFalse(
                any(marker in text for marker in CONFLICT_MARKERS),
                f"{path.name} contains unresolved Git conflict markers",
            )
            with path.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertTrue(rows, f"{path.name} should contain data")
            self.assertTrue(
                all(None not in row for row in rows),
                f"{path.name} contains rows with more values than its header",
            )


if __name__ == "__main__":
    unittest.main()
