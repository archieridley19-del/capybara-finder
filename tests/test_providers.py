"""Provider tests.

All offline. The network-dependent behaviour is covered by asserting on the
query-shaping and filtering logic, which is where the bugs actually live -- the
HTTP call itself is the boring part.
"""

import os
import unittest

from capyfind.classify import GENERIC_VENUES, classify_result
from capyfind.models import SearchResult
from capyfind.providers import build_providers
from capyfind.providers.base import KIND_SOCIAL, KIND_WEB
from capyfind.providers.forums import (
    TRADE_FORUMS,
    HackerNewsProvider,
    StackExchangeProvider,
    TradeForumProvider,
    keyword_terms,
    relevant,
)
from capyfind.signals import reach_evidence


def result(title, url, snippet="", rank=1):
    return SearchResult(
        title=title, url=url, snippet=snippet, rank=rank, provider="test"
    )


class KeywordShapingTests(unittest.TestCase):
    """Stack Exchange ANDs every term, so scaffolding words match nothing."""

    def test_question_scaffolding_is_stripped(self):
        terms = keyword_terms("is there a tool for eicr expiry reminder")
        self.assertNotIn("tool", terms)
        self.assertNotIn("for", terms)
        self.assertIn("eicr", terms)

    def test_terms_are_capped(self):
        terms = keyword_terms("convert bank statement pdf into excel csv quickbooks", 4)
        self.assertEqual(len(terms), 4)

    def test_order_is_preserved(self):
        self.assertEqual(
            keyword_terms("convert bank statement pdf"),
            ["convert", "bank", "statement", "pdf"],
        )

    def test_duplicates_removed(self):
        self.assertEqual(keyword_terms("invoice invoice invoice"), ["invoice"])

    def test_all_template_words_yields_nothing(self):
        self.assertEqual(keyword_terms("is there an app for the tool"), [])


class RelevanceFilterTests(unittest.TestCase):
    """Algolia ORs terms, so unrelated hits come back and must be dropped.
    Letting them through would turn noise into 'demand evidence'."""

    def test_offtopic_hit_is_rejected(self):
        self.assertFalse(
            relevant(
                "Groupon has no viable business model",
                ["track", "certificate", "expiry", "dates"],
            )
        )

    def test_ontopic_hit_is_kept(self):
        self.assertTrue(
            relevant(
                "Show HN: Bank statement PDF to Excel, totals verified",
                ["convert", "bank", "statement", "pdf"],
            )
        )

    def test_half_the_terms_is_enough(self):
        self.assertTrue(relevant("bank statement tooling", ["convert", "bank", "statement", "pdf"]))

    def test_single_incidental_word_is_not_enough(self):
        self.assertFalse(relevant("a statement about politics", ["convert", "bank", "statement", "pdf"]))

    def test_no_terms_means_no_filtering(self):
        self.assertTrue(relevant("anything at all", []))


class ProviderAvailabilityTests(unittest.TestCase):
    def test_keyless_providers_are_available_by_default(self):
        self.assertTrue(StackExchangeProvider().available())
        self.assertTrue(HackerNewsProvider().available())

    def test_keyless_providers_can_be_disabled(self):
        os.environ["CAPYFIND_DISABLE_HN"] = "1"
        try:
            self.assertFalse(HackerNewsProvider().available())
        finally:
            del os.environ["CAPYFIND_DISABLE_HN"]

    def test_trade_forums_need_a_web_provider(self):
        provider = TradeForumProvider(web=None)
        self.assertFalse(provider.available())
        self.assertIn("web search provider", provider.why_unavailable())

    def test_trade_forums_cost_one_query_only(self):
        self.assertEqual(TradeForumProvider().max_queries, 1)

    def test_reddit_explains_how_to_get_credentials(self):
        from capyfind.providers.social import RedditProvider

        self.assertIn("reddit.com/prefs/apps", RedditProvider().why_unavailable())


class RegistryTests(unittest.TestCase):
    def test_social_role_fans_out(self):
        providers = build_providers()
        names = {p.name for p in providers.roles[KIND_SOCIAL]}
        self.assertEqual(
            names, {"stackexchange", "hackernews", "reddit", "tradeforums"}
        )

    def test_free_sources_come_before_paid_ones(self):
        order = [p.name for p in build_providers().roles[KIND_SOCIAL]]
        self.assertLess(order.index("stackexchange"), order.index("tradeforums"))

    def test_all_returns_only_usable_providers(self):
        # Without any keys, Reddit and the forum sweep are unavailable, but the
        # two keyless sources still are -- so a run can reach `verified` for free.
        usable = {p.name for p in build_providers().all(KIND_SOCIAL)}
        self.assertIn("stackexchange", usable)
        self.assertIn("hackernews", usable)
        self.assertNotIn("reddit", usable)

    def test_fixture_mode_replaces_every_role(self):
        providers = build_providers(use_fixtures=True)
        self.assertTrue(providers.fixture_mode)
        self.assertEqual(providers.get(KIND_WEB).name, "fixture")

    def test_describe_explains_missing_providers(self):
        text = "\n".join(build_providers().describe())
        self.assertIn("reddit", text)
        self.assertIn("prefs/apps", text)


