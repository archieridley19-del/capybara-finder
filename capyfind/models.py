"""Core data structures.

Two invariants are enforced here rather than by convention, because the v1 of
this tool failed on exactly these points:

1. A `Signal` cannot exist without a citation. `Signal.__post_init__` raises if
   the evidence list is empty, so an uncited positive cannot be constructed at
   all.
2. Retrieval is proven, not claimed. A `Run` counts `Retrieval` records that
   actually came back from a provider. `Run.verified` is False when nothing was
   fetched, and a score is never rendered for an unverified run.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

# Dimensions of the composite score, with their maximum values.
DIMENSIONS = {
    "demand": 4,
    "supply_weak": 4,
    "pay": 3,
    "reach": 3,
    "buildable": 1,
}

COMPETITOR_CLASSES = ("polished", "weak", "mismatched", "abandoned")


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Retrieval:
    """Proof that a provider call happened. Recorded whether it succeeded."""

    provider: str
    query: str
    ts: str = field(default_factory=utcnow)
    n_results: int = 0
    url: str | None = None
    ok: bool = True
    error: str | None = None
    from_cache: bool = False
    #: Which role produced this -- "web", "social", "suggest", "trends".
    role: str = "web"

    @property
    def productive(self) -> bool:
        """Did this call actually yield material to reason over?"""
        return self.ok and self.n_results > 0


@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str
    rank: int
    provider: str
    fetched_at: str = field(default_factory=utcnow)

    @property
    def domain(self) -> str:
        from urllib.parse import urlparse

        host = urlparse(self.url).netloc.lower()
        return host[4:] if host.startswith("www.") else host


@dataclass
class Evidence:
    """A single citable fact. `claim` is the one-liner shown next to a tick."""

    claim: str
    url: str
    snippet: str = ""
    source: str = ""
    ts: str = field(default_factory=utcnow)

    def __post_init__(self) -> None:
        if not self.claim.strip():
            raise ValueError("evidence needs a claim")
        if not self.url.strip():
            raise ValueError(f"evidence needs a source URL: {self.claim!r}")


@dataclass
class Signal:
    """A scored observation. Cannot exist without at least one citation."""

    dimension: str
    label: str
    weight: float
    evidence: list[Evidence] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.dimension not in DIMENSIONS:
            raise ValueError(f"unknown dimension {self.dimension!r}")
        if not self.evidence:
            # Requirement 4: a tick with no evidence behind it is a false tick.
            raise ValueError(
                f"signal {self.label!r} has no evidence; uncited signals are dropped"
            )


@dataclass
class Competitor:
    name: str
    url: str
    domain: str
    klass: str
    reasons: list[str] = field(default_factory=list)
    pricing: str | None = None
    target_user: str | None = None
    country: str | None = None
    last_updated: str | None = None
    review_sentiment: str | None = None
    #: False for generic suites -- they are supply, but not *dedicated* supply,
    #: so they do not belong in the demand-to-dedicated-supply ratio.
    dedicated: bool = True
    evidence: list[Evidence] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.klass not in COMPETITOR_CLASSES:
            raise ValueError(f"unknown competitor class {self.klass!r}")

    @property
    def is_strong_supply(self) -> bool:
        """Only a polished, correctly-targeted product counts as strong supply."""
        return self.klass == "polished"


@dataclass
class Score:
    demand: int = 0
    supply_weak: int = 0
    pay: int = 0
    reach: int = 0
    buildable: int = 0
    unverified: bool = True

    @property
    def composite(self) -> int:
        """Multiplicative: a zero in any essential dimension kills the score."""
        return (
            self.demand * self.supply_weak * self.pay * self.reach * self.buildable
        )

    @property
    def max_composite(self) -> int:
        result = 1
        for cap in DIMENSIONS.values():
            result *= cap
        return result

    def components(self) -> dict[str, int]:
        return {k: getattr(self, k) for k in DIMENSIONS}

    def display(self) -> str:
        """Requirement 1: never show a number for an ungrounded result."""
        return "UNVERIFIED" if self.unverified else str(self.composite)


@dataclass
class Blocker:
    """A buildability obstacle, with the source that evidenced it.

    `hard` blockers zero the score. `possible` blockers are surfaced in the
    report but do not kill on their own -- the word "licensed" appearing on
    some listicle is not proof that a niche is regulated.
    """

    claim: str
    severity: str  # 'hard' | 'possible'
    evidence: Evidence

    def __post_init__(self) -> None:
        if self.severity not in ("hard", "possible"):
            raise ValueError(f"unknown blocker severity {self.severity!r}")


@dataclass
class Failure:
    stage: str
    message: str
    ts: str = field(default_factory=utcnow)


@dataclass
class Run:
    """Everything learned about one candidate phrase, plus the audit trail."""

    candidate: str
    depth: str = "shallow"
    retrievals: list[Retrieval] = field(default_factory=list)
    results: list[SearchResult] = field(default_factory=list)
    signals: list[Signal] = field(default_factory=list)
    competitors: list[Competitor] = field(default_factory=list)
    reach_venues: list[Evidence] = field(default_factory=list)
    blockers: list[Blocker] = field(default_factory=list)
    failures: list[Failure] = field(default_factory=list)
    argument_against: str = ""
    score: Score = field(default_factory=Score)
    band: str = "unverified"
    kill_reason: str = ""
    started_at: str = field(default_factory=utcnow)
    finished_at: str | None = None

    # --- retrieval proof -------------------------------------------------

    @property
    def n_retrievals(self) -> int:
        return len(self.retrievals)

    @property
    def n_productive(self) -> int:
        return sum(1 for r in self.retrievals if r.productive)

    @property
    def supply_assessed(self) -> bool:
        """Did any web search actually run?

        Without one, page one was never looked at, so "no competitor found"
        means "nobody looked" -- which would turn a saturated niche into a
        maximum supply_weak score. Demand-side sources alone cannot carry a
        verdict.
        """
        return any(r.productive and r.role == "web" for r in self.retrievals)

    @property
    def verified(self) -> bool:
        """Gate for every result. Needs sources *and* an assessable supply
        side; either missing => UNVERIFIED."""
        return self.n_productive > 0 and bool(self.results) and self.supply_assessed

    # --- headline ratio --------------------------------------------------

    @property
    def dedicated_supply(self) -> int:
        """Dedicated products only. Listicles and directories are excluded
        upstream by `classify`, and generic suites are excluded here."""
        return sum(1 for c in self.competitors if c.dedicated)

    @property
    def demand_hits(self) -> int:
        return sum(1 for s in self.signals if s.dimension == "demand")

    @property
    def ratio(self) -> float:
        """Demand-to-dedicated-supply, mirroring the search-volume-to-results
        play. Denominator floors at 1 so a zero-supply niche stays finite."""
        return round(self.demand_hits / max(1, self.dedicated_supply), 2)

    @property
    def polished_competitors(self) -> list[Competitor]:
        return [c for c in self.competitors if c.is_strong_supply]

    @property
    def hard_blockers(self) -> list[Blocker]:
        return [b for b in self.blockers if b.severity == "hard"]

    def evidence_for(self, dimension: str) -> list[Evidence]:
        out: list[Evidence] = []
        for signal in self.signals:
            if signal.dimension == dimension:
                out.extend(signal.evidence)
        return out

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["composite"] = self.score.composite
        data["composite_display"] = self.score.display()
        data["ratio"] = self.ratio
        data["verified"] = self.verified
        data["n_retrievals"] = self.n_retrievals
        data["n_productive"] = self.n_productive
        data["dedicated_supply"] = self.dedicated_supply
        return data

    def to_json(self, indent: int | None = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False)


#: Keys added downstream for convenience that are not Run constructor fields --
#: `to_dict` derivations plus the lead marks the store folds into list rows.
_DERIVED = {
    "composite", "composite_display", "ratio", "verified",
    "n_retrievals", "n_productive", "dedicated_supply",
    "lead_status", "lead_notes",
}


def run_from_dict(data: dict[str, Any]) -> Run:
    """Rebuild a Run from stored JSON, for the table, CSV export and web UI."""
    payload = {k: v for k, v in data.items() if k not in _DERIVED}

    payload["retrievals"] = [Retrieval(**r) for r in payload.get("retrievals", [])]
    payload["results"] = [SearchResult(**r) for r in payload.get("results", [])]
    payload["failures"] = [Failure(**f) for f in payload.get("failures", [])]
    payload["reach_venues"] = [Evidence(**e) for e in payload.get("reach_venues", [])]
    payload["blockers"] = [
        Blocker(
            claim=b["claim"],
            severity=b["severity"],
            evidence=Evidence(**b["evidence"]),
        )
        for b in payload.get("blockers", [])
    ]
    payload["signals"] = [
        Signal(
            dimension=s["dimension"],
            label=s["label"],
            weight=s["weight"],
            evidence=[Evidence(**e) for e in s["evidence"]],
        )
        for s in payload.get("signals", [])
    ]
    payload["competitors"] = [
        Competitor(
            **{k: v for k, v in c.items() if k != "evidence"},
            evidence=[Evidence(**e) for e in c.get("evidence", [])],
        )
        for c in payload.get("competitors", [])
    ]
    payload["score"] = Score(**payload.get("score", {}))
    return Run(**payload)
