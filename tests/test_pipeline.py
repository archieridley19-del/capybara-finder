"""End-to-end tests: anchors, generation, persistence, batching.

These run entirely offline against recorded fixtures, so they are safe in CI
and do not depend on live sites staying the same.
"""

import tempfile
import unittest
from pathlib import Path

from capyfind.discover import discover
from capyfind.generate import generate_candidates, match_vertical
from capyfind.models import run_from_dict
from capyfind.providers import build_providers
from capyfind.report import render_report, render_table, to_csv
from capyfind.scoring import ANCHORS
from capyfind.store import Store
from capyfind.verify import verify


def fixture_providers():
    return build_providers(use_fixtures=True)


class AnchorTests(unittest.TestCase):
    """The headline requirement: if the verifier cannot confidently reject
    'convert bank statement pdf to excel', it is not working yet."""

    def test_every_anchor_rejects(self):
        providers = fixture_providers()
        for phrase, expected in ANCHORS.items():
            with self.subTest(phrase=phrase):
                run = verify(phrase, providers, depth="deep")
                self.assertTrue(run.verified, f"{phrase} failed to retrieve")
                self.assertEqual(run.band, expected["expected_band"])
                self.assertEqual(run.score.composite, 0)

    def test_bank_statement_anchor_names_its_incumbent(self):
        run = verify(
            "convert bank statement pdf to excel", fixture_providers(), depth="deep"
        )
        self.assertTrue(run.polished_competitors)
        self.assertIn("docuclipper", run.kill_reason.lower())

    def test_listicles_are_excluded_from_the_teardown(self):
        run = verify(
            "convert bank statement pdf to excel", fixture_providers(), depth="deep"
        )
        domains = {c.domain for c in run.competitors}
        self.assertNotIn("techradar.com", domains)
        self.assertNotIn("g2.com", domains)
        self.assertIn("docuclipper.com", domains)


class UnverifiedPathTests(unittest.TestCase):
    def test_unknown_phrase_retrieves_nothing_and_stays_unverified(self):
        run = verify("a phrase with no fixture whatsoever", fixture_providers())
        self.assertFalse(run.verified)
        self.assertEqual(run.score.display(), "UNVERIFIED")
        self.assertIn("UNVERIFIED", run.kill_reason)

    def test_unverified_report_shows_no_score_section(self):
        run = verify("another phrase with no fixture", fixture_providers())
        text = render_report(run)
        self.assertIn("VERDICT: UNVERIFIED", text)
        self.assertNotIn("composite =", text)


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.run = verify("eicr expiry reminder app", fixture_providers(), depth="deep")

    def test_argument_appears_before_the_score(self):
        text = render_report(self.run)
        self.assertLess(
            text.index("STRONGEST ARGUMENT AGAINST"),
            text.index("SCORE"),
            "the case against must be stated before the number",
        )

    def test_report_shows_retrieval_proof(self):
        self.assertIn("RETRIEVAL PROOF", render_report(self.run))

    def test_report_shows_every_component(self):
        text = render_report(self.run)
        for part in ("demand", "supply_weak", "pay", "reach", "buildable"):
            self.assertIn(part, text)

    def test_csv_round_trips(self):
        csv_text = to_csv([self.run])
        self.assertIn("eicr expiry reminder app", csv_text)
        self.assertIn("argument_against", csv_text)

    def test_table_renders(self):
        self.assertIn("eicr", render_table([self.run]))


