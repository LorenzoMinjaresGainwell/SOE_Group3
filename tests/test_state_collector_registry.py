import contextlib
import io
import sys
import unittest
from unittest import mock

from scripts import state_contracts, state_opportunities, state_updates
from services.state_collector_registry import load_state_collector_config
from services.state_contracts import STATE_CLIENTS as CONTRACT_CLIENTS
from services.state_opportunities import STATE_CLIENTS as OPPORTUNITY_CLIENTS
from services.state_updates import STATE_CLIENTS as UPDATE_CLIENTS


EXPECTED_TAGS = {
    "opportunities": "AK AL AR AZ CA CO DC DE FL GA HI IA ID IL IN KS KY LA MA MD ME MI MO MS MT NC NE NJ NM NV NY OK OR PA PR RI SC SD TN TX UT VA VI VT WA WI WV WY".split(),
    "contracts": "AK AL AR AZ CA CO DC DE FL GA IA ID IL IN LA MA MD MI MO NC NJ NY OK OR PA PR TN TX UT VA VT WA WV WY".split(),
    "updates": "AK AL AR AZ CA CO CT DC FL GA HI IA ID IL IN KY LA MD ME MI MO MP MS MT NC ND NE NJ NM NV NY OK OR PA PR RI SC SD TN TX UT VA VI VT WA WV WY".split(),
}


class StateCollectorRegistryTests(unittest.TestCase):
    def test_config_counts_exact_tags_modules_and_runtime_registries(self):
        config = load_state_collector_config()
        clients = {
            "opportunities": OPPORTUNITY_CLIENTS,
            "contracts": CONTRACT_CLIENTS,
            "updates": UPDATE_CLIENTS,
        }
        self.assertEqual({family: len(tags) for family, tags in config.items()}, {
            "opportunities": 48,
            "contracts": 34,
            "updates": 47,
        })
        for family, expected in EXPECTED_TAGS.items():
            with self.subTest(family=family):
                self.assertEqual(expected, list(config[family]))
                self.assertEqual({tag: tag.lower() for tag in expected}, config[family])
                self.assertEqual(expected, list(clients[family]))
                for tag, fetcher in clients[family].items():
                    self.assertEqual(f"services.state_{family}.{config[family][tag]}", fetcher.__module__)

    def test_each_cli_requires_an_explicit_selection(self):
        for module in (state_opportunities, state_contracts, state_updates):
            with self.subTest(module=module.__name__), mock.patch.object(sys, "argv", [module.__name__]):
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr):
                    self.assertEqual(2, module.main())
                self.assertIn("Specify --states CSV or --all", stderr.getvalue())

    def test_each_cli_rejects_unsupported_tags(self):
        for module in (state_opportunities, state_contracts, state_updates):
            with self.subTest(module=module.__name__), mock.patch.object(
                sys, "argv", [module.__name__, "--states", "ZZ"]
            ):
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr):
                    self.assertEqual(2, module.main())
                self.assertIn("Unsupported or unregistered", stderr.getvalue())
                self.assertIn("ZZ", stderr.getvalue())

    def test_all_selects_every_configured_tag_for_each_family(self):
        cases = (
            (state_opportunities, "opportunities", ["--dry-run"], "fetch_state_opportunities"),
            (state_contracts, "contracts", ["--vendors", "Acme", "--dry-run"], "fetch_state_contracts"),
            (state_updates, "updates", ["--dry-run"], "fetch_state_updates"),
        )
        for module, family, trailing_arguments, fetch_name in cases:
            for selection in (["--all"], ["--states", "all"]):
                with self.subTest(family=family, selection=selection), mock.patch.object(
                    sys, "argv", [module.__name__, *selection, *trailing_arguments]
                ), mock.patch.object(module, fetch_name, return_value=[]) as fetch:
                    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                        self.assertEqual(0, module.main())
                    self.assertEqual(EXPECTED_TAGS[family], fetch.call_args.kwargs["states"])

    def test_all_state_tag_cannot_be_combined_with_other_tags(self):
        for module in (state_opportunities, state_contracts, state_updates):
            with self.subTest(module=module.__name__), mock.patch.object(
                sys, "argv", [module.__name__, "--states", "all,PA"]
            ):
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr):
                    self.assertEqual(2, module.main())
                self.assertIn("cannot be combined", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
