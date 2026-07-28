from __future__ import annotations

import importlib
import unittest
from unittest import mock

from services.state_updates.store import STATE_UPDATE_FIELDS


ACTIVE = ("ct", "me", "mt", "nd")


class OfficialUpdateCollectorClosureTests(unittest.TestCase):
    def test_simple_html_collectors_normalize_dates_permalinks_and_exclude_procurement(self) -> None:
        markup = """
          <a href="/docs/medicaid-provider-notice-07-15-2026.pdf">Medicaid provider update 07/15/2026</a>
          <a href="/docs/medicaid-contract-rfp-07-16-2026.pdf">Medicaid contract RFP 07/16/2026</a>
          <a href="/undated/medicaid-policy.pdf">Medicaid policy</a>
        """
        for name in ACTIVE:
            with self.subTest(name=name):
                module = importlib.import_module(f"services.state_updates.{name}")
                source_url = f"https://agency.example.gov/{name}/updates"
                sources = [(f"{name}_fixture", source_url, "medicaid_notice", ["medicaid"])]
                calls = []

                def fake_fetch(url: str, **kwargs: object) -> str:
                    calls.append((url, kwargs))
                    return markup

                with mock.patch.object(module, "SOURCES", sources), mock.patch.object(module, "fetch_text", side_effect=fake_fetch):
                    records = module.fetch_updates(keywords=["Medicaid"], max_records=10)
                self.assertEqual(1, len(records))
                self.assertEqual(STATE_UPDATE_FIELDS, list(records[0]))
                self.assertEqual(name.upper(), records[0]["state"])
                self.assertEqual("2026-07-15", records[0]["posted_date"])
                self.assertEqual("https://agency.example.gov/docs/medicaid-provider-notice-07-15-2026.pdf", records[0]["document_url"])
                self.assertEqual(20, calls[0][1]["timeout"])
                self.assertEqual(1_500_000, calls[0][1]["byte_limit"])

    def test_all_implemented_collectors_have_network_free_zero_limit(self) -> None:
        for name in (*ACTIVE, "mp", "vi"):
            with self.subTest(name=name):
                module = importlib.import_module(f"services.state_updates.{name}")
                with mock.patch.object(module, "fetch_text", side_effect=AssertionError("network attempted")):
                    self.assertEqual([], module.fetch_updates(keywords=[], max_records=0))

    def test_minnesota_is_a_documented_network_free_blocked_noop(self) -> None:
        module = importlib.import_module("services.state_updates.mn")
        messages: list[str] = []
        with mock.patch.object(module, "fetch_text", side_effect=AssertionError("network attempted")):
            self.assertEqual([], module.fetch_updates(keywords=[], max_records=10, progress=messages.append))
        self.assertTrue(any("Radware Bot Manager CAPTCHA" in message for message in messages))

    def test_mp_official_csv_has_exact_dates_and_excludes_procurement(self) -> None:
        module = importlib.import_module("services.state_updates.mp")
        content = '''intro,,,,,\nTransmittal Number,Approval Date,Effective Date,Topics,Summary,Links\nMP-26-0001,05/14/2026,04/01/2026,Coverage,Medicaid disaster policy,Approval\nMP-26-0002,05/15/2026,04/02/2026,Procurement,Medicaid contract RFP,Approval\n'''
        with mock.patch.object(module, "fetch_text", return_value=content) as fetch:
            records = module.fetch_updates(keywords=["Medicaid"], max_records=5)
        self.assertEqual(1, len(records))
        self.assertEqual(STATE_UPDATE_FIELDS, list(records[0]))
        self.assertEqual("2026-05-14", records[0]["posted_date"])
        self.assertEqual("2026-04-01", records[0]["effective_date"])
        self.assertEqual(module.LISTING_URL, records[0]["document_url"])
        self.assertEqual({"timeout": 20, "byte_limit": 500_000}, fetch.call_args.kwargs)

    def test_vi_news_cards_keep_exact_date_and_document_permalink(self) -> None:
        module = importlib.import_module("services.state_updates.vi")
        markup = '''
          <div class="news-card">06/29/26
            <h3>Medicaid Program Public Notice: Provider Enrollment Policy Manual Public Comment</h3>
            <a href="/uploads/provider-policy.pdf">READ MORE</a>
          </div>
          <div>06/30/26 <h3>Medicaid system contract RFP</h3><a href="/uploads/rfp.pdf">READ MORE</a></div>
        '''
        with mock.patch.object(module, "fetch_text", return_value=markup):
            records = module.fetch_updates(keywords=[], max_records=5)
        self.assertEqual(1, len(records))
        self.assertEqual(STATE_UPDATE_FIELDS, list(records[0]))
        self.assertEqual("2026-06-29", records[0]["posted_date"])
        self.assertEqual("https://dhs.vi.gov/uploads/provider-policy.pdf", records[0]["document_url"])
        self.assertEqual("true", records[0]["comment_required_flag"])


if __name__ == "__main__":
    unittest.main()
