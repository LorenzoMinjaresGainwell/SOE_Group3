import csv
import tempfile
import unittest
from pathlib import Path

from services.csv_store import CsvStore


class CsvStoreOpportunityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temporary.name)
        self.store = CsvStore(self.data_dir)

    def tearDown(self):
        self.temporary.cleanup()

    def write_csv(self, name, rows):
        with (self.data_dir / name).open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=rows[0].keys())
            writer.writeheader()
            writer.writerows(rows)

    def test_reads_only_canonical_opportunity_inputs(self):
        self.write_csv("opportunities.csv", [{"id": "legacy", "title": "Legacy aggregate"}])
        self.write_csv("contracts.csv", [{"id": "legacy-contract", "title": "Legacy contract"}])
        self.write_csv("state_opportunities.csv", [{
            "id": "state-1", "title": "State RFP", "state": "TX", "agency": "HHSC",
            "source": "state portal", "document_type": "RFP", "amount": "100",
            "relevance_score": "70", "matched_keywords": "Medicaid",
        }])
        self.write_csv("federal_opportunities.csv", [{
            "opportunity_id": "federal-1", "title": "Federal RFP", "agency": "CMS",
            "source_key": "sam_opportunities", "record_type": "opportunity", "importance_score": "80",
        }])
        self.write_csv("federal_grants.csv", [{
            "grant_id": "grant-1", "opportunity_title": "Federal Grant", "agency": "HRSA",
            "opportunity_number": "G-1", "estimated_total_program_funding": "1000", "importance_score": "75",
        }])

        records = {row["id"]: row for row in self.store.list_opportunities()}
        self.assertEqual(set(records), {"state-opportunity-state-1", "federal-1", "grant-1"})
        self.assertEqual(records["grant-1"]["opportunity_type"], "grant")
        self.assertNotIn("legacy-contract", records)

    def test_api_sourced_opportunities_can_be_reviewed_and_pinned(self):
        self.write_csv("state_opportunities.csv", [{
            "id": "tx-1", "title": "State RFP", "state": "TX", "agency": "HHSC",
            "source": "state portal", "document_type": "RFP", "relevance_score": "75",
            "amount": "500", "matched_keywords": "Medicaid",
        }])

        opportunity_id = "state-opportunity-tx-1"
        self.assertEqual(self.store.update_status(opportunity_id, "Pursue")["status"], "Pursue")
        pinned = self.store.update_pinned(opportunity_id, True)
        self.assertTrue(pinned["pinned"])
        self.assertTrue(pinned["reviewable"])
        self.assertEqual(pinned["status_history"][-1]["to"], "Pursue")

    def test_honors_data_dir_scoring_and_competitor_configs(self):
        custom_dir = self.data_dir / "custom"
        custom_dir.mkdir()
        (custom_dir / "capability_rules.csv").write_text(
            "rule_id,category,tier,terms,strength,description\n"
            "rht,rht,explicit,local transformation,1,Local RHT\n"
            "special,capability,direct,custom capability,1,Local capability\n",
            encoding="utf-8",
        )
        (custom_dir / "strategic_jurisdictions.csv").write_text(
            "jurisdiction,priority,reason\nZZ,1,Local priority\n", encoding="utf-8"
        )
        (custom_dir / "competitor_aliases.csv").write_text(
            "profile_order,organization_key,canonical_name,organization_type,alias,alias_type\n"
            "1,gainwell,Gainwell Local,gainwell,Gainwell Local,canonical\n"
            "2,localco,Local Competitor,competitor,Local Competitor,canonical\n",
            encoding="utf-8",
        )

        custom = CsvStore(custom_dir)
        result = custom.priority_scorer.score(
            {"title": "Local transformation custom capability", "state": "ZZ"}, "opportunities"
        )
        self.assertEqual(result["rht_strength"], "explicit")
        self.assertEqual([profile.key for profile in custom.competitor_intelligence.profiles], ["gainwell", "localco"])

    def test_nonfinite_csv_numbers_fall_back_instead_of_entering_responses(self):
        self.write_csv("federal_opportunities.csv", [{
            "opportunity_id": "bad-number", "title": "Bad number", "award_amount": "nan",
            "importance_score": "inf", "source_key": "sam_opportunities",
        }])
        record = self.store.list_opportunities()[0]
        self.assertEqual(record["amount"], 0)
        self.assertEqual(record["importance_score"], 0)

    def test_sorts_pinned_first_then_by_amount(self):
        self.write_csv("federal_opportunities.csv", [
            {"opportunity_id": "small", "title": "Small", "source_key": "sam_opportunities",
             "importance_score": "100", "award_amount": "100"},
            {"opportunity_id": "large", "title": "Large", "source_key": "sam_opportunities",
             "importance_score": "10", "award_amount": "1000"},
        ])

        self.assertEqual([row["id"] for row in self.store.list_opportunities()], ["large", "small"])
        self.store.update_pinned("small", True)
        self.assertEqual([row["id"] for row in self.store.list_opportunities()], ["small", "large"])


if __name__ == "__main__":
    unittest.main()
