from __future__ import annotations

import unittest
from unittest import mock

from services.state_updates import STATE_CLIENTS, dc, de, ma, md, oh, wi
from services.state_updates.store import STATE_UPDATE_FIELDS


MODULES = (md, ma, oh, dc, de, wi)


class MidAtlanticAndWisconsinUpdateTests(unittest.TestCase):
    def test_md_sharepoint_rows_have_dates_permalinks_and_exact_schema(self) -> None:
        payload = {"value": [
            {"Id": 1264, "Title": "General Provider #124", "Date": "2026-07-12T04:00:00Z",
             "Topic": "Upcoming MPRIME Training Sessions", "DetailLink": {"Description": "PT 04-27", "Url": "https://health.maryland.gov/mmcp/provider/Documents/transmittals/PT04-27.pdf"}},
            {"Id": 1266, "Title": "General Provider", "Date": "2026-07-13T04:00:00Z",
             "Topic": "Medicaid contract RFP", "DetailLink": {"Description": "PT 07-27", "Url": "https://health.maryland.gov/mmcp/provider/Documents/transmittals/PT07-27.pdf"}},
        ]}
        with mock.patch.object(md, "fetch_json_data", return_value=payload):
            records = md.fetch_updates(keywords=["Medicaid"], max_records=10)
        self.assertEqual(1, len(records))
        self.assertEqual(STATE_UPDATE_FIELDS, list(records[0]))
        self.assertEqual("2026-07-12", records[0]["posted_date"])
        self.assertEqual("https://health.maryland.gov/mmcp/provider/Documents/transmittals/PT04-27.pdf", records[0]["document_url"])
        self.assertEqual("1264", records[0]["source_record_id"])

    def test_md_request_is_bounded(self) -> None:
        self.assertIn("%24top=100", md.api_url(10_000))
        self.assertNotIn("skiptoken", md.api_url(10_000).lower())

    def test_dc_newsroom_parser_keeps_policy_news_and_excludes_procurement(self) -> None:
        markup = '''
        <div class="views-row views-row-1">
          <span class="date-display-single" content="2026-05-08T00:00:00-04:00">05/08/2026</span>
          <div class="views-field views-field-title"><a href="/release/new-medicaid-regulations">New Regulations for Medicaid Beneficiaries</a></div>
        </div>
        <div class="views-row views-row-2">
          <span class="date-display-single" content="2026-05-09T00:00:00-04:00">05/09/2026</span>
          <div class="views-field views-field-title"><a href="/release/medicaid-rfp">Medicaid Contract RFP</a></div>
        </div><div class="item-list">pages</div>'''
        rows = dc.parse_newsroom(markup)
        self.assertEqual(["New Regulations for Medicaid Beneficiaries"], [row["title"] for row in rows])
        with mock.patch.object(dc, "fetch_text", return_value=markup):
            records = dc.fetch_updates(keywords=[], max_records=5)
        self.assertEqual(STATE_UPDATE_FIELDS, list(records[0]))
        self.assertEqual("2026-05-08", records[0]["posted_date"])
        self.assertEqual("https://dhcf.dc.gov/release/new-medicaid-regulations", records[0]["document_url"])

    def test_ma_and_de_simple_official_parsers_exclude_procurement(self) -> None:
        ma_markup = '''<a href="/doc/masshealth-provider-bulletin-2026-04.pdf">MassHealth Provider Bulletin April 4, 2026</a>
        <a href="/procurement/rfp.pdf">MassHealth Provider Bulletin RFP April 5, 2026</a>'''
        self.assertEqual(1, len(ma.parse_bulletins(ma_markup)))
        self.assertEqual("2026-04-04", ma.parse_bulletins(ma_markup)[0]["posted_date"])
        de_markup = '''<a href="/docs/waiver.pdf">Medicaid Waiver Public Notice June 1, 2026</a>
        <a href="/docs/contract.pdf">Medicaid Contract Award June 2, 2026</a>'''
        self.assertEqual(1, len(de.parse_updates(de_markup)))
        self.assertEqual("2026-06-01", de.parse_updates(de_markup)[0]["posted_date"])
        with mock.patch.object(ma, "fetch_text", return_value=ma_markup):
            ma_records = ma.fetch_updates(keywords=[], max_records=5)
        with mock.patch.object(de, "fetch_text", return_value=de_markup):
            de_records = de.fetch_updates(keywords=[], max_records=5)
        self.assertEqual(STATE_UPDATE_FIELDS, list(ma_records[0]))
        self.assertEqual(STATE_UPDATE_FIELDS, list(de_records[0]))
        self.assertEqual("https://www.mass.gov/doc/masshealth-provider-bulletin-2026-04.pdf", ma_records[0]["document_url"])
        self.assertEqual("https://medicaid.dhss.delaware.gov/docs/waiver.pdf", de_records[0]["document_url"])

    def test_all_six_nonpositive_limits_make_no_network_calls(self) -> None:
        for module in MODULES:
            with self.subTest(state=module.__name__.rsplit(".", 1)[-1].upper()):
                patches = []
                if hasattr(module, "fetch_text"):
                    patches.append(mock.patch.object(module, "fetch_text", side_effect=AssertionError("network attempted")))
                if hasattr(module, "fetch_json_data"):
                    patches.append(mock.patch.object(module, "fetch_json_data", side_effect=AssertionError("network attempted")))
                started = [patch.start() for patch in patches]
                try:
                    self.assertEqual([], module.fetch_updates(keywords=[], max_records=0))
                    self.assertEqual([], module.fetch_updates(keywords=[], max_records=-1))
                finally:
                    for patch in reversed(patches):
                        patch.stop()
                    del started

    def test_only_verified_collectors_are_registered(self) -> None:
        for state in ("MD", "DC"):
            self.assertIn(state, STATE_CLIENTS)
        for state in ("MA", "OH", "DE", "WI"):
            self.assertNotIn(state, STATE_CLIENTS)

    def test_blocked_collectors_isolate_source_failure(self) -> None:
        for module in (ma, de):
            messages: list[str] = []
            with self.subTest(module=module.__name__), mock.patch.object(module, "fetch_text", side_effect=RuntimeError("blocked")):
                self.assertEqual([], module.fetch_updates(keywords=[], max_records=5, progress=messages.append))
        self.assertTrue(messages)


if __name__ == "__main__":
    unittest.main()
