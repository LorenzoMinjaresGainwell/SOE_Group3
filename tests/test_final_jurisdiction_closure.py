from __future__ import annotations

import importlib
import unittest
from unittest import mock

from services.state_contracts import gu as gu_contracts
from services.state_contracts import mp as mp_contracts
from services.state_contracts import oh as oh_contracts
from services.state_contracts import wi as wi_contracts
from services.state_updates import gu as gu_updates
from services.state_updates import mp as mp_updates
from services.state_updates import oh as oh_updates
from services.state_updates import wi as wi_updates
from services.state_updates.store import STATE_UPDATE_FIELDS

as_contracts = importlib.import_module("services.state_contracts.as")
as_updates = importlib.import_module("services.state_updates.as")


CONTRACT_MODULES = (
    oh_contracts,
    wi_contracts,
    as_contracts,
    gu_contracts,
    mp_contracts,
)
UPDATE_NOOPS = (oh_updates, wi_updates, as_updates, gu_updates)


class FinalJurisdictionClosureTests(unittest.TestCase):
    def test_contract_classifications_are_documented_network_free_noops(self) -> None:
        for module in CONTRACT_MODULES:
            messages: list[str] = []
            with self.subTest(module=module.__name__), mock.patch(
                "services.state_http.fetch_url", side_effect=AssertionError("network attempted")
            ) as fetch:
                records = module.fetch_contracts(
                    vendor_terms=["Gainwell"],
                    keywords=["Medicaid"],
                    max_per_vendor=5,
                    progress=messages.append,
                )
            fetch.assert_not_called()
            self.assertEqual([], records)
            self.assertTrue(module.SOURCE_URL.startswith("https://"))
            self.assertIn("public", module.BLOCKED_REASON)
            self.assertIn(module.SOURCE_URL, messages[0])

    def test_update_classifications_are_documented_network_free_noops(self) -> None:
        for module in UPDATE_NOOPS:
            messages: list[str] = []
            with self.subTest(module=module.__name__), mock.patch(
                "services.state_http.fetch_url", side_effect=AssertionError("network attempted")
            ) as fetch:
                records = module.fetch_updates(
                    keywords=["Medicaid"], max_records=5, progress=messages.append
                )
            fetch.assert_not_called()
            self.assertEqual([], records)
            self.assertIn("stable public", module.BLOCKED_REASON)
            self.assertTrue(messages)

    def test_mp_official_csv_normalizes_exact_update_schema(self) -> None:
        content = """CNMI Medicaid State Plan Amendments,,,,,
Transmittal Number,Approved Date,Effective Date,Topics,Summary,Links
MP-25-0001,07/01/2025,01/01/2025,Medicaid eligibility,Eligibility policy update,View
MP-25-0002,07/02/2025,01/02/2025,Medicaid contract RFP,Procurement,View
"""
        with mock.patch.object(mp_updates, "fetch_text", return_value=content):
            records = mp_updates.fetch_updates(keywords=["Medicaid"], max_records=10)
        self.assertEqual(1, len(records))
        self.assertEqual(STATE_UPDATE_FIELDS, list(records[0]))
        self.assertEqual("MP", records[0]["state"])
        self.assertEqual("MP-25-0001", records[0]["source_record_id"])
        self.assertEqual("2025-07-01", records[0]["posted_date"])
        self.assertEqual("2025-01-01", records[0]["effective_date"])
        self.assertEqual(mp_updates.LISTING_URL, records[0]["source_url"])

    def test_mp_nonpositive_limit_is_network_free(self) -> None:
        with mock.patch.object(
            mp_updates, "fetch_text", side_effect=AssertionError("network attempted")
        ) as fetch:
            self.assertEqual([], mp_updates.fetch_updates(keywords=[], max_records=0))
        fetch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
