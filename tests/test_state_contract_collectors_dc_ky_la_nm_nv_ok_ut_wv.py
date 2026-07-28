import datetime as dt
import unittest
from unittest import mock

from services.state_contracts import dc, ky, la, nm, nv, ok, ut, wv
from services.state_contracts.store import STATE_CONTRACT_FIELDS


class TargetStateContractCollectorTests(unittest.TestCase):
    @staticmethod
    def future(days=365):
        return (dt.date.today() + dt.timedelta(days=days)).isoformat()

    def assert_schema(self, record, state, number, vendor):
        self.assertEqual(STATE_CONTRACT_FIELDS, list(record))
        self.assertEqual(state, record["state"])
        self.assertEqual(number, record["contract_number"])
        self.assertEqual(vendor, record["vendor_name"])

    def test_dc_official_api_row(self):
        row = {"id": "opaque-1", "contractNumber": "CW120781", "title": "Medicaid verification", "agencyNames": ["DHCF"], "vendor": "Acme Consulting", "contractAmount": "$1,200,000", "awardDate": "4/1/2025", "startDate": "4/1/2026", "endDate": self.future()}
        record = dc.normalize_row(row, vendor_terms=["Acme"], keywords=["Medicaid"])
        self.assert_schema(record, "DC", "CW120781", "Acme Consulting")
        self.assertEqual("1200000", record["amount"])
        self.assertEqual("opaque-1", record["source_record_id"])

    def test_dc_fetch_is_term_and_result_bounded(self):
        row = {"id": "1", "contractNumber": "C1", "title": "Medicaid", "vendor": "Acme", "endDate": self.future()}
        with mock.patch.object(dc, "fetch_rows", return_value=[row]) as fetch:
            records = dc.fetch_contracts(vendor_terms=["Acme"], keywords=["Medicaid"], max_per_vendor=1)
        self.assertEqual(1, len(records))
        self.assertEqual(2, fetch.call_count)

    def test_louisiana_current_contract_table(self):
        html = '''<table><tr><td>Contract #</td><td>Contract Description</td><td>Vendor Name</td><td>T-number</td><td>Effective From - To</td><td>P-card</td></tr>
        <tr><td><a href="dsp_LagovContractDetail.cfm?Contract=4401">4401</a></td><td>Medicaid software</td><td>Acme LLC</td><td>92911</td><td>06/19/2025 - 12/19/2028</td><td>No</td></tr></table>'''
        rows = la.parse_results(html)
        self.assertEqual(1, len(rows))
        record = la.normalize_row(rows[0], vendor_terms=["Acme"], keywords=["Medicaid"])
        self.assert_schema(record, "LA", "4401", "Acme LLC")
        self.assertEqual("2025-06-19", record["start_date"])

    def test_utah_cooperative_contract_table(self):
        html = '''<table><tr><th>Contract ID</th><th>Description</th><th>Contractor Name</th><th>Expiration</th><th></th></tr>
        <tr><td>PA5086</td><td>Medicaid Enterprise System</td><td>Acme Inc.</td><td>12/31/2028</td><td><a href="/Contract/Details/PA5086-x">View Details</a></td></tr></table>'''
        rows = ut.parse_results(html)
        self.assertEqual("https://statecontracts.utah.gov/Contract/Details/PA5086-x", rows[0]["detail_url"])
        record = ut.normalize_row(rows[0], vendor_terms=["Acme"], keywords=["Medicaid"])
        self.assert_schema(record, "UT", "PA5086", "Acme Inc.")

    def test_west_virginia_current_contract_table(self):
        html = '''<table><tr><th>Good or Services</th><th>Contract # and Change Orders</th><th>Ordering</th><th>Other</th><th>Awarded Vendor</th><th>Mandatory</th><th>CFR</th><th>Expiration Date</th><th>Status</th></tr>
        <tr><td>Medicaid services</td><td><a href="/admin/purchase/SWC/MED25.pdf">MED25</a></td><td></td><td></td><td>Acme LLC</td><td>M</td><td>Y</td><td>06/30/2028</td><td>N/A</td></tr></table>'''
        rows = wv.parse_results(html)
        record = wv.normalize_row(rows[0], vendor_terms=["Acme"], keywords=["Medicaid"])
        self.assert_schema(record, "WV", "MED25", "Acme LLC")
        self.assertEqual("WV Mandatory Contract", record["document_type"])

    def test_oklahoma_post_award_csv_has_neutral_missing_end_date(self):
        rows = ok.parse_csv("EVENT #,AWARDED SUPPLIER,CLOSED DATE,AWARD DATE,AGENCY NAME\nEV1,Acme LLC,1/1/2025,2/1/2025,Health Department\n")
        record = ok.normalize_row(rows[0], vendor_terms=["Acme"], keywords=[])
        self.assert_schema(record, "OK", "EV1", "Acme LLC")
        self.assertEqual("", record["end_date"])
        self.assertEqual("Unknown end date", record["recompete_signal"])

    def test_expired_rows_are_never_promoted(self):
        row = {"contract_id": "OLD", "title": "Medicaid", "vendor": "Acme", "end_date": (dt.date.today() - dt.timedelta(days=1)).isoformat()}
        self.assertEqual({}, ut.normalize_row(row, vendor_terms=["Acme"], keywords=["Medicaid"]))

    def test_zero_limits_are_network_free(self):
        active = (dc, la, ok, ut, wv)
        for module in active:
            with self.subTest(module=module.__name__), mock.patch.object(module, "fetch_rows", side_effect=AssertionError("network attempted")):
                self.assertEqual([], module.fetch_contracts(vendor_terms=["Acme"], keywords=["Medicaid"], max_per_vendor=0))

    def test_explicit_blocked_classifications(self):
        for module in (ky, nm, nv):
            messages = []
            self.assertEqual([], module.fetch_contracts(vendor_terms=[], keywords=[], max_per_vendor=1, progress=messages.append))
            self.assertTrue(module.BLOCKED_REASON)
            self.assertIn(module.SOURCE_URL, messages[0])


if __name__ == "__main__":
    unittest.main()
