"""The single interface every data source sits behind.

Swapping SerpAPI for Brave, or adding a grounded-LLM source, means adding one
class here -- nothing downstream changes. Each call returns both the results
and a `Retrieval` record, so proof of retrieval is produced by the provider
itself and cannot be fabricated by a caller further up the stack.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..models import Retrieval, SearchResult
from ..store import Store

# What a provider contributes. Used to decide which signals may be derived.
KIND_WEB = "web"
KIND_SOCIAL = "social"
KIND_SUGGEST = "suggest"
KIND_TRENDS = "trends"


class SearchProvider(ABC):
    name: str = "base"
    kind: str = KIND_WEB
    #: True when results are real retrievals; False for estimates or fixtures.
    live: bool = True
    #: Cap on queries per candidate. Free sources can afford several; ones that
    #: burn paid search credits are capped at one.
    max_queries: int = 3

    def __init__(self, store: Store | None = None) -> None:
        self.store = store

    @abstractmethod
    def available(self) -> bool:
        """Is this provider configured and usable right now?"""

    @abstractmethod
    def _fetch(self, query: str, limit: int) -> tuple[list[SearchResult], str]:
        """Do the work. Returns (results, endpoint_url)."""

    def why_unavailable(self) -> str:
        return f"{self.name} is not configured"

    def search(self, query: str, limit: int = 10) -> tuple[list[SearchResult], Retrieval]:
        """Cached, instrumented search. Never raises -- failures come back as a
        Retrieval with ok=False and an error message, so they can be recorded
        against the candidate and shown in the output."""
        variant = f"{self.kind}:{limit}"

        if self.store is not None:
            cached = self.store.cache_get(self.name, query, variant)
            if cached is not None:
                results = [SearchResult(**r) for r in cached]
                return results, Retrieval(
                    provider=self.name,
                    query=query,
                    role=self.kind,
                    n_results=len(results),
                    ok=True,
                    from_cache=True,
                    url=None,
                )

        if not self.available():
            return [], Retrieval(
                provider=self.name,
                query=query,
                role=self.kind,
                n_results=0,
                ok=False,
                error=self.why_unavailable(),
            )

        try:
            results, endpoint = self._fetch(query, limit)
        except Exception as exc:  # recorded, never silent
            return [], Retrieval(
                provider=self.name,
                query=query,
                role=self.kind,
                n_results=0,
                ok=False,
                error=f"{type(exc).__name__}: {exc}",
            )

        if self.store is not None:
            self.store.cache_put(
                self.name,
                query,
                [r.__dict__ for r in results],
                variant,
            )

        return results, Retrieval(
            provider=self.name,
            query=query,
            role=self.kind,
            n_results=len(results),
            ok=True,
            url=endpoint,
        )
