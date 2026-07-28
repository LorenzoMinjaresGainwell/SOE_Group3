import csv
import json
import tempfile
import threading
import time
import unittest
from datetime import date
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import app
from services.csv_store import CsvStore


class DashboardFamilyTests(unittest.TestCase):
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

    def seed_all_families(self):
        self.write_csv("state_opportunities.csv", [{
            "id": "state-opp", "state": "TX", "source": "state portal", "source_record_id": "RFP-1",
            "title": "State RFP", "agency": "HHSC", "document_type": "RFP", "posted_date": "2026-01-01",
            "due_date": "2026-02-01", "status": "open", "amount": "1200", "document_url": "https://example/opp",
            "source_url": "https://example", "matched_keywords": "Medicaid;MMIS", "relevance_score": "72",
            "last_checked_at": "2026-01-02",
        }])
        self.write_csv("federal_opportunities.csv", [{
            "opportunity_id": "fed-opp", "source_key": "sam_opportunities", "sam_notice_id": "notice-1",
            "solicitation_number": "SOL-1", "title": "Federal RFP", "notice_type": "Solicitation",
            "record_type": "opportunity", "agency": "CMS", "posted_date": "2026-01-01", "due_date": "2026-03-01",
            "program_focus": "medicaid", "topic_keys": "mmis;claims", "lifecycle_status": "upcoming",
            "importance_score": "81", "document_url": "https://sam.example/1", "source_url": "https://sam.example",
            "summary": "Procurement", "last_checked_at": "2026-01-02",
        }])
        self.write_csv("federal_grants.csv", [{
            "grant_id": "grant-1", "opportunity_number": "HHS-1", "opportunity_title": "Rural grant",
            "agency": "HRSA", "posted_date": "2026-01-01", "close_date": "2026-04-01", "award_ceiling": "500",
            "award_floor": "10", "estimated_total_program_funding": "5000", "expected_awards": "10",
            "eligibility": "States", "program_focus": "rural_health", "topic_keys": "rht",
            "importance_score": "90", "document_url": "https://grants.example/1", "last_checked_at": "2026-01-02",
        }])
        self.write_csv("state_contracts.csv", [{
            "id": "state-contract", "state": "OH", "source": "award portal", "vendor_name": "Gainwell Technologies LLC",
            "agency": "Medicaid", "contract_number": "C-1", "title": "Claims", "amount": "900",
            "start_date": "2020-01-01", "end_date": "2024-01-01", "months_to_end": "-24",
            "recompete_signal": "Expired", "contract_record_type": "award", "relevance_score": "75",
            "matched_keywords": "claims", "last_checked_at": "2026-01-02",
        }])
        self.write_csv("federal_contract_lifecycle.csv", [{
            "contract_id": "fed-contract", "source_keys": "usaspending", "lifecycle_status": "unknown",
            "contract_vehicle": "task_order", "title": "Eligibility system", "agency": "CMS",
            "vendor_name": "MAXIMUS Federal Services, Inc.", "piid": "P-1", "period_start_date": "2025-01-01", "period_end_date": "",
            "days_until_end": "", "award_amount": "4000", "competitor_flag": "true", "importance_score": "88",
            "program_focus": "medicaid", "topic_keys": "eligibility", "last_checked_at": "2026-01-02",
        }])
        self.write_csv("state_policy_updates.csv", [{
            "id": "state-policy", "state": "WA", "source": "medicaid notices", "record_type": "spa_notice",
            "title": "SPA notice", "agency": "HCA", "program_focus": "medicaid", "topic_keys": "waiver",
            "posted_date": "2026-01-01", "due_date": "2026-02-01", "comment_required_flag": "true",
            "action_required_by": "2026-02-01", "importance_score": "70", "summary": "Comment requested",
            "document_url": "https://example/update", "source_url": "https://example", "matched_keywords": "Medicaid",
            "rht_flag": "false", "last_checked_at": "2026-01-02",
        }])
        self.write_csv("federal_updates_catalog.csv", [
            {"update_id": "fed-policy", "source_key": "federal_register", "source_record_id": "FR-1",
             "record_type": "policy_update", "title": "Federal rule", "agency": "CMS",
             "importance_score": "60", "posted_date": "2026-01-01", "topic_keys": "medicaid",
             "rht_flag": "true", "comment_required_flag": "true",
             "score_evidence_json": '{"topic_score": 20}'},
            {"update_id": "duplicate-opp", "source_key": "sam_opportunities", "source_record_id": "notice-1",
             "record_type": "opportunity", "title": "Federal RFP", "agency": "CMS", "importance_score": "99"},
            {"update_id": "duplicate-grant", "source_key": "grants", "source_record_id": "HHS-1",
             "record_type": "grant", "title": "Rural grant", "agency": "HRSA", "importance_score": "99"},
            {"update_id": "duplicate-contract", "source_key": "usaspending", "source_record_id": "P-1",
             "record_type": "award", "title": "Eligibility system", "agency": "CMS", "importance_score": "99"},
            {"update_id": "irrelevant-data", "source_key": "cms_data", "source_record_id": "data-0",
             "record_type": "dataset_signal", "title": "Irrelevant data", "agency": "CMS", "importance_score": "0"},
            {"update_id": "relevant-data", "source_key": "cms_data", "source_record_id": "data-1",
             "record_type": "dataset_signal", "title": "Relevant data", "agency": "CMS", "importance_score": "25"},
        ])

    def test_families_are_strictly_separate_and_catalog_duplicates_are_excluded(self):
        self.seed_all_families()
        opportunities, contracts, updates = self.store.list_opportunities(), self.store.list_contracts(), self.store.list_updates()
        self.assertEqual({row["id"] for row in opportunities}, {"state-opportunity-state-opp", "fed-opp", "grant-1"})
        self.assertEqual({row["id"] for row in contracts}, {"state-contract-state-contract", "fed-contract"})
        self.assertEqual({row["id"] for row in updates}, {"state-update-state-policy", "fed-policy", "relevant-data"})
        for family, rows in (("opportunities", opportunities), ("contracts", contracts), ("updates", updates)):
            self.assertTrue(all(row["family"] == family for row in rows))

    def test_family_mappings_relevance_and_lifecycle_are_meaningful(self):
        self.seed_all_families()
        grant = self.store.get_opportunity("grant-1")
        self.assertEqual((grant["due_date"], grant["amount"], grant["opportunity_type"]),
                         ("2026-04-01", 5000, "grant"))
        state_opportunity = self.store.get_opportunity("state-opportunity-state-opp")
        self.assertEqual(state_opportunity["status"], "Unreviewed")
        self.assertEqual(state_opportunity["source_status"], "open")
        unknown = self.store.get_contract("fed-contract")
        self.assertEqual(unknown["status"], "unknown")
        self.assertIsNone(unknown["days_until_end"])
        self.assertFalse(unknown["expired"])
        expired = self.store.get_contract("state-contract-state-contract")
        self.assertEqual(expired["status"], "expired")
        self.assertTrue(expired["expired"])
        federal_records = {row["id"]: row for row in self.store.list_federal_records()}
        self.assertEqual(set(federal_records), {"fed-policy", "relevant-data"})
        self.assertEqual(federal_records["fed-policy"]["source_key"], "federal_register")
        self.assertEqual(federal_records["fed-policy"]["score_evidence_json"], '{"topic_score": 20}')
        self.assertTrue(federal_records["fed-policy"]["rht_flag"])
        self.assertTrue(federal_records["fed-policy"]["comment_required_flag"])
        self.assertNotIn("rht", federal_records["fed-policy"])
        self.assertNotIn("comment_required", federal_records["fed-policy"])

    def test_contract_notices_are_excluded_only_when_the_opportunity_family_represents_them(self):
        self.write_csv("federal_opportunities.csv", [
            {"opportunity_id": "sam_opportunities-notice-1", "sam_notice_id": "notice-1",
             "solicitation_number": "SOL-1", "title": "Matched notice", "agency": "CMS"},
            {"opportunity_id": "opp-2", "sam_notice_id": "notice-2", "solicitation_number": "SOL-2",
             "title": "Matched award", "agency": "HHS"},
            {"opportunity_id": "opp-3", "sam_notice_id": "notice-3", "solicitation_number": "SOL-3",
             "title": "Title fallback", "agency": "HRSA"},
        ])
        self.write_csv("federal_contract_lifecycle.csv", [
            {"contract_id": "duplicate-source", "source_record_ids": "notice-1", "solicitation_number": "OTHER",
             "contract_vehicle": "opportunity_notice", "lifecycle_status": "expired", "title": "Different", "agency": "CMS"},
            {"contract_id": "duplicate-solicitation", "source_record_ids": "different", "solicitation_number": "SOL-2",
             "contract_vehicle": "award_notice", "lifecycle_status": "award_notice", "title": "Different", "agency": "HHS"},
            {"contract_id": "duplicate-title", "source_record_ids": "different-3", "solicitation_number": "OTHER-3",
             "contract_vehicle": "task_order", "lifecycle_status": "opportunity", "title": "Title fallback", "agency": "HRSA"},
            {"contract_id": "contract-only", "source_record_ids": "notice-4", "solicitation_number": "SOL-4",
             "contract_vehicle": "award_notice", "lifecycle_status": "award_notice", "title": "Contract only", "agency": "CMS"},
        ])

        contract_ids = {row["id"] for row in self.store.list_contracts()}
        self.assertIn("contract-only", contract_ids)
        self.assertTrue({"duplicate-source", "duplicate-solicitation", "duplicate-title"}.isdisjoint(contract_ids))
        self.assertTrue({"sam_opportunities-notice-1", "opp-2", "opp-3"} <=
                        {row["id"] for row in self.store.list_opportunities()})

    def test_contract_urls_and_expiration_use_current_schema_fields(self):
        self.write_csv("federal_contract_lifecycle.csv", [
            {"contract_id": "past-award", "contract_vehicle": "task_order", "lifecycle_status": "Past award",
             "period_end_date": "", "source_urls": "javascript:bad;https://example.test/document;https://api.example.test/item"},
            {"contract_id": "past-end", "contract_vehicle": "standalone_award", "lifecycle_status": "active",
             "period_end_date": "2020-01-01", "source_urls": "https://example.test/past"},
        ])

        contracts = {row["id"]: row for row in self.store.list_contracts()}
        self.assertTrue(contracts["past-award"]["expired"])
        self.assertEqual(contracts["past-award"]["status"], "expired")
        self.assertEqual(contracts["past-award"]["document_url"], "https://example.test/document")
        self.assertEqual(contracts["past-award"]["source_url"], "https://example.test/document")
        self.assertEqual(contracts["past-award"]["source_urls"],
                         ["https://example.test/document", "https://api.example.test/item"])
        self.assertTrue(contracts["past-end"]["expired"])

    def test_only_opportunities_have_review_state_and_pin_mutations(self):
        self.seed_all_families()
        opportunity_id = "state-opportunity-state-opp"
        self.assertEqual(self.store.update_status(opportunity_id, "Pursue")["status"], "Pursue")
        self.assertTrue(self.store.update_pinned(opportunity_id, True)["pinned"])
        self.assertIsNone(self.store.update_status("fed-contract", "Pursue"))
        self.assertIsNone(self.store.update_pinned("fed-policy", True))

    def test_model_b_schema_is_exposed_without_replacing_legacy_compatibility_scores(self):
        self.seed_all_families()
        today = date(2026, 1, 15)
        required = {
            "legacy_score", "priority_score", "priority_label", "confidence",
            "recommended_action", "score_breakdown", "scoring_model", "scored_as_of",
        }
        for rows in (
            self.store.list_opportunities(today=today),
            self.store.list_contracts(today=today),
            self.store.list_updates(today=today),
        ):
            for record in rows:
                self.assertTrue(required <= record.keys())
                self.assertEqual(record["legacy_score"], record["importance_score"])
                self.assertEqual(record["scoring_model"], "B")
                self.assertEqual(record["scored_as_of"], today.isoformat())
                self.assertTrue(record["score_breakdown"])
                for dimension in record["score_breakdown"]:
                    self.assertTrue({"dimension", "score", "max", "evidence", "missing_notes"} <= dimension.keys())
                if "fit_score" in record:
                    self.assertEqual(record["fit_score"], record["legacy_score"])

    def test_contract_scoring_uses_one_injected_date_and_preserves_family_separation(self):
        self.write_csv("state_contracts.csv", [{
            "id": "dated", "state": "OH", "source": "award portal", "vendor_name": "Gainwell Technologies",
            "title": "Dated contract", "end_date": "2024-01-01", "relevance_score": "75",
        }])
        early = {row["id"]: row for row in self.store.list_contracts(today=date(2023, 1, 1))}
        late = {row["id"]: row for row in self.store.list_contracts(today=date(2026, 1, 15))}
        self.assertFalse(early["state-contract-dated"]["expired"])
        self.assertTrue(late["state-contract-dated"]["expired"])
        self.assertEqual({row["family"] for row in late.values()}, {"contracts"})
        self.assertTrue(all(row["scored_as_of"] == "2026-01-15" for row in late.values()))

    def test_focus_summaries_are_compact_bounded_and_action_specific(self):
        self.seed_all_families()
        today = date(2026, 1, 15)
        rht = self.store.rht_overview(today=today, limit=1)
        self.assertEqual(set(rht["counts"]["by_family"]), {"opportunities", "contracts", "updates"})
        expected_families = {family for family, counts in rht["counts"]["by_family"].items() if counts["rht"]}
        self.assertEqual({row["family"] for row in rht["top_records"]}, expected_families)
        self.assertLessEqual(len(rht["top_records"]), len(expected_families))
        self.assertEqual(rht["top_record_limit_scope"], "per_family")
        self.assertTrue(all("score_breakdown" not in row for row in rht["top_records"]))

        competitors = self.store.competitor_profiles(today=today, query="eligibility system", limit=1)
        self.assertEqual(competitors["profiles"][0]["organization_key"], "gainwell")
        self.assertEqual(competitors["profiles"][0]["recommended_action"], "Retain")
        self.assertTrue({"record_count", "active_count", "total_value", "end_windows", "jurisdictions"} <=
                        competitors["profiles"][0]["summary"].keys())
        competitor_profiles = [p for p in competitors["profiles"] if p["organization_type"] == "competitor"]
        self.assertTrue(all(profile["recommended_action"] == "Compete" for profile in competitor_profiles))
        self.assertEqual(competitors["search"]["count"], 1)
        self.assertLessEqual(len(competitors["search"]["records"]), 1)

    def test_list_and_detail_share_the_normalized_envelope(self):
        self.seed_all_families()
        common = {"id", "family", "scope", "title", "state", "agency", "source", "source_url",
                  "document_url", "record_type", "summary", "posted_date", "updated_date", "due_date",
                  "effective_date", "last_checked_at", "importance_score", "program_focus", "topic_keys"}
        for rows, getter in ((self.store.list_opportunities(), self.store.get_opportunity),
                             (self.store.list_contracts(), self.store.get_contract),
                             (self.store.list_updates(), self.store.get_update)):
            self.assertTrue(common <= rows[0].keys())
            detail = getter(rows[0]["id"])
            self.assertEqual({key: rows[0][key] for key in common}, {key: detail[key] for key in common})


