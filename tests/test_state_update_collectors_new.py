import unittest
from unittest import mock

from services.state_updates import STATE_CLIENTS, ga, ia, ky, la, nc, ny, wv
from services.state_updates.common import is_procurement_update, source_id_from_url
from services.state_updates.store import STATE_UPDATE_FIELDS


NEW_STATES = ("GA", "HI", "IA", "IN", "KY", "LA", "MN", "NC", "NM", "NY", "OH", "WA", "WI", "WV")
INTEGRATED_STATES = tuple(state for state in NEW_STATES if state not in {"MN", "OH", "WI"})


class NewStateUpdateCollectorTests(unittest.TestCase):
    def test_nc_bulletin_parser_and_procurement_exclusion(self):
        html = '''
        <div class="views-row"><div><a href="/blog/2026/01/02/coverage-update">Coverage Update</a></div>
        <div><time datetime="2026-01-02T12:00:00Z">January 2</time></div>
        <div class="field-content">NC Medicaid coverage policy changed.</div></div></div>
        <div class="views-row"><div><a href="/bid">Request for Proposal</a></div>
        <div><time datetime="2026-01-03T12:00:00Z">January 3</time></div>
        <div class="field-content">Procurement notice.</div></div></div>'''
        rows = nc.parse_bulletins(html)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["posted_date"], "2026-01-02")

    def test_ga_public_notice_parser_excludes_procurement(self):
        html = '''<ul>
        <li><a href="/document/rural/download" data-text="Public Rural Hospital Medicaid Payment Notice">notice</a> - Posted 7/14/2026</li>
        <li><a href="/document/rfp/download" data-text="Medicaid Procurement Request for Proposal">notice</a> - Posted 7/15/2026</li>
        </ul>'''
        rows = ga.parse_public_notices(html)
        self.assertEqual([row["title"] for row in rows], ["Public Rural Hospital Medicaid Payment Notice"])
        self.assertEqual(rows[0]["posted_date"], "2026-07-14")

    def test_procurement_matcher_has_boundaries_and_decodes_urls(self):
        for value in ("RFP", "RFQ", "ITB", "solicitation", "bid", "award", "procurement", "contract", "recompete"):
            with self.subTest(value=value):
                self.assertTrue(is_procurement_update(value))
        self.assertTrue(is_procurement_update("policy update", "", "https://example.gov/%52FQ/42"))
        self.assertFalse(is_procurement_update("contractor enrollment and awardee reporting"))

    def test_procurement_filter_applies_to_each_specialized_parser_field(self):
        ga_html = '<li><a href="/document/%52FP/download" data-text="Medicaid policy update">notice</a> - Posted 7/14/2026</li>'
        ky_html = '<a href="/agencies/dms/Documents/solicitation.pdf">Public Notice Medicaid Changes (June 22, 2026) - PDF</a>'
        nc_html = '''<div class="views-row"><div><a href="/policy">Coverage Update</a></div>
        <div><time datetime="2026-01-02T12:00:00Z">January 2</time></div>
        <div class="field-content">Medicaid contract award.</div></div></div>'''
        wv_html = '''<div style="padding-bottom:15px;"><b><a href="https://dhhr.wv.gov/News/Pages/%49TB.aspx">Medicaid policy update</a></b><br><i>12/29/2025</i><br>Provider implementation notice.<a title="Click here to read the full article">more</a></div>'''
        self.assertEqual([], ga.parse_public_notices(ga_html))
        self.assertEqual([], ky.parse_public_notices(ky_html))
        self.assertEqual([], nc.parse_bulletins(nc_html))
        self.assertEqual([], wv.parse_health_news(wv_html))

    def test_ny_current_issue_parser(self):
        html = '''<h2>Current Issue: May 2026</h2><ul>
        <li><a href="/update/may.htm">Web version</a></li>
        <li><a href="/update/may.pdf">Printer-Ready PDF version</a></li></ul>'''
        row = ny.parse_current_issue(html)
        self.assertEqual(row, {
            "issue": "May 2026", "posted_date": "2026-05-01",
            "web_url": "https://www.health.ny.gov/update/may.htm",
            "pdf_url": "https://www.health.ny.gov/update/may.pdf",
        })

    def test_ky_public_notice_parser(self):
        html = '''<a href="/agencies/dms/Documents/BudgetSPAPublicNotice.pdf">
        Public Notice Regarding State Plan Payment Changes (June 22, 2026) - PDF</a>'''
        rows = ky.parse_public_notices(html)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["posted_date"], "2026-06-22")

    def test_la_provider_update_parser(self):
        rows = la.parse_provider_updates('''<a href="provider_update_07_26.pdf">7/1/26</a>
        <a href="unrelated.pdf">7/2/26</a>''')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["title"], "Louisiana Medicaid Provider Update — 2026-07")

    def test_wv_health_news_parser_and_procurement_exclusion(self):
        html = '''
        <div style="padding-bottom:15px;"><b><a href="https://dhhr.wv.gov/News/Pages/rht.aspx">Rural Healthcare Transformation Update</a></b><br><i>12/29/2025</i><br>RHTP implementation funding update.<a title="Click here to read the full article">more</a></div>
        <div style="padding-bottom:15px;"><b><a href="https://dhhr.wv.gov/News/Pages/bid.aspx">Rural Hospital Procurement</a></b><br><i>12/30/2025</i><br>Request for proposal.<a title="Click here to read the full article">more</a></div>'''
        rows = wv.parse_health_news(html)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["posted_date"], "2025-12-29")

    def test_new_collectors_preserve_exact_ordered_schema(self):
        markup = '<a href="provider_update_07_26.pdf">7/1/26</a>'
        with mock.patch.object(la, "fetch_text", return_value=markup):
            records = la.fetch_updates(keywords=["Medicaid"], max_records=1)
        self.assertEqual(list(records[0]), STATE_UPDATE_FIELDS)

    def test_all_new_collectors_return_zero_without_network(self):
        for state in NEW_STATES:
            with self.subTest(state=state):
                module = __import__(f"services.state_updates.{state.lower()}", fromlist=["fetch_updates"])
                patcher = (
                    mock.patch.object(module, "fetch_text", side_effect=AssertionError("network attempted"))
                    if hasattr(module, "fetch_text")
                    else mock.patch.object(module, "fetch_updates", wraps=module.fetch_updates)
                )
                with patcher:
                    self.assertEqual([], module.fetch_updates(keywords=[], max_records=0))
                    self.assertEqual([], module.fetch_updates(keywords=[], max_records=-1))

    def test_only_verified_new_collectors_are_present_in_current_registry(self):
        self.assertEqual(list(INTEGRATED_STATES), [state for state in NEW_STATES if state in STATE_CLIENTS])

    def test_source_ids_hash_identity_query_strings(self):
        first = source_id_from_url("https://agency.gov/download?id=123")
        second = source_id_from_url("https://agency.gov/download?id=456")
        self.assertNotEqual(first, second)
        self.assertIn("query-", first)
        self.assertNotIn("123", first)

    def test_ia_trusted_provider_letter_context_is_retained(self):
        markup = '<a href="/media/9999/download?inline=">IL 2526-MC-FFS-D</a>'
        sources = [("ia_letters", "https://hhs.iowa.gov/provider-letters", "provider_bulletin", ["il 25"])]
        with mock.patch.object(ia, "SOURCES", sources), mock.patch.object(ia, "fetch_text", return_value=markup):
            records = ia.fetch_updates(keywords=[], max_records=1)
        self.assertEqual(1, len(records))
        self.assertEqual("IL 2526-MC-FFS-D", records[0]["title"])
        self.assertEqual("https://hhs.iowa.gov/media/9999/download?inline=", records[0]["document_url"])
        self.assertEqual(STATE_UPDATE_FIELDS, list(records[0]))


if __name__ == "__main__":
    unittest.main()
