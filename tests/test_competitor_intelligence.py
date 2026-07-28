from __future__ import annotations

import datetime as dt
import unittest

from services.competitor_intelligence import (
    CompetitorIntelligence,
    classify_organization,
    custom_search,
    load_profiles,
    search_mentions,
    summarize_records,
)


class CompetitorIdentityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = CompetitorIntelligence()

    def test_gainwell_is_first_and_distinct(self):
        profiles = load_profiles()
        self.assertEqual("gainwell", profiles[0].key)
        self.assertEqual("gainwell", profiles[0].organization_type)
        self.assertTrue(all(profile.organization_type == "competitor" for profile in profiles[1:]))
        self.assertEqual("competitor", classify_organization("MAXIMUS Federal Services, Inc."))

    def test_case_and_punctuation_are_ignored_but_boundaries_are_not(self):
        result = self.engine.resolve("gAiNwElL-TECHNOLOGIES, L.L.C.")
        self.assertEqual("gainwell", result.organization_key)
        self.assertEqual("Gainwell Technologies", result.canonical_name)

        self.assertEqual("acentra", self.engine.resolve("C.N.S.I., L.L.C.").organization_key)
        self.assertEqual("deloitte", self.engine.resolve("Deloitte & Touche").organization_key)
        self.assertEqual("other", self.engine.classify("PCGamer Media"))
        self.assertEqual("other", self.engine.classify("CGIAR Consortium"))
        self.assertEqual("other", self.engine.classify("MaximusHealth Clinic"))

    def test_predecessors_resolve_to_gainwell(self):
        for name in (
            "Health Management Systems, Inc.",
            "HMS Holdings Corp.",
            "DXC Technology Services LLC",
            "Hewlett-Packard Enterprise Services, LLC",
            "HP Enterprise Services LLC",
        ):
            with self.subTest(name=name):
                result = self.engine.resolve(name)
                self.assertEqual("gainwell", result.organization_key)
                self.assertEqual("predecessor", result.alias_type)

    def test_ambiguous_acronyms_only_match_exact_vendor_identity_fields(self):
        for acronym, key in (("HMS", "gainwell"), ("PCG", "pcg"), ("CGI", "cgi")):
            with self.subTest(acronym=acronym):
                self.assertEqual(key, self.engine.resolve(acronym).organization_key)
                self.assertEqual(key, self.engine.find_mentions({"vendor_name": acronym})[0].organization_key)
                self.assertEqual((), self.engine.find_mentions({"title": f"Modernizing the {acronym} program"}))
                self.assertEqual((), self.engine.find_mentions({"summary": f"General prose about {acronym} services"}))
        self.assertEqual("cgi", self.engine.find_mentions({"title": "CGI Federal incumbent transition"})[0].organization_key)
        self.assertEqual("pcg", self.engine.find_mentions({"summary": "Public Consulting Group supports eligibility"})[0].organization_key)

    def test_broad_health_management_names_do_not_collide(self):
        for name in (
            "Health Management Associates, Inc.",
            "Community Health Management Group",
            "Health Management Consulting Services",
        ):
            with self.subTest(name=name):
                self.assertEqual("other", self.engine.classify(name))

    def test_unmatched_vendor_identity_is_preserved(self):
        result = self.engine.resolve("Acme Health, LLC")
        self.assertFalse(result.matched)
        self.assertEqual("Acme Health, LLC", result.original_name)
        self.assertEqual("Acme Health, LLC", result.canonical_name)
        self.assertEqual("other", result.organization_type)


class CompetitorRecordTests(unittest.TestCase):
    def setUp(self):
        self.records = [
            {
                "id": "c1",
                "vendor_name": "HMS Holdings Corp.",
                "title": "Medicaid claims platform",
                "state": "UT",
                "amount": "$1,250,000.00",
                "end_date": "2026-03-15",
            },
            {
                "id": "c2",
                "vendor_name": "MAXIMUS Federal Services, Inc.",
                "title": "Eligibility support",
                "state": "VA",
                "award_amount": "250000",
                "period_end_date": "2026-08-01",
            },
            {
                "id": "o1",
                "title": "Acentra Health incumbent transition for Medicaid analytics",
                "summary": "Responses are due soon.",
                "state": "VA",
                "due_date": "2027-06-01",
            },
            {
                "id": "x1",
                "vendor_name": "Health Management Associates, Inc.",
                "title": "Unrelated advisory work",
                "state": "FL",
            },
        ]

    def test_searches_normalized_contract_and_opportunity_mentions(self):
        gainwell = search_mentions(self.records, organization_keys=["gainwell"])
        self.assertEqual(["c1"], [row["id"] for row in gainwell])
        self.assertEqual("gainwell", gainwell[0]["organization_key"])

        competitors = search_mentions(self.records, organization_types=["competitor"])
        self.assertEqual(["c2", "o1"], [row["id"] for row in competitors])
        self.assertEqual("acentra", competitors[1]["organization_mentions"][0]["organization_key"])

    def test_custom_search_is_local_case_and_punctuation_insensitive(self):
        matches = custom_search(self.records, "medicaid-analytics")
        self.assertEqual(["o1"], [row["id"] for row in matches])
        self.assertEqual([], custom_search(self.records, "not collected here"))

    def test_summary_aggregates_counts_value_end_windows_and_jurisdictions(self):
        summary = summarize_records(self.records, as_of=dt.date(2026, 1, 1))
        self.assertEqual(4, summary["record_count"])
        self.assertEqual(1_500_000.0, summary["total_value"])
        self.assertEqual({"count": 2, "value": 250000.0}, summary["by_jurisdiction"]["VA"])
        self.assertEqual(1, summary["by_organization"]["Gainwell Technologies"]["count"])
        self.assertEqual(2, summary["by_organization_type"]["competitor"]["count"])
        self.assertEqual(1, summary["end_windows"]["0_90_days"])
        self.assertEqual(1, summary["end_windows"]["181_365_days"])
        self.assertEqual(1, summary["end_windows"]["over_365_days"])
        self.assertEqual(1, summary["end_windows"]["unknown"])


if __name__ == "__main__":
    unittest.main()
