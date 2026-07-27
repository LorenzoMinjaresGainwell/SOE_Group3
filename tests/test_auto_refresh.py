from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.auto_refresh import apply_changes, compare_snapshots


class RefreshComparisonTests(unittest.TestCase):
    def test_labels_new_updated_and_current_records(self):
        before = {
            "same": {"id": "same", "title": "Same"},
            "changed": {"id": "changed", "title": "Old"},
        }
        after = {
            "same": {"id": "same", "title": "Same"},
            "changed": {"id": "changed", "title": "New"},
            "added": {"id": "added", "title": "Added"},
        }

        changes = compare_snapshots(before, after)

        self.assertEqual(changes["same"]["label"], "Current")
        self.assertEqual(changes["changed"]["label"], "Updated")
        self.assertEqual(changes["changed"]["changed_fields"], ["title"])
        self.assertEqual(changes["added"]["label"], "New")

    def test_sam_records_are_always_marked_cached(self):
        records = [{"id": "sam-1", "source": "SAM.gov Opportunities API"}]
        apply_changes(records, {})
        self.assertEqual(records[0]["refresh_label"], "SAM cached")


if __name__ == "__main__":
    unittest.main()
