"""Demand-side providers: autocomplete, Reddit, Trends.

These answer "are people asking for this, and do they keep asking?" -- the
recurrence-over-time question that separates a real need from a one-off spike.
"""

from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.request

from ..http import USER_AGENT, RetrievalError, request_json
from ..models import SearchResult
from .base import KIND_SOCIAL, KIND_SUGGEST, KIND_TRENDS, SearchProvider


class AutocompleteProvider(SearchProvider):
    """Google's public suggest endpoint.

    This is the documented autocomplete JSON endpoint, not SERP scraping. A
    phrase that produces many specific completions is being typed by real
    people; a phrase with none is usually invented by the candidate generator.
    """

    name = "autocomplete"
    kind = KIND_SUGGEST
    endpoint = "https://suggestqueries.google.com/complete/search"

    def available(self) -> bool:
        return os.environ.get("CAPYFIND_DISABLE_AUTOCOMPLETE") != "1"

    def why_unavailable(self) -> str:
        return "autocomplete disabled via CAPYFIND_DISABLE_AUTOCOMPLETE"

    def _fetch(self, query: str, limit: int) -> tuple[list[SearchResult], str]:
        params = {
            "client": "firefox",
            "q": query,
            "hl": os.environ.get("CAPYFIND_LANG", "en"),
            "gl": os.environ.get("CAPYFIND_COUNTRY", "uk").lower(),
        }
        url = f"{self.endpoint}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=15) as response:
                raw = response.read().decode("utf-8", errors="replace")
            payload = json.loads(raw)
        except Exception as exc:
            raise RetrievalError(f"autocomplete failed: {exc}") from exc

        suggestions = payload[1] if len(payload) > 1 else []
        verify_url = "https://www.google.com/search?q=" + urllib.parse.quote(query)
        return [
            SearchResult(
                title=text,
                url=verify_url,
                snippet=f"autocomplete suggestion for {query!r}",
                rank=i + 1,
                provider=self.name,
            )
            for i, text in enumerate(suggestions[:limit])
        ], url


class RedditProvider(SearchProvider):
    """Reddit's official OAuth API (script app, client-credentials grant)."""

    name = "reddit"
    kind = KIND_SOCIAL
    endpoint = "https://oauth.reddit.com/search"
    token_url = "https://www.reddit.com/api/v1/access_token"

    _token: str | None = None
    _token_expires: float = 0.0

    def available(self) -> bool:
        return bool(
            os.environ.get("REDDIT_CLIENT_ID")
            and os.environ.get("REDDIT_CLIENT_SECRET")
        )

    def why_unavailable(self) -> str:
        return (
            "REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET not set. Note the official "
            "API now needs approval (Responsible Builder policy) -- the "
            "reddit-web provider reaches Reddit through your web key with no "
            "approval, and runs automatically once a web provider is set."
        )

    @property
    def user_agent(self) -> str:
        # Reddit asks for a descriptive UA and rate-limits generic ones harder.
        return os.environ.get("REDDIT_USER_AGENT", USER_AGENT)

    def _get_token(self) -> str:
        if self._token and time.time() < self._token_expires:
            return self._token

        import base64

        creds = f"{os.environ['REDDIT_CLIENT_ID']}:{os.environ['REDDIT_CLIENT_SECRET']}"
        basic = base64.b64encode(creds.encode()).decode()
        body = urllib.parse.urlencode({"grant_type": "client_credentials"}).encode()
        req = urllib.request.Request(
            self.token_url,
            data=body,
            headers={
                "Authorization": f"Basic {basic}",
                "User-Agent": self.user_agent,
                "Content-Type": "application/x-www-form-urlencoded",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=20) as response:
            data = json.loads(response.read().decode())
        self._token = data["access_token"]
        self._token_expires = time.time() + data.get("expires_in", 3600) - 60
        return self._token

    def _fetch(self, query: str, limit: int) -> tuple[list[SearchResult], str]:
        data = request_json(
            self.endpoint,
            headers={
                "Authorization": f"Bearer {self._get_token()}",
                "User-Agent": self.user_agent,
            },
            params={
                "q": query,
                "limit": limit,
                "sort": "relevance",
                "type": "link",
                # 'all' rather than the default year, so recurrence across
                # multiple years is visible.
                "t": "all",
            },
        )
        results = []
        for i, child in enumerate(data.get("data", {}).get("children", [])[:limit]):
            post = child.get("data", {})
            created = post.get("created_utc")
            stamp = (
                time.strftime("%Y-%m-%d", time.gmtime(created)) if created else "?"
            )
            results.append(
                SearchResult(
                    title=post.get("title", ""),
                    url="https://reddit.com" + post.get("permalink", ""),
                    snippet=(
                        f"r/{post.get('subreddit','?')} | {stamp} | "
                        f"{post.get('num_comments',0)} comments | "
                        f"{(post.get('selftext','') or '')[:200]}"
                    ),
                    rank=i + 1,
                    provider=self.name,
                )
            )
        return results, self.endpoint


class TrendsProvider(SearchProvider):
    """Google Trends.

    Deliberately opt-in: the endpoints Trends exposes are undocumented and
    change without notice. When it is off, trend direction is simply absent
    from the evidence rather than estimated -- an absent signal is honest, an
    invented one is not.
    """

    name = "trends"
    kind = KIND_TRENDS

    def available(self) -> bool:
        return os.environ.get("CAPYFIND_ENABLE_TRENDS") == "1"

    def why_unavailable(self) -> str:
        return "Trends is opt-in; set CAPYFIND_ENABLE_TRENDS=1 (undocumented endpoint)"

    def _fetch(self, query: str, limit: int) -> tuple[list[SearchResult], str]:
        raise RetrievalError(
            "Trends provider is a stub: wire pytrends or the Ads API here. "
            "Left unimplemented rather than returning a guessed curve."
        )
