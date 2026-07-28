from __future__ import annotations

import importlib
import unittest
from unittest import mock

from services.state_updates.store import STATE_UPDATE_FIELDS


# OH and WI are now explicit unavailable-source no-ops covered by
# test_final_jurisdiction_closure.py rather than generic HTML collectors.
MODULES = ["in", "ia", "mn", "wa", "nm", "hi"]


class NewStateUpdateCollectorTests(unittest.TestCase):
    def test_collectors_normalize_official_links_hermetically(self) -> None:
        markup = """
            <a href="/files/medicaid-provider-bulletin-07-15-2026.pdf">
              Medicaid Provider Bulletin 07/15/2026
            </a>
            <a href="/procurement/medicaid-contract-07-16-2026.pdf">
              Medicaid managed care contract recompete
            </a>
            <a href="/about">About us</a>
        """
        for module_name in MODULES:
            with self.subTest(module=module_name):
                module = importlib.import_module(f"services.state_updates.{module_name}")
                source_url = f"https://agency.example.gov/{module_name}/updates"
                sources = [(f"{module_name}_test", source_url, "provider_bulletin", ["bulletin"])]
                with mock.patch.object(module, "SOURCES", sources), mock.patch.object(module, "fetch_text", return_value=markup):
                    records = module.fetch_updates(keywords=[], max_records=10)
                self.assertEqual(1, len(records))
                record = records[0]
                self.assertEqual(STATE_UPDATE_FIELDS, list(record))
                self.assertEqual(module_name.upper(), record["state"])
                self.assertEqual("provider_bulletin", record["record_type"])
                self.assertEqual("2026-07-15", record["posted_date"])
                self.assertEqual(source_url, record["source_url"])
                self.assertTrue(record["document_url"].endswith(".pdf"))
                self.assertNotIn("contract", record["title"].lower())

    def test_collectors_preserve_extensionless_query_permalinks(self) -> None:
        markup = '''
            <a href="/updates/item?id=123">Medicaid provider bulletin 07/15/2026</a>
            <a href="/updates/item?id=456">Medicaid provider bulletin 07/16/2026</a>
        '''
        for module_name in MODULES:
            with self.subTest(module=module_name):
                module = importlib.import_module(f"services.state_updates.{module_name}")
                source_url = f"https://agency.example.gov/{module_name}/updates"
                sources = [(f"{module_name}_test", source_url, "provider_bulletin", ["bulletin"])]
                with mock.patch.object(module, "SOURCES", sources), mock.patch.object(module, "fetch_text", return_value=markup):
                    records = module.fetch_updates(keywords=[], max_records=10)
                self.assertEqual(2, len(records))
                self.assertEqual(
                    {"https://agency.example.gov/updates/item?id=123", "https://agency.example.gov/updates/item?id=456"},
                    {record["document_url"] for record in records},
                )
                self.assertEqual(2, len({record["source_record_id"] for record in records}))

    def test_collectors_skip_challenge_pages_and_report(self) -> None:
        for module_name in MODULES:
            with self.subTest(module=module_name):
                module = importlib.import_module(f"services.state_updates.{module_name}")
                sources = [("blocked", "https://agency.example.gov/updates", "medicaid_notice", ["medicaid"])]
                messages: list[str] = []
                challenge = '<a href="https://validate.perfdrive.com/">Access validation</a>'
                with mock.patch.object(module, "SOURCES", sources), mock.patch.object(module, "fetch_text", return_value=challenge):
                    records = module.fetch_updates(keywords=[], max_records=10, progress=messages.append)
                self.assertEqual([], records)
                self.assertTrue(any("challenge page" in message for message in messages))

    def test_source_rows_exclude_contracts_and_recompetes(self) -> None:
        markup = (
            '<a href="notice.pdf">Medicaid waiver public notice 2026-06-01</a>'
            '<a href="contract.pdf">Medicaid contract recompete 2026-06-02</a>'
        )
        for module_name in MODULES:
            with self.subTest(module=module_name):
                module = importlib.import_module(f"services.state_updates.{module_name}")
                rows = module.source_rows(markup, "https://agency.example.gov/notices/", ["medicaid"])
                self.assertEqual(["Medicaid waiver public notice 2026-06-01"], [row["title"] for row in rows])


if __name__ == "__main__":
    unittest.main()
