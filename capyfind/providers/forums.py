"""Forum providers beyond Reddit.

Three sources, chosen because each answers a different question:

- **Software Recommendations (Stack Exchange)** -- a whole site of people
  asking "is there a tool that does X". The single highest-signal demand source
  for this tool's purpose. Free, no key.
- **Hacker News** (via the Algolia API) -- catches "Show HN" launches and
  "why is there no app for" threads, and is the fastest way to find out that a
  founder already tried this and abandoned it. Free, no key.
- **Trade forums** -- where UK sole traders actually gather. These have no
  APIs, so they are reached with site-restricted queries through whichever web
  search API is configured. This is the only one that costs money, so it is
  capped at one query per candidate.

The first two mean the tool can produce genuinely verified demand evidence with
no paid key at all.
"""

from __future__ import annotations

import os
import time
from datetime import datetime

import re

from ..http import request_json
from ..models import SearchResult
from .base import KIND_SOCIAL, SearchProvider

# Imported lazily-ish to keep the provider layer free of classifier internals,
# but the stopword list is the same one the classifier uses.
from ..classify import TEMPLATE_TOKENS


def keyword_terms(phrase: str, max_terms: int = 4) -> list[str]:
    """Content words, in order, for keyword-matching APIs.

    Stack Exchange ANDs every term, so "is there a tool for eicr expiry
    reminder" matches nothing while "eicr expiry reminder" matches. Hacker
    News ORs them, which is the opposite problem -- see `relevant()`.
    """
    words = re.findall(r"[a-z0-9]+", phrase.lower())
    terms: list[str] = []
    for word in words:
        if word in TEMPLATE_TOKENS or len(word) <= 2 or word in terms:
            continue
        terms.append(word)
    return terms[:max_terms]


