from __future__ import annotations

import importlib
import unittest
from unittest import mock

from services.state_updates import official_feed
from services.state_updates.store import STATE_UPDATE_FIELDS

STATES = ("ID", "KS", "MO", "NV", "OK", "UT", "SC", "MS", "NE", "NH", "RI")


class OfficialStatePolicyCollectorTests(unittest.TestCase):
    def test_all_collectors_normalize_exact_dated_item_links_and_schema(self) -> None:
        markup = """
        <ul>
          <li>July 15, 2026 — <a href="/documents/provider-bulletin-26-07.pdf">Medicaid Provider Bulletin 26-07</a></li>
          <li>07/16/2026 — <a href="/procurement/contract.pdf">Medicaid contract award bulletin</a></li>
          <li><a href="/documents/undated.pdf">Medicaid provider bulletin without item date</a></li>
        </ul>
        """
        for state in STATES:
            with self.subTest(state=state):
                module = importlib.import_module(f"services.state_updates.{state.lower()}")
                source_url = f"https://health.{state.lower()}.gov/medicaid/updates"
                source = {"key": f"{state.lower()}_test", "url": source_url, "record_type": "provider_bulletin", "terms": ["bulletin"]}
                with mock.patch.object(module, "SOURCES", [source]), mock.patch.object(official_feed, "fetch_text", return_value=markup):
                    records = module.fetch_updates(keywords=["Medicaid"], max_records=10)
                self.assertEqual(1, len(records))
                record = records[0]
                self.assertEqual(STATE_UPDATE_FIELDS, list(record))
                self.assertEqual(state, record["state"])
                self.assertEqual("provider_bulletin", record["record_type"])
                self.assertEqual("2026-07-15", record["posted_date"])
                self.assertEqual("https://health.%s.gov/documents/provider-bulletin-26-07.pdf" % state.lower(), record["document_url"])
                self.assertEqual(source_url, record["source_url"])

    def test_zero_and_negative_limits_never_attempt_network(self) -> None:
        for state in STATES:
            with self.subTest(state=state):
                module = importlib.import_module(f"services.state_updates.{state.lower()}")
                with mock.patch.object(official_feed, "fetch_text", side_effect=AssertionError("network attempted")):
                    self.assertEqual([], module.fetch_updates(keywords=[], max_records=0))
                    self.assertEqual([], module.fetch_updates(keywords=[], max_records=-1))

    def test_json_feed_preserves_official_item_url_and_date(self) -> None:
        payload = '{"items":[{"title":"1115 Medicaid waiver public notice","url":"/notices/1115-2026","published_date":"2026-06-02"},{"title":"Medicaid RFP","url":"/bids/1","published_date":"2026-06-03"}]}'
        rows = official_feed.source_rows(payload, "https://agency.gov/feed.json", ["medicaid"])
        self.assertEqual([{"title": "1115 Medicaid waiver public notice", "url": "https://agency.gov/notices/1115-2026", "date": "2026-06-02", "context": "1115 Medicaid waiver public notice /notices/1115-2026 2026-06-02"}], rows)

    def test_monthly_bulletin_uses_declared_issue_month_only(self) -> None:
        self.assertEqual("2025-01-01", official_feed.date_from_text("January 2025 Provider Bulletin"))
        self.assertEqual("", official_feed.date_from_text("Training July-September 2026"))

    def test_csv_feed_is_dated_and_procurement_free(self) -> None:
        payload = "title,url,date\nRural Health Transformation update,/rht/7,7/1/2026\nRural Health RFP,/rfp/8,7/2/2026\nUndated Medicaid notice,/notice/9,\n"
        rows = official_feed.source_rows(payload, "https://agency.gov/feed.csv", ["rural health", "medicaid"])
        self.assertEqual(1, len(rows))
        self.assertEqual("https://agency.gov/rht/7", rows[0]["url"])
        self.assertEqual("2026-07-01", rows[0]["date"])

    def test_challenge_pages_are_skipped_without_bypass(self) -> None:
        module = importlib.import_module("services.state_updates.id")
        messages: list[str] = []
        source = {"key": "blocked", "url": "https://agency.gov/notices", "record_type": "medicaid_notice", "terms": ["medicaid"]}
        with mock.patch.object(module, "SOURCES", [source]), mock.patch.object(official_feed, "fetch_text", return_value='<a href="https://validate.perfdrive.com/">Access validation</a>'):
            self.assertEqual([], module.fetch_updates(keywords=[], max_records=5, progress=messages.append))
        self.assertTrue(any("challenge page" in message for message in messages))

    def test_configured_sources_are_official_and_not_procurement_feeds(self) -> None:
        for state in STATES:
            module = importlib.import_module(f"services.state_updates.{state.lower()}")
            for source in module.SOURCES:
                with self.subTest(state=state, source=source["key"]):
                    self.assertTrue(source["url"].startswith("https://"))
                    self.assertNotRegex(source["url"].lower(), r"procurement|purchas|contract|solicitation|bid")
                    self.assertTrue(source["terms"])


if __name__ == "__main__":
    unittest.main()
