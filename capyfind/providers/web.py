"""Web search providers.

All are paid/official APIs. Scraping Google SERPs is deliberately not
implemented: it breaks their terms and gets blocked within a few dozen
requests, which is exactly the failure mode this tool cannot tolerate.
"""

from __future__ import annotations

import os

from ..http import request_json
from ..models import SearchResult
from .base import KIND_WEB, SearchProvider


class SerperProvider(SearchProvider):
    """serper.dev -- cheapest reliable Google SERP access."""

    name = "serper"
    kind = KIND_WEB
    endpoint = "https://google.serper.dev/search"

    def available(self) -> bool:
        return bool(os.environ.get("SERPER_API_KEY"))

    def why_unavailable(self) -> str:
        return "SERPER_API_KEY is not set"

    def _fetch(self, query: str, limit: int) -> tuple[list[SearchResult], str]:
        gl = os.environ.get("CAPYFIND_COUNTRY", "uk").lower()
        data = request_json(
            self.endpoint,
            method="POST",
            headers={"X-API-KEY": os.environ["SERPER_API_KEY"]},
            payload={"q": query, "num": limit, "gl": gl},
        )
        results = [
            SearchResult(
                title=item.get("title", ""),
                url=item.get("link", ""),
                snippet=item.get("snippet", ""),
                rank=i + 1,
                provider=self.name,
            )
            for i, item in enumerate(data.get("organic", [])[:limit])
            if item.get("link")
        ]
        return results, self.endpoint


class BraveProvider(SearchProvider):
    """Brave Search API -- independent index, generous free tier."""

    name = "brave"
    kind = KIND_WEB
    endpoint = "https://api.search.brave.com/res/v1/web/search"

    def available(self) -> bool:
        return bool(os.environ.get("BRAVE_API_KEY"))

    def why_unavailable(self) -> str:
        return "BRAVE_API_KEY is not set"

    def _fetch(self, query: str, limit: int) -> tuple[list[SearchResult], str]:
        country = os.environ.get("CAPYFIND_COUNTRY", "GB").upper()
        data = request_json(
            self.endpoint,
            headers={
                "X-Subscription-Token": os.environ["BRAVE_API_KEY"],
                "Accept": "application/json",
            },
            params={"q": query, "count": limit, "country": country},
        )
        results = [
            SearchResult(
                title=item.get("title", ""),
                url=item.get("url", ""),
                snippet=item.get("description", ""),
                rank=i + 1,
                provider=self.name,
            )
            for i, item in enumerate(data.get("web", {}).get("results", [])[:limit])
            if item.get("url")
        ]
        return results, self.endpoint


class SerpApiProvider(SearchProvider):
    name = "serpapi"
    kind = KIND_WEB
    endpoint = "https://serpapi.com/search.json"

    def available(self) -> bool:
        return bool(os.environ.get("SERPAPI_API_KEY"))

    def why_unavailable(self) -> str:
        return "SERPAPI_API_KEY is not set"

    def _fetch(self, query: str, limit: int) -> tuple[list[SearchResult], str]:
        data = request_json(
            self.endpoint,
            params={
                "q": query,
                "num": limit,
                "gl": os.environ.get("CAPYFIND_COUNTRY", "uk").lower(),
                "api_key": os.environ["SERPAPI_API_KEY"],
                "engine": "google",
            },
        )
        results = [
            SearchResult(
                title=item.get("title", ""),
                url=item.get("link", ""),
                snippet=item.get("snippet", ""),
                rank=i + 1,
                provider=self.name,
            )
            for i, item in enumerate(data.get("organic_results", [])[:limit])
            if item.get("link")
        ]
        return results, self.endpoint