class GenerationTests(unittest.TestCase):
    def test_uk_trades_matches_its_lexicon(self):
        name, _ = match_vertical("UK trades")
        self.assertEqual(name, "uk trades")

    def test_unknown_seed_falls_back_to_generic(self):
        name, _ = match_vertical("underwater basket weaving")
        self.assertEqual(name, "generic")

    def test_generates_a_useful_number_of_candidates(self):
        phrases = generate_candidates("UK trades", limit=120)
        self.assertGreaterEqual(len(phrases), 50)
        self.assertLessEqual(len(phrases), 120)

    def test_candidates_are_concrete_not_industry_names(self):
        phrases = generate_candidates("UK trades", limit=60)
        self.assertNotIn("uk trades", phrases)
        self.assertTrue(any("eicr" in p for p in phrases))

    def test_candidates_are_unique(self):
        phrases = generate_candidates("small landlords", limit=200)
        self.assertEqual(len(phrases), len(set(phrases)))

    def test_every_builtin_vertical_is_reachable_and_rich(self):
        from capyfind.generate import VERTICALS

        for name, data in VERTICALS.items():
            # Its own name should route back to its lexicon, not to generic.
            matched, _ = match_vertical(name)
            self.assertEqual(matched, name, f"{name!r} did not match itself")
            # And every alias should resolve to a real (non-generic) lexicon.
            for alias in data["aliases"]:
                m, _ = match_vertical(alias)
                self.assertNotEqual(m, "generic", f"alias {alias!r} fell through")
            # It should generate a useful pile of concrete, unique candidates.
            phrases = generate_candidates(name, limit=200)
            self.assertGreaterEqual(
                len(phrases), 40, f"{name!r} generated only {len(phrases)}"
            )
            self.assertEqual(len(phrases), len(set(phrases)), f"{name!r} has dupes")
            self.assertNotIn(name, phrases, f"{name!r} emitted the bare seed")

    def test_new_verticals_match_common_phrasings(self):
        for phrase, expected in [
            ("childminders", "childminders"),
            ("dog groomer", "dog groomers"),
            ("driving instructor", "driving instructors"),
            ("mobile hairdresser", "mobile hairdressers"),
            ("cleaning company", "cleaners"),
            ("street food", "caterers"),
            ("wedding photographer", "photographers"),
            ("domiciliary care", "home care"),
            ("airbnb host", "holiday lets"),
        ]:
            with self.subTest(phrase=phrase):
                self.assertEqual(match_vertical(phrase)[0], expected)

    def test_tasks_file_is_honoured(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tasks.txt"
            path.write_text("# comment\nwidget calibration log\n", encoding="utf-8")
            phrases = generate_candidates("anything", limit=200, tasks_file=path)
            self.assertIn("widget calibration log", phrases)
            self.assertIn("widget calibration log app", phrases)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "test.db")

    def tearDown(self):
        self.tmp.cleanup()

    def test_run_round_trips_through_sqlite(self):
        run = verify("eicr expiry reminder app", fixture_providers(), depth="deep")
        self.store.save_run(run, seed="test")
        restored = run_from_dict(self.store.get_run(run.candidate, run.depth))
        self.assertEqual(restored.candidate, run.candidate)
        self.assertEqual(restored.score.composite, run.score.composite)
        self.assertEqual(len(restored.competitors), len(run.competitors))
        self.assertEqual(restored.argument_against, run.argument_against)

    def test_has_run_enables_resume(self):
        run = verify("invoice generator", fixture_providers(), depth="deep")
        self.assertFalse(self.store.has_run(run.candidate, run.depth))
        self.store.save_run(run, seed="test")
        self.assertTrue(self.store.has_run(run.candidate, run.depth))

    def test_cache_stores_and_returns(self):
        self.store.cache_put("p", "q", [{"a": 1}])
        self.assertEqual(self.store.cache_get("p", "q"), [{"a": 1}])

    def test_expired_cache_returns_none(self):
        store = Store(Path(self.tmp.name) / "ttl.db", ttl=-1)
        store.cache_put("p", "q", [1])
        self.assertIsNone(store.cache_get("p", "q"))

    def test_unverified_runs_sort_last(self):
        good = verify("invoice generator", fixture_providers(), depth="deep")
        bad = verify("no fixture for this", fixture_providers(), depth="deep")
        self.store.save_run(bad, seed="s")
        self.store.save_run(good, seed="s")
        listed = self.store.list_runs(seed="s")
        self.assertEqual(listed[0]["candidate"], good.candidate)


class BatchTests(unittest.TestCase):
    def test_batch_stops_after_consecutive_failures(self):
        batch = discover(
            "seed",
            fixture_providers(),
            candidates=[f"nonexistent phrase {i}" for i in range(20)],
            failure_threshold=3,
            store=None,
        )
        self.assertTrue(batch.stopped_early)
        self.assertLessEqual(len(batch.runs), 4)

    def test_batch_skips_already_researched_candidates(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "b.db")
            phrases = ["invoice generator"]
            first = discover("s", fixture_providers(), candidates=phrases, store=store)
            self.assertEqual(len(first.runs), 1)
            second = discover("s", fixture_providers(), candidates=phrases, store=store)
            self.assertEqual(second.skipped, 1)
            self.assertEqual(len(second.runs), 0)

    def test_survivors_excludes_zero_scores(self):
        batch = discover(
            "s",
            fixture_providers(),
            candidates=["invoice generator", "convert bank statement pdf to excel"],
            store=None,
        )
        self.assertEqual(batch.survivors, [])

    def test_ranked_puts_unverified_last(self):
        batch = discover(
            "s",
            fixture_providers(),
            candidates=["no fixture here", "invoice generator"],
            failure_threshold=99,
            store=None,
        )
        ranked = batch.ranked()
        self.assertTrue(ranked[0].verified)
        self.assertFalse(ranked[-1].verified)


if __name__ == "__main__":
    unittest.main()
