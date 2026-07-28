from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.audit_business_terms import audit
from services.search_taxonomy import (
    contains_term,
    load_search_taxonomy,
    matching_terms,
    ordered_dedupe,
)

ROOT = Path(__file__).resolve().parents[1]


class SearchTaxonomyTests(unittest.TestCase):
    def test_config_groups_capabilities_and_aliases_are_loaded(self) -> None:
        taxonomy = load_search_taxonomy()

        self.assertEqual("rural health transformation", taxonomy.groups["rht_explicit"][0])
        self.assertIn("claims processing", taxonomy.groups["gainwell_capabilities"])
        self.assertIn("Gainwell Technologies LLC", taxonomy.aliases_by_organization["gainwell"])
        self.assertIn("Acentra Health", taxonomy.competitor_aliases)
        self.assertEqual("Medicaid", taxonomy.business_terms[0])

    def test_stable_ordered_dedupe_and_boundary_matching(self) -> None:
        self.assertEqual(["CMS", "Medicaid"], ordered_dedupe(["CMS", "cms", " Medicaid "]))
        self.assertTrue(contains_term("CMS.gov Medicaid modernization", "CMS"))
        self.assertFalse(contains_term("A mechanism and craftsmanship", "CMS"))
        self.assertFalse(contains_term("bright rural future", "RHT"))
        self.assertEqual(
            ["rural health", "CMS"],
            matching_terms("CMS rural   health update", ["rural health", "CMS", "cms"]),
        )

    def test_legacy_monitored_keywords_remain_supported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "parameters.json"
            path.write_text(json.dumps({"monitored_keywords": ["Custom", "custom", "Medicaid"]}), encoding="utf-8")
            taxonomy = load_search_taxonomy(path)
        self.assertEqual(["Custom", "Medicaid"], taxonomy.business_terms)

    def test_invalid_group_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "parameters.json"
            path.write_text(json.dumps({"taxonomy": {"rht_explicit": "RHT"}}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "taxonomy.rht_explicit"):
                load_search_taxonomy(path)


class BusinessTermAuditTests(unittest.TestCase):
    def test_audit_categorizes_remaining_constants(self) -> None:
        findings = audit(ROOT)
        categories = {finding.category for finding in findings}
        self.assertIn("migration_candidate", categories)
        self.assertIn("justified_source_specific", categories)
        self.assertTrue(any(finding.name == "TOPIC_RULES" for finding in findings))
        self.assertFalse(any(finding.path == "services/usaspending_client.py" and finding.name == "DEFAULT_KEYWORDS" for finding in findings))


if __name__ == "__main__":
    unittest.main()