class ForumClassificationTests(unittest.TestCase):
    def test_trade_forums_are_communities_not_products(self):
        for group in TRADE_FORUMS.values():
            for domain in group:
                with self.subTest(domain=domain):
                    kind, _ = classify_result(result("a thread", f"https://{domain}/t/1"))
                    self.assertEqual(kind, "community")

    def test_trade_forum_counts_as_a_reach_venue(self):
        venues = reach_evidence(
            [result("thread", "https://landlordzone.co.uk/forum/t/1")], []
        )
        self.assertEqual(len(venues), 1)
        self.assertIn("landlordzone", venues[0].claim)

    def test_hacker_news_is_not_a_reach_venue(self):
        # Good demand evidence, but not a place UK sole traders gather.
        venues = reach_evidence(
            [result("thread", "https://news.ycombinator.com/item?id=1")], []
        )
        self.assertEqual(venues, [])

    def test_stack_exchange_is_not_a_reach_venue(self):
        venues = reach_evidence(
            [result("q", "https://softwarerecs.stackexchange.com/questions/1")], []
        )
        self.assertEqual(venues, [])

    def test_named_subreddit_is_a_reach_venue(self):
        venues = reach_evidence(
            [result("thread", "https://reddit.com/r/uklandlords/comments/x/")], []
        )
        self.assertEqual(len(venues), 1)
        self.assertIn("r/uklandlords", venues[0].claim)

    def test_generic_venues_are_excluded_from_reach(self):
        for domain in sorted(GENERIC_VENUES):
            with self.subTest(domain=domain):
                venues = reach_evidence([result("t", f"https://{domain}/x")], [])
                self.assertEqual(venues, [])


class SupplyAssessmentTests(unittest.TestCase):
    """The most dangerous false positive: demand sources succeed, no web
    search runs, and "no competitor found" scores as a wide-open market."""

    def _run_with(self, roles):
        from capyfind.models import Retrieval, Run

        run = Run(candidate="x")
        for role in roles:
            run.retrievals.append(
                Retrieval(provider=role, query="x", role=role, n_results=5, ok=True)
            )
        run.results.append(result("a", "https://example.com/a"))
        return run

    def test_demand_only_run_is_not_verified(self):
        run = self._run_with(["social", "suggest"])
        self.assertTrue(run.n_productive > 0)
        self.assertFalse(run.supply_assessed)
        self.assertFalse(run.verified)

    def test_web_retrieval_enables_verification(self):
        run = self._run_with(["web", "social"])
        self.assertTrue(run.supply_assessed)
        self.assertTrue(run.verified)

    def test_failed_web_retrieval_does_not_count(self):
        from capyfind.models import Retrieval, Run

        run = Run(candidate="x")
        run.retrievals.append(
            Retrieval(provider="serper", query="x", role="web", ok=False, error="401")
        )
        run.retrievals.append(
            Retrieval(provider="hn", query="x", role="social", n_results=3, ok=True)
        )
        run.results.append(result("a", "https://example.com/a"))
        self.assertFalse(run.supply_assessed)

    def test_kill_reason_names_the_missing_key(self):
        from capyfind.scoring import finalise

        run = finalise(self._run_with(["social"]))
        self.assertIn("supply was never assessed", run.kill_reason)
        self.assertIn("SERPER_API_KEY", run.kill_reason)

    def test_retrievals_are_tagged_with_their_role(self):
        from capyfind.providers.base import SearchProvider

        class Stub(SearchProvider):
            name = "stub"
            kind = KIND_SOCIAL

            def available(self):
                return True

            def _fetch(self, query, limit):
                return [result("a", "https://example.com/a")], "stub://"

        _, retrieval = Stub().search("anything", limit=1)
        self.assertEqual(retrieval.role, KIND_SOCIAL)

    def test_unavailable_provider_still_tags_its_role(self):
        provider = TradeForumProvider(web=None)
        _, retrieval = provider.search("anything")
        self.assertEqual(retrieval.role, KIND_SOCIAL)
        self.assertFalse(retrieval.ok)


class TradeForumQueryTests(unittest.TestCase):
    def test_default_sites_span_several_verticals(self):
        sites = TradeForumProvider().sites
        self.assertIn("landlordzone.co.uk", sites)
        self.assertIn("accountingweb.co.uk", sites)
        self.assertGreaterEqual(len(sites), 5)

    def test_group_selects_a_narrower_list(self):
        self.assertEqual(
            TradeForumProvider(group="trades").sites, TRADE_FORUMS["trades"]
        )


if __name__ == "__main__":
    unittest.main()
