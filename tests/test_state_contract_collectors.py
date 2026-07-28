import datetime as dt
import io
import json
import unittest
from unittest import mock
import zipfile

from services.state_contracts import STATE_CLIENTS
from services.state_contracts import de, ga, ks, md, mn, mo, nc, ny, va, wa
from services.state_contracts.store import STATE_CONTRACT_FIELDS
from services.state_contracts.tabular import limit_records, normalized_end_date_fields, normalize_awarded_record, relevance_score
from services.state_opportunities.ga import GeorgiaGprClient


# `in` cannot appear in a normal Python import statement.
in_ = __import__("services.state_contracts.in", fromlist=["fetch_contracts"])


class StateContractCollectorTests(unittest.TestCase):
    def assert_contract(self, record, state, number, vendor="Acme"):
        self.assertEqual(list(record), STATE_CONTRACT_FIELDS)
        self.assertEqual(record["state"], state)
        self.assertEqual(record["contract_number"], number)
        self.assertEqual(record["vendor_name"], vendor)

    @staticmethod
    def future_date(days=365):
        return (dt.date.today() + dt.timedelta(days=days)).isoformat()

    @staticmethod
    def excel_serial(value):
        return str((value - dt.date(1899, 12, 30)).days)

    def test_delaware_awarded_vendor_row(self):
        row = {"contracttitle": "Medicaid platform", "companyname": "Acme", "contracturl": {"url": "https://mmp.delaware.gov/Contracts/Details/42"}, "expiredate": self.future_date(), "awarded_vendor": "Y"}
        self.assert_contract(de.normalize_row(row, vendor_terms=["Acme"], keywords=["Medicaid"]), "DE", "42")

    def test_exact_expiration_and_negative_months_do_not_score(self):
        normalized, months, signal, expired = normalized_end_date_fields("2026-07-01", today=dt.date(2026, 7, 28))
        self.assertEqual((normalized, months, signal, expired), ("2026-07-01", 0, "Expired/past award", True))
        self.assertEqual(relevance_score([], [], -1, "ordinary contract"), relevance_score([], [], None, "ordinary contract"))
        self.assertEqual(
            normalized_end_date_fields("2076-08-01", today=dt.date(2026, 7, 28)),
            ("", None, "Open-ended/placeholder end date", False),
        )

        yesterday = (dt.date.today() - dt.timedelta(days=1)).isoformat()
        row = {"contracttitle": "Medicaid platform", "companyname": "Acme", "contracturl": {"url": "https://example.test/42"}, "expiredate": yesterday, "awarded_vendor": "Y"}
        self.assertEqual(de.normalize_row(row, vendor_terms=["Acme"], keywords=["Medicaid"]), {})

    def test_placeholder_date_is_blank_retains_raw_and_sorts_below_real_date(self):
        placeholder = normalize_awarded_record(
            {"end": "2099-12-31"}, state="ZZ", source="test", source_key="test", source_note="test", source_url="https://example.test",
            vendor_terms=["Acme"], keywords=[], contract_number="P", title="Platform", vendor_name="Acme", end_date="2099-12-31",
        )
        real = normalize_awarded_record(
            {"end": self.future_date(400)}, state="ZZ", source="test", source_key="test", source_note="test", source_url="https://example.test",
            vendor_terms=["Acme"], keywords=[], contract_number="R", title="Platform", vendor_name="Acme", end_date=self.future_date(400),
        )
        self.assertEqual(placeholder["end_date"], "")
        self.assertEqual(placeholder["months_to_end"], "")
        self.assertEqual(placeholder["recompete_signal"], "Open-ended/placeholder end date")
        self.assertEqual(json.loads(placeholder["raw_json"])["row"]["end"], "2099-12-31")
        placeholder["relevance_score"] = real["relevance_score"]
        self.assertEqual([row["contract_number"] for row in limit_records([placeholder, real], 2, ["Acme"])], ["R", "P"])

    def test_shared_limit_is_per_vendor_term_and_keyword_discovery_is_not_vendor(self):
        records = []
        for query, suffix in (("Acme", "a1"), ("Acme", "a2"), ("Beta", "b1"), ("Beta", "b2"), ("", "k1"), ("", "k2")):
            records.append({"id": suffix, "vendor_query": query, "relevance_score": "10", "end_date": "2027-01-01"})
        limited = limit_records(records, 1, ["Acme", "Beta"])
        self.assertEqual(len(limited), 3)
        self.assertEqual({row["vendor_query"] for row in limited}, {"Acme", "Beta", ""})

        keyword_only = normalize_awarded_record(
            {}, state="ZZ", source="test", source_key="test", source_note="test", source_url="https://example.test",
            vendor_terms=["Acme"], keywords=["Medicaid"], contract_number="K", title="Medicaid services", vendor_name="Other",
        )
        self.assertTrue(keyword_only)
        self.assertEqual(keyword_only["vendor_query"], "")

    def test_defensive_awarded_and_current_checks(self):
        de_row = {"contracttitle": "Medicaid", "companyname": "Acme", "contracturl": {"url": "https://example.test/1"}, "expiredate": self.future_date(), "awarded_vendor": "N"}
        self.assertEqual(de.normalize_row(de_row, vendor_terms=["Acme"], keywords=["Medicaid"]), {})
        md_row = {"Status": "Open", "Description": "Medicaid", "Vendor": "Acme", "BPO No": "001B1234567", "Award End Date": self.future_date()}
        self.assertEqual(md.normalize_row(md_row, vendor_terms=["Acme"], keywords=["Medicaid"]), {})
        ny_row = {"status": "Completed", "contract_number": "T1", "project_number": "P1", "title": "Medicaid", "vendor_name": "Acme", "contract_end_date": self.future_date()}
        self.assertEqual(ny.normalize_row(ny_row, vendor_terms=["Acme"], keywords=["Medicaid"]), {})

    def test_georgia_rejects_non_awarded_event_and_bounds_reads(self):
        row = {"status": "Open", "esourceNumber": "GA-1", "title": "Medicaid platform"}
        self.assertEqual(ga.normalize_row(row, vendor_terms=[], keywords=["Medicaid"]), {})

        class Response:
            headers = {}
            status = 200
            def __init__(self): self.read_size = None
            def __enter__(self): return self
            def __exit__(self, *args): return None
            def read(self, size): self.read_size = size; return b"{}"
            def geturl(self): return "https://example.test"
            def getcode(self): return 200

        response = Response()
        client = GeorgiaGprClient()
        client.opener.open = mock.Mock(return_value=response)
        self.assertEqual(client._request_text("https://example.test", referer="https://example.test", byte_limit=99)[0], "{}")
        self.assertEqual(response.read_size, 100)

    def test_maryland_award_fields(self):
        row = {"Status": "Awarded", "Description": "Medicaid platform", "Vendor": "Acme", "BPO No": "001B1234567.pdf", "Award Start Date": "1/2/2025", "Award End Date": self.future_date(), "links": []}
        record = md.normalize_row(row, vendor_terms=["Acme"], keywords=["Medicaid"])
        self.assert_contract(record, "MD", "001B1234567")
        self.assertEqual(record["start_date"], "2025-01-02")

    def test_north_carolina_table_parser_and_vendor_split(self):
        parser = nc.ContractTableParser()
        parser.feed("<table><tr><th>Title</th><th>Beginning Date</th><th>Ending Date</th><th>Mandatory / Convenience</th><th>Contract Manager</th><th>Awarded Vendor(s)</th><th>Related Content</th></tr><tr><td><a href='/contract/1'>123 - Medicaid platform</a></td><td>1/2/2025</td><td>3/4/2027</td><td>Mandatory</td><td>Buyer</td><td><ul><li>Acme</li><li>Beta</li></ul></td><td></td></tr></table>")
        self.assertEqual(nc.split_vendors(parser.rows[1][5]["text"]), ["Acme", "Beta"])

    def test_new_york_and_virginia_require_ids_and_avoid_collisions(self):
        ny_row = {"status": "Active", "contract_number": "T1", "project_number": "P1", "title": "Medicaid platform", "vendor_name": "Acme", "contract_award_date": "2025-01-01", "contract_end_date": self.future_date()}
        self.assert_contract(ny.normalize_row(ny_row, vendor_terms=["Acme"], keywords=["Medicaid"]), "NY", "T1")
        self.assertEqual(ny.normalize_row({**ny_row, "project_number": ""}, vendor_terms=["Acme"], keywords=["Medicaid"]), {})

        va_row = {"Order #": "P1", "Order Line Number": "1", "Contract Number": "C1", "Item Description": "Medicaid platform", "Vendor Name": "Acme", "Ordered Date": "2025-01-01"}
        record = va.normalize_row(va_row, vendor_terms=["Acme"], keywords=["Medicaid"])
        self.assert_contract(record, "VA", "C1")
        self.assertEqual(record["contract_record_type"], "purchase_order")
        self.assertIn("C1", record["source_record_id"])
        other = va.normalize_row({**va_row, "Contract Number": "C2"}, vendor_terms=["Acme"], keywords=["Medicaid"])
        self.assertNotEqual(record["id"], other["id"])
        self.assertEqual(va.normalize_row({**va_row, "Order Line Number": ""}, vendor_terms=["Acme"], keywords=["Medicaid"]), {})

    def test_missouri_bounds_expanded_xlsx_entries(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("xl/worksheets/sheet1.xml", "x" * 100)
        with mock.patch.object(mo, "MAX_EXPANDED_ENTRY_BYTES", 64):
            with self.assertRaisesRegex(RuntimeError, "expanded byte limit"):
                mo.parse_xlsx_rows(buffer.getvalue())

    def test_active_in_mo_wa_normalizers_preserve_contract_schema(self):
        future = dt.date.today() + dt.timedelta(days=365)
        in_row = {"id": "IN-1", "vendorName": "Acme", "endDate": future.isoformat(), "pdfUrl": "https://example.test/in.pdf", "agencyName": "FSSA", "actionType": "Contract"}
        self.assert_contract(in_.normalize_row(in_row, vendor_terms=["Acme"], keywords=[]), "IN", "IN-1")

        mo_row = {"Contract Number": "MO-1", "Contractor Name": "Acme", "Contract Title": "Medicaid services", "Agency": "DSS", "Contract Expiration Date": self.excel_serial(future)}
        self.assert_contract(mo.normalize_row(mo_row, vendor_terms=["Acme"], keywords=["Medicaid"]), "MO", "MO-1")

        wa_row = {"agency_contract_no": "WA-1", "contractor_name_search_for": "Acme", "agency_number_agency_name": "HCA", "purpose_of_the_contract": "Medicaid services", "contract_effective_end_date": future.isoformat()}
        self.assert_contract(wa.normalize_row(wa_row, vendor_terms=["Acme"], keywords=["Medicaid"]), "WA", "WA-1")

    def test_active_normalizers_reject_exactly_expired_rows_and_neutralize_placeholders(self):
        yesterday = dt.date.today() - dt.timedelta(days=1)
        in_row = {"id": "IN-1", "vendorName": "Acme", "endDate": yesterday.isoformat(), "pdfUrl": "https://example.test/in.pdf"}
        self.assertIsNone(in_.normalize_row(in_row, vendor_terms=["Acme"], keywords=[]))
        mo_row = {"Contract Number": "MO-1", "Contractor Name": "Acme", "Contract Title": "Medicaid", "Contract Expiration Date": self.excel_serial(yesterday)}
        self.assertIsNone(mo.normalize_row(mo_row, vendor_terms=["Acme"], keywords=["Medicaid"]))
        wa_row = {"agency_contract_no": "WA-1", "contractor_name_search_for": "Acme", "purpose_of_the_contract": "Medicaid", "contract_effective_end_date": yesterday.isoformat()}
        self.assertIsNone(wa.normalize_row(wa_row, vendor_terms=["Acme"], keywords=["Medicaid"]))

        in_row.update(endDate="2099-01-01")
        placeholder = in_.normalize_row(in_row, vendor_terms=["Acme"], keywords=[])
        self.assertEqual((placeholder["end_date"], placeholder["months_to_end"], placeholder["recompete_signal"]), ("", "", "Open-ended/placeholder end date"))

    def test_blocked_adapters_are_not_registered(self):
        blocked = {"KS": ks, "MN": mn}
        for state, module in blocked.items():
            messages = []
            self.assertEqual(module.fetch_contracts(vendor_terms=[], keywords=[], max_per_vendor=1, progress=messages.append), [])
            self.assertTrue(module.BLOCKED_REASON)
            self.assertNotIn(state, STATE_CLIENTS)
        for state in {"DE", "GA", "IN", "MD", "MO", "NC", "NY", "VA", "WA"}:
            self.assertIn(state, STATE_CLIENTS)


if __name__ == "__main__":
    unittest.main()
