import csv
import tempfile
import unittest
from pathlib import Path

from services.csv_store import CsvStore


class CsvStoreCategoryTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp_dir.name)
        self.store = CsvStore(self.data_dir)

    def tearDown(self):
        self.temp_dir.cleanup()

    def write_csv(self, name, rows):
        path = self.data_dir / name
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=rows[0].keys())
            writer.writeheader()
            writer.writerows(rows)

    def test_aggregates_state_federal_grant_and_contract_feeds(self):
        self.write_csv("opportunities.csv", [
            {
                "id": "sam-1", "title": "Federal RFP", "state": "Federal",
                "agency": "CMS", "source": "SAM.gov Opportunities API",
                "document_type": "Solicitation", "fit_score": "80",
                "budget_estimate": "100", "program_focus": "",
                "keywords_matched": "", "risks": "",
            },
            {
                "id": "grant-1", "title": "Rural health grant", "state": "Federal",
                "agency": "HRSA", "source": "Grants.gov Search API",
                "document_type": "Posted", "fit_score": "70",
                "budget_estimate": "0", "program_focus": "",
                "keywords_matched": "rural health", "risks": "",
            },
        ])
        self.write_csv("state_opportunities.csv", [
            {
                "id": "tx-1", "title": "State RFP", "state": "TX",
                "agency": "HHSC", "source": "TXSmartBuy", "document_type": "RFP",
                "relevance_score": "75", "amount": "500", "matched_keywords": "Medicaid",
            },
        ])
        self.write_csv("contracts.csv", [
            {
                "id": "award-1", "recipient_name": "Competitor", "description": "Claims",
                "awarding_agency": "HHS", "recompete_signal": "Expiring soon",
                "relevance_score": "65", "award_amount": "1000", "matched_keywords": "claims",
            },
        ])

        records = {row["id"]: row for row in self.store.list_opportunities()}

        self.assertIn("federal_opportunities", records["sam-1"]["categories"])
        self.assertIn("grants", records["grant-1"]["categories"])
        self.assertIn("state_opportunities", records["state-opportunity-tx-1"]["categories"])
        self.assertIn("competitor_signals", records["federal-contract-award-1"]["categories"])
        self.assertIn("contract_expirations", records["federal-contract-award-1"]["categories"])

    def test_contract_expiration_requires_actionable_recompete_window(self):
        self.write_csv("contracts.csv", [
            {
                "id": "award-2", "recipient_name": "Competitor", "description": "Platform",
                "recompete_signal": "Longer-term contract", "relevance_score": "30",
                "award_amount": "100", "matched_keywords": "",
            },
        ])

        record = self.store.list_opportunities()[0]

        self.assertIn("competitor_signals", record["categories"])
        self.assertNotIn("contract_expirations", record["categories"])


if __name__ == "__main__":
    unittest.main()