def relevant(text: str, terms: list[str]) -> bool:
    """Does this hit actually concern the query?

    Algolia matches loosely, so a search for certificate expiry happily returns
    threads about Groupon's business model. Letting those through would turn
    noise into "demand evidence", which is the exact failure this tool exists
    to prevent.
    """
    if not terms:
        return True
    lowered = text.lower()
    hits = sum(1 for term in terms if term in lowered)
    return hits >= max(1, (len(terms) + 1) // 2)

#: Where UK sole traders, landlords and small practices actually post.
#: Grouped so each group fits in one OR-joined query.
TRADE_FORUMS = {
    "property": [
        "landlordzone.co.uk",
        "propertytribes.com",
        "propertyhub.net",
    ],
    "business": [
        "ukbusinessforums.co.uk",
        "accountingweb.co.uk",
        "contractoruk.com",
    ],
    "trades": [
        "community.screwfix.com",
        "diynot.com",
        "electriciansforums.net",
    ],
    "consumer": [
        "forums.moneysavingexpert.com",
        "mumsnet.com",
    ],
}

#: Stack Exchange sites worth searching, most useful first.
DEFAULT_SE_SITES = ("softwarerecs", "webapps")


class StackExchangeProvider(SearchProvider):
    """Stack Exchange search/advanced. No key needed (300 requests/day/IP).

    Set STACKEXCHANGE_KEY to raise the quota, and CAPYFIND_SE_SITES to change
    which sites are searched.
    """

    name = "stackexchange"
    kind = KIND_SOCIAL
    endpoint = "https://api.stackexchange.com/2.3/search/advanced"
    max_queries = 2

    def available(self) -> bool:
        return os.environ.get("CAPYFIND_DISABLE_STACKEXCHANGE") != "1"

    def why_unavailable(self) -> str:
        return "disabled via CAPYFIND_DISABLE_STACKEXCHANGE"

    @property
    def sites(self) -> tuple[str, ...]:
        raw = os.environ.get("CAPYFIND_SE_SITES")
        return tuple(s.strip() for s in raw.split(",") if s.strip()) if raw else DEFAULT_SE_SITES

    def _fetch(self, query: str, limit: int) -> tuple[list[SearchResult], str]:
        results: list[SearchResult] = []
        per_site = max(1, limit // len(self.sites))
        # Every term is ANDed, so send content words only and keep it short.
        terms = keyword_terms(query, max_terms=4)
        if not terms:
            return [], self.endpoint

        for site in self.sites:
            params = {
                "order": "desc",
                "sort": "relevance",
                "q": " ".join(terms),
                "site": site,
                "pagesize": per_site,
            }
            key = os.environ.get("STACKEXCHANGE_KEY")
            if key:
                params["key"] = key

            data = request_json(self.endpoint, params=params)
            for item in data.get("items", []):
                created = item.get("creation_date")
                stamp = (
                    datetime.utcfromtimestamp(created).strftime("%Y-%m-%d")
                    if created
                    else "?"
                )
                results.append(
                    SearchResult(
                        title=item.get("title", ""),
                        url=item.get("link", ""),
                        snippet=(
                            f"{site}.stackexchange.com | {stamp} | "
                            f"{item.get('answer_count', 0)} answers | "
                            f"score {item.get('score', 0)} | "
                            f"{item.get('view_count', 0)} views"
                        ),
                        rank=len(results) + 1,
                        provider=self.name,
                    )
                )
            if data.get("quota_remaining", 1) <= 0:
                break

        return results[:limit], self.endpoint


class HackerNewsProvider(SearchProvider):
    """Hacker News via the public Algolia search API. Free, no key."""

    name = "hackernews"
    kind = KIND_SOCIAL
    endpoint = "https://hn.algolia.com/api/v1/search"
    max_queries = 2

    def available(self) -> bool:
        return os.environ.get("CAPYFIND_DISABLE_HN") != "1"

    def why_unavailable(self) -> str:
        return "disabled via CAPYFIND_DISABLE_HN"

    def _fetch(self, query: str, limit: int) -> tuple[list[SearchResult], str]:
        terms = keyword_terms(query, max_terms=5)
        data = request_json(
            self.endpoint,
            params={
                "query": " ".join(terms) if terms else query,
                "tags": "(story,comment)",
                # Over-fetch, because most hits are filtered out below.
                "hitsPerPage": min(50, limit * 5),
            },
        )

        results = []
        for hit in data.get("hits", []):
            title = hit.get("title") or hit.get("story_title") or ""
            text = (hit.get("comment_text") or hit.get("story_text") or "")[:200]

            if title.strip() in ("[dead]", "[flagged]", "[deleted]"):
                continue
            # Algolia ORs the terms, so drop hits that merely share one word.
            if not relevant(f"{title} {text}", terms):
                continue

            created = hit.get("created_at", "")[:10] or "?"
            results.append(
                SearchResult(
                    title=title or "(comment)",
                    url=f"https://news.ycombinator.com/item?id={hit.get('objectID')}",
                    snippet=(
                        f"HN | {created} | {hit.get('points') or 0} points | "
                        f"{hit.get('num_comments') or 0} comments | {text}"
                    ),
                    rank=len(results) + 1,
                    provider=self.name,
                )
            )
            if len(results) >= limit:
                break

        return results, self.endpoint


class TradeForumProvider(SearchProvider):
    """UK trade and small-business forums, reached via site-restricted search.

    These forums have no APIs and scraping them is neither polite nor durable,
    so this delegates to the configured web search provider with an OR-joined
    `site:` filter. One query covers a whole group of forums, which keeps the
    cost at a single search credit per candidate.
    """

    name = "tradeforums"
    kind = KIND_SOCIAL
    max_queries = 1

    def __init__(self, store=None, web: SearchProvider | None = None, group: str = "") -> None:
        super().__init__(store=store)
        self.web = web
        self.group = group

    @property
    def sites(self) -> list[str]:
        if self.group and self.group in TRADE_FORUMS:
            return TRADE_FORUMS[self.group]
        # Default: the broadest useful mix, one query's worth.
        return [
            "landlordzone.co.uk",
            "ukbusinessforums.co.uk",
            "accountingweb.co.uk",
            "community.screwfix.com",
            "propertytribes.com",
            "forums.moneysavingexpert.com",
        ]

    def available(self) -> bool:
        return self.web is not None and self.web.available()

    def why_unavailable(self) -> str:
        if self.web is None:
            return "needs a web search provider (set SERPER_API_KEY or BRAVE_API_KEY)"
        return f"underlying web provider unavailable: {self.web.why_unavailable()}"

    def _fetch(self, query: str, limit: int) -> tuple[list[SearchResult], str]:
        site_filter = " OR ".join(f"site:{s}" for s in self.sites)
        results, retrieval = self.web.search(f"({site_filter}) {query}", limit=limit)
        if not retrieval.ok:
            from ..http import RetrievalError

            raise RetrievalError(retrieval.error or "forum search failed")

        # Re-stamp provenance so the report shows where these came from.
        for result in results:
            result.provider = self.name
            if not result.snippet.startswith(result.domain):
                result.snippet = f"{result.domain} | {result.snippet}"
        return results, retrieval.url or "web-provider"
