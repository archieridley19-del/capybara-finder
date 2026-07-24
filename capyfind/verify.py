"""Part 2 -- deep verification of a single niche.

Slow and thorough, the opposite of discovery's wide sweep. The pipeline is
fixed and the order matters:

    retrieve -> classify supply -> extract cited signals -> detect blockers
             -> write the argument AGAINST -> score -> band

Scoring happens last, after the objection is written, so a verdict is never
back-filled to justify a number.
"""

from __future__ import annotations

from dataclasses import dataclass

from .classify import NicheContext, classify_competitor, classify_result, detect_blockers
from .models import Failure, Run, SearchResult
from .page import fetch_text
from .providers import KIND_SOCIAL, KIND_SUGGEST, KIND_WEB, ProviderSet
from .scoring import finalise
from .signals import demand_signals, pay_signals, reach_evidence
from .store import Store


@dataclass
class Depth:
    name: str
    web_queries: int
    social_queries: int
    results_per_query: int
    fetch_pages: int  # how many competitor homepages to open


SHALLOW = Depth("shallow", web_queries=1, social_queries=1, results_per_query=10, fetch_pages=0)
DEEP = Depth("deep", web_queries=4, social_queries=3, results_per_query=10, fetch_pages=6)

DEPTHS = {"shallow": SHALLOW, "deep": DEEP}


def web_query_plan(candidate: str) -> list[str]:
    return [
        candidate,
        f"{candidate} software",
        f"{candidate} pricing",
        f"best {candidate} tool",
    ]


def social_query_plan(candidate: str) -> list[str]:
    return [
        candidate,
        f"is there a tool for {candidate}",
        f"{candidate} alternative expensive",
    ]


def verify(
    candidate: str,
    providers: ProviderSet,
    *,
    depth: str = "deep",
    country: str = "UK",
    buyer_size: str = "sole trader",
    store: Store | None = None,
) -> Run:
    cfg = DEPTHS.get(depth, DEEP)
    run = Run(candidate=candidate, depth=cfg.name)
    ctx = NicheContext(candidate=candidate, country=country, buyer_size=buyer_size)

    # Fixture runs must stay fully offline, or the "offline" calibration test
    # quietly depends on the network and on live sites not changing.
    web_provider = providers.get(KIND_WEB)
    fetch_pages = cfg.fetch_pages if (web_provider and web_provider.live) else 0

    web_results: list[SearchResult] = []
    threads: list[SearchResult] = []
    suggestions: list[SearchResult] = []

    # --- retrieval ---------------------------------------------------------

    web = web_provider
    if web is None:
        run.failures.append(Failure("web", providers.notes.get(KIND_WEB, "no web provider")))
    else:
        for query in web_query_plan(candidate)[: cfg.web_queries]:
            results, retrieval = web.search(query, limit=cfg.results_per_query)
            run.retrievals.append(retrieval)
            if not retrieval.ok:
                run.failures.append(Failure("web", f"{query!r}: {retrieval.error}"))
            web_results.extend(results)

    suggest = providers.get(KIND_SUGGEST)
    if suggest is not None:
        results, retrieval = suggest.search(candidate, limit=10)
        run.retrievals.append(retrieval)
        if not retrieval.ok:
            run.failures.append(Failure("suggest", str(retrieval.error)))
        suggestions.extend(results)

    # Fan out across every usable social source. Each covers a different
    # audience -- Software Recommendations and HN catch the "is there a tool
    # that..." asks, Reddit catches the trade subreddits, and the forum
    # provider reaches UK sole traders who are on none of the above.
    social_providers = providers.all(KIND_SOCIAL)
    if not social_providers:
        run.failures.append(Failure("social", "no social provider available"))
    for social in social_providers:
        budget = min(cfg.social_queries, social.max_queries)
        for query in social_query_plan(candidate)[:budget]:
            results, retrieval = social.search(query, limit=cfg.results_per_query)
            run.retrievals.append(retrieval)
            if not retrieval.ok:
                run.failures.append(
                    Failure(social.name, f"{query!r}: {retrieval.error}")
                )
            threads.extend(results)

    # De-duplicate by URL, keeping best rank.
    run.results = _dedupe(web_results + threads + suggestions)

    # Requirement 1: no proof of retrieval, no score. Bail before any judgement.
    if not run.verified:
        return finalise(run)

    # --- supply ------------------------------------------------------------

    seen: set[str] = set()
    for result in _dedupe(web_results):
        kind, reasons = classify_result(result)
        if kind != "product":
            continue  # listicles, directories and forums are never competitors
        if result.domain in seen:
            continue
        seen.add(result.domain)

        page_text = ""
        if len(seen) <= fetch_pages:
            page_text = fetch_text(result.url)

        competitor = classify_competitor(result, ctx, page_text=page_text)
        run.competitors.append(competitor)

    # --- demand, pay, reach -------------------------------------------------

    run.signals.extend(demand_signals(suggestions, threads))
    run.signals.extend(pay_signals(run.competitors, threads))
    run.reach_venues = reach_evidence(threads, web_results)

    # --- blockers -----------------------------------------------------------

    run.blockers = detect_blockers(run.results)

    run = finalise(run)
    if store is not None:
        store.save_run(run)
    return run


def _dedupe(results: list[SearchResult]) -> list[SearchResult]:
    best: dict[str, SearchResult] = {}
    for result in results:
        if not result.url:
            continue
        existing = best.get(result.url)
        if existing is None or result.rank < existing.rank:
            best[result.url] = result
    return sorted(best.values(), key=lambda r: r.rank)
