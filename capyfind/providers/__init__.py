"""Provider registry.

Callers ask for a *role* ("web", "social") rather than a vendor, so swapping
Serper for Brave is a config change. A role can hold several providers: the
social role fans out across Reddit, Stack Exchange, Hacker News and the UK
trade forums, because no single one of them covers a niche audience.

`describe()` reports which roles are live, which are missing and why -- the CLI
prints this before every run so it is always obvious which signals came from
real data.
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
from .forums import HackerNewsProvider, StackExchangeProvider, TradeForumProvider
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

ROLE_ORDER = (KIND_WEB, KIND_SUGGEST, KIND_SOCIAL, KIND_TRENDS)


class ProviderSet:
    def __init__(
        self, roles: dict[str, list[SearchProvider]], notes: dict[str, str] | None = None
    ):
        self.roles = roles
        self.notes = notes or {}

    def all(self, role: str) -> list[SearchProvider]:
        """Every configured and currently usable provider for this role."""
        return [p for p in self.roles.get(role, []) if p.available()]

    def get(self, role: str) -> SearchProvider | None:
        """The primary provider for a role, or None."""
        usable = self.all(role)
        return usable[0] if usable else None

    @property
    def any_live(self) -> bool:
        return any(
            p.live for role in self.roles for p in self.all(role)
        )

    @property
    def fixture_mode(self) -> bool:
        return any(not p.live for role in self.roles for p in self.roles[role])

    def describe(self) -> list[str]:
        lines = []
        for role in ROLE_ORDER:
            configured = self.roles.get(role, [])
            if not configured:
                lines.append(f"  {role:<10} -- none ({self.notes.get(role, 'n/a')})")
                continue
            for provider in configured:
                if provider.available():
                    tag = "LIVE" if provider.live else "FIXTURE"
                    lines.append(f"  {role:<10} -- {provider.name} [{tag}]")
                else:
                    lines.append(
                        f"  {role:<10} -- {provider.name} unavailable: "
                        f"{provider.why_unavailable()}"
                    )
        return lines


def build_providers(store: Store | None = None, use_fixtures: bool = False) -> ProviderSet:
    if use_fixtures:
        return ProviderSet(
            roles={
                KIND_WEB: [FixtureProvider(kind="web")],
                KIND_SOCIAL: [FixtureProvider(kind="social")],
                KIND_SUGGEST: [FixtureProvider(kind="suggest")],
                KIND_TRENDS: [],
            },
            notes={KIND_TRENDS: "not recorded in fixtures"},
        )

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

    return ProviderSet(
        roles={
            KIND_WEB: [web],
            KIND_SUGGEST: [AutocompleteProvider(store=store)],
            # Order matters: free, high-signal sources first, so a candidate
            # can reach `verified` before any paid credit is spent.
            KIND_SOCIAL: [
                StackExchangeProvider(store=store),
                HackerNewsProvider(store=store),
                RedditProvider(store=store),
                TradeForumProvider(store=store, web=web),
            ],
            KIND_TRENDS: [TrendsProvider(store=store)],
        },
        notes=notes,
    )
