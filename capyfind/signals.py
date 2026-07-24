"""Turning raw results into cited signals.

Every function here returns `Signal` objects, and `Signal` refuses to be built
without evidence. So a dimension can only be scored on things that have a
clickable source behind them.
"""

from __future__ import annotations

import re
from collections import Counter

from .classify import GENERIC_VENUES, classify_result, domain_in
from .models import Evidence, SearchResult, Signal

ASK_PATTERNS = re.compile(
    r"(is there (a|an|any) (tool|app|software|way))|(why is there no)"
    r"|(looking for (a|an|any)? ?(tool|app|software))|(does anyone know of)"
    r"|(any recommendations for)|(need (a|an) (tool|app|way) to)"
    r"|(how do (you|i) (all )?(handle|track|manage|do))"
    r"|(anyone found (a|an|any))|(alternative to)",
    re.IGNORECASE,
)
SPREADSHEET_PAIN = re.compile(
    r"(spreadsheet)|(excel)|(google sheets?)|(manually)|(by hand)|(pen and paper)"
    r"|(paper (form|record))|(word doc)",
    re.IGNORECASE,
)
PRICE_COMPLAINT = re.compile(
    r"(cheaper alternative)|(too expensive)|(overpriced)|(can'?t afford)"
    r"|(price (went up|increase|hike))|(rip[- ]?off)|(why so expensive)"
    r"|(cheaper than)|(free alternative to)|(not worth (the|£|\$))",
    re.IGNORECASE,
)
PAYING_HINT = re.compile(
    r"(i pay)|(we pay)|(paying)|(subscription)|(per month)|(a month)"
    r"|(£\d)|(\$\d)|(quote|invoice)d? for",
    re.IGNORECASE,
)
DATE_IN_SNIPPET = re.compile(r"\b(20\d{2})-\d{2}-\d{2}\b")
SUBREDDIT = re.compile(r"\br/([A-Za-z0-9_]{2,30})\b")


def _ev(result: SearchResult, claim: str) -> Evidence:
    return Evidence(
        claim=claim,
        url=result.url,
        snippet=result.snippet[:300],
        source=result.domain,
    )


# --- demand ------------------------------------------------------------------


def demand_signals(
    suggestions: list[SearchResult], threads: list[SearchResult]
) -> list[Signal]:
    signals: list[Signal] = []

    if len(suggestions) >= 3:
        signals.append(
            Signal(
                dimension="demand",
                label=f"{len(suggestions)} autocomplete completions for this phrase",
                weight=1.0,
                evidence=[
                    _ev(s, f"autocomplete: {s.title!r}") for s in suggestions[:5]
                ],
            )
        )

    asking = [t for t in threads if ASK_PATTERNS.search(f"{t.title} {t.snippet}")]
    if len(threads) >= 2:
        signals.append(
            Signal(
                dimension="demand",
                label=f"{len(threads)} forum threads on this task",
                weight=1.0,
                evidence=[_ev(t, f"thread: {t.title[:90]}") for t in threads[:5]],
            )
        )

    if asking:
        signals.append(
            Signal(
                dimension="demand",
                label=f"{len(asking)} threads explicitly asking for a tool",
                weight=1.0,
                evidence=[
                    _ev(t, f"explicit ask: {t.title[:90]}") for t in asking[:5]
                ],
            )
        )

    years = Counter()
    year_examples: dict[str, SearchResult] = {}
    for thread in threads:
        for year in DATE_IN_SNIPPET.findall(thread.snippet):
            years[year] += 1
            year_examples.setdefault(year, thread)
    if len(years) >= 2:
        span = f"{min(years)}-{max(years)}"
        signals.append(
            Signal(
                dimension="demand",
                label=f"question recurs across {len(years)} separate years ({span})",
                weight=1.0,
                evidence=[
                    _ev(year_examples[y], f"asked again in {y}")
                    for y in sorted(year_examples)[:4]
                ],
            )
        )

    pain = [t for t in threads if SPREADSHEET_PAIN.search(f"{t.title} {t.snippet}")]
    if pain:
        signals.append(
            Signal(
                dimension="demand",
                label="people describe doing this manually or in a spreadsheet",
                weight=0.5,
                evidence=[_ev(t, f"manual workaround: {t.title[:90]}") for t in pain[:3]],
            )
        )

    return signals


# --- willingness to pay ------------------------------------------------------


def pay_signals(competitors, threads: list[SearchResult]) -> list[Signal]:
    """Searching is not paying. These are the only three things that count as
    evidence money moves in this area."""
    signals: list[Signal] = []

    priced = [c for c in competitors if c.pricing]
    if priced:
        signals.append(
            Signal(
                dimension="pay",
                label=f"{len(priced)} existing products charge for this",
                weight=1.0,
                evidence=[
                    Evidence(
                        claim=f"{c.name[:60]} shows pricing ({c.pricing})",
                        url=c.url,
                        source=c.domain,
                    )
                    for c in priced[:4]
                ],
            )
        )

    complaints = [t for t in threads if PRICE_COMPLAINT.search(f"{t.title} {t.snippet}")]
    if complaints:
        # The strongest buy signal available: someone already opened their wallet
        # and resents the price.
        signals.append(
            Signal(
                dimension="pay",
                label="users complain about incumbent pricing",
                weight=1.5,
                evidence=[_ev(t, f"price complaint: {t.title[:90]}") for t in complaints[:4]],
            )
        )

    paying = [
        t
        for t in threads
        if PAYING_HINT.search(f"{t.title} {t.snippet}")
        and t not in complaints
    ]
    if paying:
        signals.append(
            Signal(
                dimension="pay",
                label="users mention paying for tools in this area",
                weight=1.0,
                evidence=[_ev(t, f"spend mentioned: {t.title[:90]}") for t in paying[:3]],
            )
        )

    return signals


# --- reachability ------------------------------------------------------------


def reach_evidence(threads: list[SearchResult], web: list[SearchResult]) -> list[Evidence]:
    """Name the specific places these people gather.

    Only *niche* venues count. A named subreddit or a trade forum is a
    distribution channel; Hacker News, YouTube and Stack Overflow are not, so
    they are excluded even though they are perfectly good demand evidence. An
    audience you cannot reach kills an otherwise good idea, so an empty list
    here scores zero rather than being fudged upward.
    """
    found: dict[str, Evidence] = {}

    for thread in threads:
        for match in SUBREDDIT.findall(f"{thread.title} {thread.snippet} {thread.url}"):
            key = f"r/{match}".lower()
            found.setdefault(
                key,
                Evidence(
                    claim=f"subreddit: r/{match}",
                    url=f"https://reddit.com/r/{match}",
                    snippet=thread.title[:150],
                    source="reddit",
                ),
            )

    for result in web + threads:
        if domain_in(result.domain, GENERIC_VENUES) or "reddit.com" in result.domain:
            continue
        kind, _ = classify_result(result)
        if kind == "community":
            found.setdefault(
                result.domain,
                Evidence(
                    claim=f"trade forum: {result.domain}",
                    url=result.url,
                    snippet=result.title[:150],
                    source=result.domain,
                ),
            )

    return list(found.values())
