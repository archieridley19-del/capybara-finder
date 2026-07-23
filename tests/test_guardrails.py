"""Tests for the six failure modes that killed v1.

Each test below maps to a numbered requirement in the brief. If one of these
starts failing, the tool has regressed into the version that rated almost every
niche a winner.
"""

import unittest

from capyfind.classify import (
    NicheContext,
    classify_competitor,
    classify_result,
    detect_blockers,
    is_dedicated,
)
from capyfind.models import (
    Blocker,
    Competitor,
    Evidence,
    Run,
    Score,
    SearchResult,
    Signal,
)
from capyfind.scoring import BAND_REJECT, calibration_warning, finalise


def result(title, url, snippet="", rank=1):
    return SearchResult(
        title=title, url=url, snippet=snippet, rank=rank, provider="test"
    )


def evidence(claim="a claim", url="https://example.com/x"):
    return Evidence(claim=claim, url=url)


def verified_run(candidate="test niche", **kwargs):
    run = Run(candidate=candidate, **kwargs)
    run.retrievals.append(
        __import__("capyfind.models", fromlist=["Retrieval"]).Retrieval(
            provider="test", query=candidate, n_results=5, ok=True
        )
    )
    run.results.append(result("a result", "https://example.com/a"))
    return run


# --- requirement 1: retrieval proof -----------------------------------------


class RetrievalProofTests(unittest.TestCase):
    def test_run_with_no_retrievals_is_unverified(self):
        self.assertFalse(Run(candidate="x").verified)

    def test_failed_retrievals_do_not_count_as_proof(self):
        from capyfind.models import Retrieval

        run = Run(candidate="x")
        run.retrievals.append(
            Retrieval(provider="p", query="x", ok=False, error="401")
        )
        self.assertFalse(run.verified)

    def test_retrieval_with_zero_results_is_not_productive(self):
        from capyfind.models import Retrieval

        r = Retrieval(provider="p", query="x", ok=True, n_results=0)
        self.assertFalse(r.productive)

    def test_unverified_run_displays_no_number(self):
        run = finalise(Run(candidate="x"))
        self.assertEqual(run.score.display(), "UNVERIFIED")
        self.assertEqual(run.band, "unverified")

    def test_unverified_run_cannot_be_scored_positively(self):
        run = Run(candidate="x")
        run.signals.append(
            Signal("demand", "lots of demand", 4.0, [evidence()])
        )
        run = finalise(run)
        self.assertTrue(run.score.unverified)
        self.assertEqual(run.score.composite, 0)


# --- requirement 2: supply judged on quality, not existence -----------------


