import tempfile
import unittest
from datetime import date
from pathlib import Path

from services.priority_scoring import FAMILY_WEIGHTS, PriorityScorer


TODAY = date(2026, 7, 28)


class PriorityScoringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.scorer = PriorityScorer(Path(__file__).resolve().parents[1] / "data")

    def test_approved_weights_and_explainable_shape(self):
        record = {
            "title": "Rural Health Transformation MMIS claims interoperability procurement",
            "agency": "Texas HHSC", "state": "TX", "due_date": "2026-08-27",
            "amount": "100000000", "importance_score": "100", "rht_flag": "true",
        }
        result = self.scorer.score(record, "opportunity", model="B", today=TODAY)
        self.assertEqual({d["dimension"]: d["max"] for d in result["dimensions"]},
                         FAMILY_WEIGHTS["opportunities"])
        self.assertEqual(result["rht_strength"], "explicit")
        self.assertEqual(result["action"], "Pursue")
        self.assertEqual(result["confidence"]["penalty"], 0)
        for dimension in result["dimensions"]:
            self.assertIn("score", dimension)
            self.assertIn("evidence", dimension)
            self.assertIn("missing_notes", dimension)
        self.assertGreaterEqual(result["score"], 90)
        self.assertLessEqual(result["score"], 100)

    def test_model_a_uses_ten_percent_source_and_b_ignores_it(self):
        record = {
            "title": "Medicaid claims system", "agency": "Agency", "state": "TX",
            "due_date": "2026-08-20", "amount": "1000000", "relevance_score": "100",
        }
        a = self.scorer.score(record, "opportunities", model="A", today=TODAY)
        b = self.scorer.score(record, "opportunities", model="B", today=TODAY)
        self.assertAlmostEqual(a["score"], round(0.9 * b["dimensional_score"] + 10, 1))
        self.assertEqual(b["source_score_note"], "ignored by Model B")
        no_source = dict(record)
        no_source.pop("relevance_score")
        self.assertEqual(self.scorer.score(no_source, "opportunities", model="A", today=TODAY)["score"],
                         self.scorer.score(no_source, "opportunities", model="B", today=TODAY)["score"])

    def test_opportunity_urgency_peaks_from_fifteen_through_forty_five_days(self):
        def urgency(days):
            result = self.scorer.score({"due_date": date.fromordinal(TODAY.toordinal() + days).isoformat()},
                                       "opportunities", today=TODAY)
            return next(d["score"] for d in result["dimensions"] if d["dimension"] == "urgency")
        self.assertEqual(urgency(15), 10)
        self.assertEqual(urgency(45), 10)
        self.assertLess(urgency(7), 10)
        self.assertLess(urgency(60), 10)

    def test_rht_tiers_are_explicit_direct_related_generic_none(self):
        examples = (
            ({"title": "RHT funding"}, "explicit"),
            ({"title": "Rural hospital modernization"}, "direct"),
            ({"title": "Medicaid transformation"}, "related"),
            ({"title": "Health system notice"}, "generic"),
            ({"title": "Office furniture"}, "none"),
        )
        for record, expected in examples:
            with self.subTest(expected=expected):
                self.assertEqual(self.scorer.score(record, "updates", today=TODAY)["rht_strength"], expected)

    def test_contract_actions_cover_approved_vocabulary(self):
        records = (
            ({"end_date": "2026-01-01"}, "Historical"),
            ({"end_date": "2026-12-01", "vendor_name": "Gainwell Technologies"}, "Retain"),
            ({"end_date": "2026-12-01", "competitor_flag": "true"}, "Compete"),
            ({"end_date": "2027-12-01"}, "Prepare"),
            ({"end_date": "2030-12-01"}, "Monitor"),
        )
        for record, action in records:
            with self.subTest(action=action):
                self.assertEqual(self.scorer.score(record, "contracts", today=TODAY)["action"], action)

    def test_update_actions_and_confidence_penalty_are_separate(self):
        actions = (
            ({"action_required_by": "2026-08-10"}, "Act"),
            ({"comment_required_flag": "true"}, "Review"),
            ({"title": "Rural health notice", "state": "TX"}, "Monitor"),
            ({"title": "Office notice"}, "Informational"),
        )
        for record, expected in actions:
            with self.subTest(expected=expected):
                result = self.scorer.score(record, "updates", today=TODAY)
                self.assertEqual(result["action"], expected)
                self.assertGreaterEqual(result["confidence"]["penalty"], 0)
                self.assertLessEqual(result["confidence"]["penalty"], 2.75)
                self.assertAlmostEqual(result["score"], max(0, result["dimensional_score"] - result["confidence"]["penalty"]))

    def test_text_scoring_uses_explicit_meaningful_fields_and_list_values(self):
        ignored_metadata = {
            "id": "rht-rural-health-transformation-mmis",
            "source_record_id": "medicaid-claims",
            "raw_json": '{"title":"Rural Health Transformation"}',
        }
        self.assertEqual(self.scorer.score(ignored_metadata, "opportunities", today=TODAY)["rht_strength"], "none")

        intended_lists = {
            "program_focus": ["Rural Health Transformation"],
            "topic_keys": ["interoperability"],
            "capabilities": ["claims processing"],
        }
        result = self.scorer.score(intended_lists, "opportunities", today=TODAY)
        self.assertEqual(result["rht_strength"], "explicit")
        capability = next(item for item in result["dimensions"] if item["dimension"] == "capability")
        self.assertTrue(capability["evidence"])

    def test_invalid_nonfinite_config_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "capability_rules.csv").write_text(
                "rule_id,category,tier,terms,strength,description\n"
                "bad,rht,explicit,rht,nan,Bad strength\n",
                encoding="utf-8",
            )
            (root / "strategic_jurisdictions.csv").write_text(
                "jurisdiction,priority,reason\nUS,0.7,Federal\n", encoding="utf-8"
            )
            with self.assertRaises(ValueError):
                PriorityScorer(root)

    def test_nonfinite_numeric_values_are_rejected(self):
        for value in ("nan", "inf", "-inf", float("nan")):
            with self.subTest(value=value):
                result = self.scorer.score({"amount": value}, "opportunities", today=TODAY)
                dimension = next(item for item in result["dimensions"] if item["dimension"] == "value")
                self.assertEqual(dimension["score"], 0)
                self.assertEqual(dimension["missing_notes"], "Monetary value missing")

    def test_injected_date_is_stable(self):
        record = {"title": "Medicaid comment notice", "posted_date": "2026-07-01", "state": "TX"}
        self.assertEqual(self.scorer.score(record, "updates", today=TODAY),
                         self.scorer.score(record, "updates", today=TODAY))


if __name__ == "__main__":
    unittest.main()