class DashboardRouteTests(DashboardFamilyTests):
    def setUp(self):
        super().setUp()
        self.seed_all_families()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), app.AppHandler)
        self.store_patch = patch.object(app, "store", self.store)
        self.changes_patch = patch.object(app.auto_refresh, "changes", return_value={})
        self.store_patch.start(); self.changes_patch.start()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join()
        self.store_patch.stop(); self.changes_patch.stop()
        super().tearDown()

    def request(self, method, path, body=None):
        connection = HTTPConnection("127.0.0.1", self.server.server_port)
        connection.request(method, path, body=body, headers={"Content-Type": "application/json"} if body is not None else {})
        response = connection.getresponse()
        payload = json.loads(response.read())
        connection.close()
        return response.status, payload

    def test_family_list_and_detail_routes(self):
        for family, record_id in (("opportunities", "fed-opp"), ("contracts", "fed-contract"), ("updates", "fed-policy")):
            status, rows = self.request("GET", f"/api/{family}")
            self.assertEqual(status, 200)
            self.assertTrue(all(row["family"] == family for row in rows))
            self.assertTrue(all(row["scoring_model"] == "B" for row in rows))
            self.assertTrue(all({"legacy_score", "priority_score", "priority_label", "confidence",
                                 "recommended_action", "score_breakdown"} <= row.keys() for row in rows))
            status, detail = self.request("GET", f"/api/{family}/{record_id}")
            self.assertEqual(status, 200)
            self.assertEqual(detail["id"], record_id)
            self.assertNotIn("analysis", detail)
            self.assertNotIn("fit_breakdown", detail)

    def test_update_api_uses_frontend_canonical_flag_fields_and_preserves_explorer_provenance(self):
        status, update = self.request("GET", "/api/updates/fed-policy")

        self.assertEqual(status, 200)
        self.assertIs(update["rht_flag"], True)
        self.assertIs(update["comment_required_flag"], True)
        self.assertEqual(update["source_key"], "federal_register")
        self.assertEqual(update["score_evidence_json"], '{"topic_score": 20}')
        frontend = (Path(__file__).resolve().parents[1] / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn("item.rht_strength", frontend)
        self.assertIn("item.comment_required_flag", frontend)
        self.assertNotIn("isTrue(item.rht_flag)", frontend)
        self.assertNotIn("item.action_required_flag", frontend)

    def test_rht_and_competitor_routes_have_bounded_summary_schemas(self):
        started = time.perf_counter()
        status, rht = self.request("GET", "/api/rht-overview?limit=1")
        self.assertEqual(status, 200)
        self.assertTrue({"as_of", "counts", "jurisdictions", "top_records", "top_record_limit", "top_record_limit_scope"} <= rht.keys())
        expected_families = {family for family, counts in rht["counts"]["by_family"].items() if counts["rht"]}
        self.assertEqual({row["family"] for row in rht["top_records"]}, expected_families)
        self.assertLessEqual(len(rht["top_records"]), len(expected_families))

        status, competitors = self.request("GET", "/api/competitors?q=eligibility%20system&limit=1")
        self.assertEqual(status, 200)
        self.assertTrue({"as_of", "profiles", "search"} <= competitors.keys())
        self.assertEqual(competitors["profiles"][0]["organization_key"], "gainwell")
        self.assertEqual(competitors["profiles"][0]["recommended_action"], "Retain")
        self.assertEqual(competitors["search"]["count"], 1)
        self.assertLessEqual(len(competitors["search"]["records"]), 1)
        self.assertLess(time.perf_counter() - started, 5.0)

        frontend = (Path(__file__).resolve().parents[1] / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn('"/api/rht-overview"', frontend)
        self.assertIn("payload.top_records", frontend)
        self.assertIn("payload.profiles", frontend)
        self.assertIn("payload.search?.records", frontend)
        self.assertIn("?q=${encodeURIComponent", frontend)
        self.assertIn("item.confidence", frontend)
        self.assertIn("item.recommended_action", frontend)
        self.assertIn("item.score_breakdown", frontend)
        self.assertIn("dimension.missing_notes", frontend)
        self.assertNotIn("/api/focus/rht", frontend)
        self.assertLess(frontend.index('[\"gainwell\", \"Gainwell\"]'),
                        frontend.index('[\"competitors\", \"All competitors\"]'))

    def test_status_and_pin_routes_are_opportunity_only(self):
        status, opportunity = self.request(
            "POST", "/api/opportunities/fed-opp/status", json.dumps({"status": "Pursue"})
        )
        self.assertEqual(status, 200)
        self.assertEqual(opportunity["status"], "Pursue")
        status, opportunity = self.request(
            "POST", "/api/opportunities/fed-opp/pin", json.dumps({"pinned": True})
        )
        self.assertEqual(status, 200)
        self.assertTrue(opportunity["pinned"])
        status, _ = self.request(
            "POST", "/api/contracts/fed-contract/status", json.dumps({"status": "Pursue"})
        )
        self.assertEqual(status, 404)

    def test_malformed_or_non_object_json_is_a_strict_400(self):
        for body in ("{", "[]"):
            status, payload = self.request("POST", "/api/opportunities/fed-opp/status", body)
            self.assertEqual(status, 400)
            self.assertIn("error", payload)


if __name__ == "__main__":
    unittest.main()
