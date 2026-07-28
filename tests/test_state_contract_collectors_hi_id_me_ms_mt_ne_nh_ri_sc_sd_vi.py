import datetime as dt
import importlib
import unittest
from unittest import mock

from services.state_contracts import id as id_contracts
from services.state_contracts.store import STATE_CONTRACT_FIELDS
from services.state_http import HttpResult


BLOCKED_MODULES = {
    state: importlib.import_module(f"services.state_contracts.{state.lower()}")
    for state in ("HI", "ME", "MS", "MT", "NE", "NH", "RI", "SC", "SD", "VI")
}

CARD_HTML = b"""
<div class="contract-card"><div class="card">
  <h2><a class="contract-title-link" href="https://purchasing.idaho.gov/statewide-contract/acme/">Acme Health LLC</a></h2>
  <p class="contract-details"><span class="contract-status status-active">Active</span> | 42 | PADD-1</p>
  <div class="contract-portfolio-pills"><span class="portfolio-pill">Health IT Services</span></div>
  <div class="description">Medicaid platform services</div>
</div></div>
"""


def detail_html(end_date: str, status: str = "Active") -> bytes:
    return f"""
    <div>Luma Contract Number: 42</div><div>DOP Contract Number: PADD-1</div>
    <div>Effective Date: 01/02/2025</div><div>Expiration Date: {end_date}</div>
    <div>Status: <span>{status}</span></div>
    """.encode()


def response(url: str, body: bytes, *, truncated: bool = False) -> HttpResult:
    return HttpResult(url, url, 200, "text/html", body, truncated)


class IdahoContractCollectorTests(unittest.TestCase):
    def test_parses_official_card_and_detail_into_exact_schema(self):
        future = (dt.date.today() + dt.timedelta(days=500)).strftime("%m/%d/%Y")
        parser = id_contracts.ContractCardParser()
        parser.feed(CARD_HTML.decode())
        self.assertEqual(parser.rows[0]["vendor_name"], "Acme Health LLC")

        with mock.patch.object(id_contracts, "fetch_url", side_effect=[
            response(id_contracts.AJAX_URL, CARD_HTML),
            response(parser.rows[0]["url"], detail_html(future)),
        ]) as fetch:
            records = id_contracts.fetch_contracts(
                vendor_terms=["Acme"], keywords=[], max_per_vendor=2
            )

        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(list(record), STATE_CONTRACT_FIELDS)
        self.assertEqual(record["state"], "ID")
        self.assertEqual(record["contract_number"], "PADD-1")
        self.assertEqual(record["vendor_name"], "Acme Health LLC")
        self.assertEqual(record["start_date"], "2025-01-02")
        self.assertEqual(record["end_date"], dt.datetime.strptime(future, "%m/%d/%Y").date().isoformat())
        self.assertEqual(record["contract_record_type"], "master_agreement")
        search_call = fetch.call_args_list[0]
        self.assertEqual(search_call.kwargs["timeout"], 20)
        self.assertEqual(search_call.kwargs["byte_limit"], 1_000_000)
        self.assertEqual(search_call.kwargs["data"]["contract_status"], "active")
        self.assertLessEqual(int(search_call.kwargs["data"]["posts_per_page"]), id_contracts.MAX_RESULTS_PER_TERM)
        self.assertEqual(fetch.call_args_list[1].kwargs["byte_limit"], 500_000)

    def test_rejects_nonactive_and_expired_and_handles_placeholder_safely(self):
        base = {
            "vendor_name": "Acme Health LLC", "details": "Active | 42 | PADD-1",
            "portfolio": "Health IT", "description": "Medicaid", "url": "https://purchasing.idaho.gov/statewide-contract/acme/",
            "luma_contract_number": "42", "dop_contract_number": "PADD-1",
            "effective_date": "01/02/2025", "detail_status": "Active",
        }
        expired = {**base, "expiration_date": (dt.date.today() - dt.timedelta(days=1)).isoformat()}
        self.assertEqual(id_contracts.normalize_row(expired, vendor_terms=["Acme"], keywords=[]), {})
        self.assertEqual(id_contracts.normalize_row({**base, "expiration_date": "", "detail_status": "Expired"}, vendor_terms=["Acme"], keywords=[]), {})

        placeholder = id_contracts.normalize_row({**base, "expiration_date": "2099-12-31"}, vendor_terms=["Acme"], keywords=[])
        self.assertEqual((placeholder["end_date"], placeholder["months_to_end"], placeholder["recompete_signal"]),
                         ("", "", "Open-ended/placeholder end date"))

    def test_truncated_search_fails_closed_and_untrusted_detail_url_is_not_fetched(self):
        with mock.patch.object(id_contracts, "fetch_url", return_value=response(id_contracts.AJAX_URL, CARD_HTML, truncated=True)):
            with self.assertRaisesRegex(RuntimeError, "byte limit"):
                id_contracts.fetch_cards("Acme", 2)
        with mock.patch.object(id_contracts, "fetch_url") as fetch:
            self.assertEqual(id_contracts.fetch_detail("https://example.test/not-official"), {})
            fetch.assert_not_called()


class ContractSourceClassificationTests(unittest.TestCase):
    def test_blocked_classifications_are_explicit_hermetic_collectors(self):
        for state, module in BLOCKED_MODULES.items():
            messages = []
            self.assertTrue(module.SOURCE_URL.startswith("https://"), state)
            self.assertTrue(module.BLOCKED_REASON, state)
            with mock.patch("services.state_http.fetch_url", side_effect=AssertionError("network attempted")):
                self.assertEqual(module.fetch_contracts(
                    vendor_terms=["Acme"], keywords=["Medicaid"], max_per_vendor=1,
                    progress=messages.append,
                ), [], state)
            self.assertEqual(len(messages), 1, state)
            self.assertIn(state, messages[0])
            self.assertIn(module.BLOCKED_REASON, messages[0])


if __name__ == "__main__":
    unittest.main()