class SupplyClassificationTests(unittest.TestCase):
    def test_listicle_title_is_not_a_product(self):
        kind, _ = classify_result(
            result("10 Best Invoice Tools in 2026", "https://example.com/x")
        )
        self.assertEqual(kind, "listicle")

    def test_blog_path_is_not_a_product(self):
        kind, _ = classify_result(
            result("Acme Invoicing", "https://acme.com/blog/how-to-invoice/")
        )
        self.assertEqual(kind, "listicle")

    def test_review_directory_is_not_a_product(self):
        kind, _ = classify_result(result("Invoice tools", "https://www.g2.com/x"))
        self.assertEqual(kind, "directory")

    def test_forum_is_not_a_product(self):
        kind, _ = classify_result(
            result("thread", "https://reddit.com/r/x/comments/y")
        )
        self.assertEqual(kind, "community")

    def test_regulator_is_not_a_product(self):
        kind, _ = classify_result(result("EICR rules", "https://www.gov.uk/eicr"))
        self.assertEqual(kind, "regulator")

    def test_plain_homepage_is_a_product(self):
        kind, _ = classify_result(result("Acme Invoicer", "https://acme.com/"))
        self.assertEqual(kind, "product")

    def test_generic_suite_is_not_dedicated(self):
        ctx = NicheContext("eicr expiry reminder", country="UK")
        dedicated, _ = is_dedicated(
            result("All-in-one property management", "https://x.com/"), ctx
        )
        self.assertFalse(dedicated)

    def test_us_only_product_is_mismatched_for_uk(self):
        ctx = NicheContext("payroll record app", country="UK")
        comp = classify_competitor(
            result(
                "PayrollCo",
                "https://payrollco.com/",
                "IRS compliant payroll record filing for all 50 states. $29 per month. 2026",
            ),
            ctx,
        )
        self.assertEqual(comp.klass, "mismatched")
        self.assertFalse(comp.is_strong_supply)

    def test_abandoned_product_is_weak_supply(self):
        ctx = NicheContext("eicr expiry reminder", country="UK")
        comp = classify_competitor(
            result(
                "CertTracker",
                "https://certtracker.co.uk/",
                "EICR expiry reminder tool. Copyright 2018.",
            ),
            ctx,
        )
        self.assertEqual(comp.klass, "abandoned")
        self.assertFalse(comp.is_strong_supply)

    def test_product_without_pricing_is_weak_not_polished(self):
        ctx = NicheContext("eicr expiry reminder", country="UK")
        comp = classify_competitor(
            result("CertMinder", "https://certminder.co.uk/", "EICR expiry reminder."),
            ctx,
        )
        self.assertEqual(comp.klass, "weak")

    def test_priced_ontarget_recent_product_is_polished(self):
        ctx = NicheContext("eicr expiry reminder", country="UK")
        comp = classify_competitor(
            result(
                "CertMinder",
                "https://certminder.co.uk/",
                "EICR expiry reminder for UK landlords. From £9 per month. Updated 2026.",
            ),
            ctx,
        )
        self.assertEqual(comp.klass, "polished")
        self.assertTrue(comp.is_strong_supply)

    def test_one_polished_competitor_forces_rejection(self):
        run = verified_run()
        run.signals.append(Signal("demand", "huge demand", 4.0, [evidence()]))
        run.signals.append(Signal("pay", "people pay", 3.0, [evidence()]))
        run.reach_venues = [evidence("r/somewhere", "https://reddit.com/r/somewhere")]
        run.competitors.append(
            Competitor(
                name="Incumbent",
                url="https://incumbent.com/",
                domain="incumbent.com",
                klass="polished",
            )
        )
        run = finalise(run)
        self.assertEqual(run.score.supply_weak, 0)
        self.assertEqual(run.score.composite, 0)
        self.assertEqual(run.band, BAND_REJECT)
        self.assertIn("incumbent.com", run.kill_reason)


# --- requirement 3: argument before score -----------------------------------


class ArgumentFirstTests(unittest.TestCase):
    def test_argument_against_is_always_written(self):
        run = finalise(verified_run())
        self.assertTrue(run.argument_against.strip())

    def test_argument_names_the_polished_incumbent(self):
        run = verified_run()
        run.competitors.append(
            Competitor("Incumbent", "https://inc.com/", "inc.com", "polished")
        )
        run = finalise(run)
        self.assertIn("Incumbent", run.argument_against)

    def test_argument_flags_missing_pay_evidence(self):
        run = finalise(verified_run())
        self.assertIn("spends money", run.argument_against)


# --- requirement 4: no citation, no signal ----------------------------------


class CitationTests(unittest.TestCase):
    def test_signal_without_evidence_is_rejected(self):
        with self.assertRaises(ValueError):
            Signal("demand", "trust me", 3.0, [])

    def test_evidence_without_url_is_rejected(self):
        with self.assertRaises(ValueError):
            Evidence(claim="a claim", url="")

    def test_evidence_without_claim_is_rejected(self):
        with self.assertRaises(ValueError):
            Evidence(claim="  ", url="https://example.com")

    def test_unknown_dimension_is_rejected(self):
        with self.assertRaises(ValueError):
            Signal("vibes", "feels good", 1.0, [evidence()])


# --- requirement 5: calibration ---------------------------------------------


class CalibrationTests(unittest.TestCase):
    def test_too_many_pursues_is_flagged_as_a_bug(self):
        runs = []
        for i in range(20):
            run = verified_run(f"c{i}")
            run.signals.append(Signal("demand", "d", 4.0, [evidence()]))
            run.signals.append(Signal("pay", "p", 3.0, [evidence()]))
            run.reach_venues = [evidence(), evidence("b", "https://b.com")]
            runs.append(finalise(run))
        warning = calibration_warning(runs)
        self.assertIsNotNone(warning)
        self.assertIn("CALIBRATION", warning)

    def test_no_warning_on_a_healthy_run(self):
        runs = []
        for i in range(20):
            run = verified_run(f"c{i}")
            run.competitors.append(
                Competitor("Inc", "https://inc.com/", "inc.com", "polished")
            )
            runs.append(finalise(run))
        self.assertIsNone(calibration_warning(runs))


