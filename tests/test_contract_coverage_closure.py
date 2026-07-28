import datetime as dt
import json
import unittest
from unittest import mock
import urllib.parse

from services.state_contracts import ct, ia, nd
from services.state_contracts.store import STATE_CONTRACT_FIELDS
from services.state_http import HttpResult


class ContractCoverageClosureTests(unittest.TestCase):
    @staticmethod
    def dotnet(value: dt.date) -> str:
        stamp = dt.datetime(value.year, value.month, value.day, tzinfo=dt.timezone.utc).timestamp()
        return f"/Date({int(stamp * 1000)})/"

    def awarded_row(self, **changes):
        today = dt.date.today()
        row = {
            "ID": "3c3d0511-dbaa-47d1-9b54-a3d3c2b7d889",
            "Number": "2023-BUS-0201",
            "IsActive": True,
            "VendorName": "Acme Health",
            "ProductService": "Medicaid provider services",
            "Description": "Current awarded contract",
            "EffectiveDate": self.dotnet(today - dt.timedelta(days=30)),
            "ExpirationDate": self.dotnet(today + dt.timedelta(days=365)),
        }
        row.update(changes)
        return row

    def test_iowa_normalizes_exact_schema_dates_scoring_and_detail_url(self):
        record = ia.normalize_row(
            self.awarded_row(), vendor_terms=["Acme"], keywords=["Medicaid"]
        )
        self.assertEqual(list(record), STATE_CONTRACT_FIELDS)
        self.assertEqual(record["state"], "IA")
        self.assertEqual(record["contract_number"], "2023-BUS-0201")
        self.assertEqual(record["vendor_query"], "Acme")
        self.assertEqual(record["start_date"], (dt.date.today() - dt.timedelta(days=30)).isoformat())
        self.assertEqual(record["end_date"], (dt.date.today() + dt.timedelta(days=365)).isoformat())
        self.assertGreaterEqual(int(record["relevance_score"]), 60)
        self.assertIn(urllib.parse.quote(self.awarded_row()["ID"]), record["document_url"])
        raw = json.loads(record["raw_json"])
        self.assertEqual(raw["source_key"], "ia_das_awarded_contracts")

    def test_iowa_requires_explicit_active_stable_award_and_rejects_expired(self):
        kwargs = {"vendor_terms": ["Acme"], "keywords": ["Medicaid"]}
        self.assertEqual(ia.normalize_row(self.awarded_row(IsActive=False), **kwargs), {})
        self.assertEqual(ia.normalize_row(self.awarded_row(ID=""), **kwargs), {})
        self.assertEqual(ia.normalize_row(self.awarded_row(Number=""), **kwargs), {})
        yesterday = dt.date.today() - dt.timedelta(days=1)
        self.assertEqual(
            ia.normalize_row(self.awarded_row(ExpirationDate=self.dotnet(yesterday)), **kwargs),
            {},
        )
        self.assertEqual(ia.dotnet_date("not-a-date"), "")

    def test_iowa_fetch_is_bounded_and_hermetic(self):
        payload = json.dumps({"sEcho": 1, "iTotalRecords": 1, "aaData": [self.awarded_row()]})
        response = HttpResult(
            requested_url=ia.API_URL,
            final_url=ia.API_URL,
            status_code=200,
            content_type="application/json",
            body=payload.encode(),
            truncated=False,
        )
        with mock.patch.object(ia, "fetch_url", return_value=response) as fetch:
            records = ia.fetch_contracts(
                vendor_terms=["Acme"], keywords=["Medicaid"], max_per_vendor=1
            )
        self.assertEqual(len(records), 1)
        self.assertEqual(fetch.call_count, 2)
        for call in fetch.call_args_list:
            self.assertEqual(call.kwargs["timeout"], 30)
            self.assertEqual(call.kwargs["byte_limit"], 2_000_000)
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(call.args[0]).query)
            self.assertLessEqual(int(query["iDisplayLength"][0]), ia.MAX_PAGE_SIZE)
            self.assertEqual(query["iDisplayStart"], ["0"])

    def test_iowa_failed_term_preserves_other_results_and_continues(self):
        rows_by_term = {
            "Acme": [self.awarded_row()],
            "Beta": [
                self.awarded_row(
                    ID="d172ed39-4416-4b43-b741-4390d34dca10",
                    Number="2025-BUS-0099",
                    VendorName="Beta Health",
                )
            ],
        }
        messages = []

        def fetch_rows(term, *, limit):
            self.assertEqual(limit, 10)
            if term == "Broken":
                raise RuntimeError("HTTP request failed (500)")
            return rows_by_term[term]

        with mock.patch.object(ia, "fetch_rows", side_effect=fetch_rows) as fetch:
            records = ia.fetch_contracts(
                vendor_terms=["Acme", "Broken", "Beta"],
                keywords=[],
                max_per_vendor=1,
                progress=messages.append,
            )

        self.assertEqual([call.args[0] for call in fetch.call_args_list], ["Acme", "Broken", "Beta"])
        self.assertEqual({record["contract_number"] for record in records}, {"2023-BUS-0201", "2025-BUS-0099"})
        self.assertTrue(all(list(record) == STATE_CONTRACT_FIELDS for record in records))
        self.assertTrue(any("query='Broken': skipped after HTTP request failed (500)" in message for message in messages))
        self.assertTrue(any("query='Beta': scanned 1" in message for message in messages))

    def test_conclusive_blocked_adapters_are_documented_network_free_noops(self):
        for module, marker in ((ct, "access-control"), (nd, "reCAPTCHA")):
            messages = []
            with mock.patch("services.state_http.fetch_url") as fetch:
                self.assertEqual(
                    module.fetch_contracts(
                        vendor_terms=["Acme"],
                        keywords=["Medicaid"],
                        max_per_vendor=1,
                        progress=messages.append,
                    ),
                    [],
                )
            fetch.assert_not_called()
            self.assertIn(marker, module.BLOCKED_REASON)
            self.assertIn(module.SOURCE_URL, messages[0])


if __name__ == "__main__":
    unittest.main()
