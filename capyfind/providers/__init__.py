"""Provider registry.

Callers ask for a *role* ("web", "social") rather than a vendor, so swapping
Serper for Brave is a config change. `describe()` reports which roles are live,
which are missing and why -- the CLI prints this before every run so it is
always obvious which signals came from real data.
"""

from __future__ import annotations

from ..store import Store
from .base import (
    KIND_SOCIAL,
    KIND_SUGGEST,
    KIND_TRENDS,
    KIND_WEB,
    SearchProvider,
)
from .fixture import FixtureProvider
from .social import AutocompleteProvider, RedditProvider, TrendsProvider
from .web import BraveProvider, SerpApiProvider, SerperProvider

#: First available wins, cheapest first.
WEB_PRIORITY = (SerperProvider, BraveProvider, SerpApiProvider)

__all__ = [
    "SearchProvider",
    "ProviderSet",
    "build_providers",
    "KIND_WEB",
    "KIND_SOCIAL",
    "KIND_SUGGEST",
    "KIND_TRENDS",
]


class ProviderSet:
    def __init__(self, roles: dict[str, SearchProvider | None], notes: dict[str, str]):
        self.roles = roles
        self.notes = notes

    def get(self, role: str) -> SearchProvider | None:
        provider = self.roles.get(role)
        if provider is not None and provider.available():
            return provider
        return None

    @property
    def any_live(self) -> bool:
        return any(
            p is not None and p.live and p.available() for p in self.roles.values()
        )

    @property
    def fixture_mode(self) -> bool:
        return any(
            p is not None and not p.live for p in self.roles.values() if p is not None
        )

    def describe(self) -> list[str]:
        lines = []
        for role in (KIND_WEB, KIND_SUGGEST, KIND_SOCIAL, KIND_TRENDS):
            provider = self.roles.get(role)
            if provider is None:
                lines.append(f"  {role:<10} -- none ({self.notes.get(role, 'n/a')})")
            elif not provider.available():
                lines.append(f"  {role:<10} -- {provider.name} unavailable: {provider.why_unavailable()}")
            else:
                tag = "LIVE" if provider.live else "FIXTURE"
                lines.append(f"  {role:<10} -- {provider.name} [{tag}]")
        return lines


def build_providers(store: Store | None = None, use_fixtures: bool = False) -> ProviderSet:
    if use_fixtures:
        return ProviderSet(
            roles={
                KIND_WEB: FixtureProvider(kind="web"),
                KIND_SOCIAL: FixtureProvider(kind="social"),
                KIND_SUGGEST: FixtureProvider(kind="suggest"),
                KIND_TRENDS: None,
            },
            notes={KIND_TRENDS: "not recorded in fixtures"},
        )

    roles: dict[str, SearchProvider | None] = {}
    notes: dict[str, str] = {}

    web: SearchProvider | None = None
    for cls in WEB_PRIORITY:
        candidate = cls(store=store)
        if candidate.available():
            web = candidate
            break
    if web is None:
        web = SerperProvider(store=store)
        notes[KIND_WEB] = "set SERPER_API_KEY, BRAVE_API_KEY or SERPAPI_API_KEY"
    roles[KIND_WEB] = web

    roles[KIND_SUGGEST] = AutocompleteProvider(store=store)
    roles[KIND_SOCIAL] = RedditProvider(store=store)
    roles[KIND_TRENDS] = TrendsProvider(store=store)

    return ProviderSet(roles=roles, notes=notes)