# --- requirement 6: demand is not the whole picture -------------------------


class MultiplicativeScoreTests(unittest.TestCase):
    def _run_with(self, pay=True, reach=True, blocker=False):
        run = verified_run()
        run.signals.append(Signal("demand", "d", 4.0, [evidence()]))
        if pay:
            run.signals.append(Signal("pay", "p", 3.0, [evidence()]))
        if reach:
            run.reach_venues = [evidence(), evidence("b", "https://b.com")]
        if blocker:
            run.blockers = [
                Blocker(claim="accreditation", severity="hard", evidence=evidence())
            ]
        return finalise(run)

    def test_no_pay_evidence_zeroes_the_score(self):
        run = self._run_with(pay=False)
        self.assertEqual(run.score.pay, 0)
        self.assertEqual(run.score.composite, 0)
        self.assertIn("pays", run.kill_reason)

    def test_no_reachable_audience_zeroes_the_score(self):
        run = self._run_with(reach=False)
        self.assertEqual(run.score.composite, 0)
        self.assertIn("reach", run.kill_reason)

    def test_hard_blocker_zeroes_the_score(self):
        run = self._run_with(blocker=True)
        self.assertEqual(run.score.buildable, 0)
        self.assertEqual(run.score.composite, 0)

    def test_possible_blocker_does_not_zero_the_score(self):
        run = verified_run()
        run.signals.append(Signal("demand", "d", 4.0, [evidence()]))
        run.signals.append(Signal("pay", "p", 3.0, [evidence()]))
        run.reach_venues = [evidence(), evidence("b", "https://b.com")]
        run.blockers = [
            Blocker(claim="maybe licensed", severity="possible", evidence=evidence())
        ]
        run = finalise(run)
        self.assertEqual(run.score.buildable, 1)
        self.assertGreater(run.score.composite, 0)


class BlockerDetectionTests(unittest.TestCase):
    def test_regulator_source_makes_a_blocker_hard(self):
        blockers = detect_blockers(
            [
                result(
                    "Electrical safety standards",
                    "https://www.gov.uk/guidance/electrical-safety",
                    "Reports must be issued by a registered installer.",
                )
            ]
        )
        self.assertTrue(blockers)
        self.assertEqual(blockers[0].severity, "hard")
        self.assertIn("gov.uk", blockers[0].evidence.url)

    def test_casual_mention_is_only_possible(self):
        blockers = detect_blockers(
            [
                result(
                    "Top 10 tools",
                    "https://someblog.com/x",
                    "Some of these are used by licensed professionals.",
                )
            ]
        )
        self.assertTrue(blockers)
        self.assertEqual(blockers[0].severity, "possible")

    def test_no_blocker_on_clean_text(self):
        self.assertEqual(
            detect_blockers([result("Acme", "https://acme.com/", "A simple app.")]),
            [],
        )


class RatioTests(unittest.TestCase):
    def test_generic_suites_do_not_count_as_dedicated_supply(self):
        run = verified_run()
        run.competitors.append(
            Competitor(
                "Suite", "https://suite.com/", "suite.com", "mismatched", dedicated=False
            )
        )
        self.assertEqual(run.dedicated_supply, 0)

    def test_ratio_floors_denominator_at_one(self):
        run = verified_run()
        run.signals.append(Signal("demand", "d", 1.0, [evidence()]))
        self.assertEqual(run.ratio, 1.0)


class ScoreDisplayTests(unittest.TestCase):
    def test_max_composite_is_144(self):
        self.assertEqual(Score().max_composite, 144)

    def test_verified_score_shows_a_number(self):
        self.assertEqual(Score(1, 1, 1, 1, 1, unverified=False).display(), "1")


if __name__ == "__main__":
    unittest.main()
