"""The scoring model, its bands, and the argument-first discipline.

Formula
-------

    composite = demand x supply_weak x pay x reach x buildable

              demand      0-4   evidence-backed demand signals
              supply_weak 0-4   how badly served the niche is
              pay         0-3   evidence money moves here
              reach       0-3   named places the audience gathers
              buildable   0/1   hard blocker present = 0

Multiplicative on purpose: a gap nobody pays for, or that you cannot reach, is
not a business, and a zero in any essential dimension should annihilate the
score rather than be averaged away. Maximum is 144.

Calibration
-----------

Bands are set against known-saturated anchors (see ANCHORS). "convert bank
statement pdf to excel" has enormous demand and must still reject, because a
single polished on-target competitor forces supply_weak to 0. If a 30-candidate
run returns more than 2-3 `pursue` verdicts, the thresholds are wrong, not the
market -- `calibration_warning()` reports exactly that.
"""

from __future__ import annotations

from .models import Run, Score, Signal

BAND_PURSUE = "pursue"
BAND_INVESTIGATE = "investigate"
BAND_REJECT = "reject"
BAND_UNVERIFIED = "unverified"

PURSUE_MIN = 48
INVESTIGATE_MIN = 12

#: Supply weight per competitor class. Polished is handled as a hard kill,
#: the rest accumulate: three abandoned projects are not three competitors.
SUPPLY_WEIGHT = {
    "polished": 3.0,
    "weak": 0.8,
    "mismatched": 0.5,
    "abandoned": 0.4,
}

#: Fixed reference points included in every judgement, so scores are relative
#: to known reality rather than to the model's mood.
ANCHORS: dict[str, dict] = {
    "convert bank statement pdf to excel": {
        "expected_band": BAND_REJECT,
        "why": "huge demand, but multiple polished dedicated converters exist "
        "(DocuClipper, Docparser, MoneyThumb). Demand never rescues a niche "
        "with a polished on-target incumbent.",
    },
    "resize image for instagram": {
        "expected_band": BAND_REJECT,
        "why": "enormous demand, saturated by free polished tools plus native "
        "platform features. Free incumbents crush willingness to pay.",
    },
    "invoice generator": {
        "expected_band": BAND_REJECT,
        "why": "generic, commoditised, bundled into every accounting suite.",
    },
}

ANCHOR_PROMPT = "\n".join(
    f"- {phrase!r} => {data['expected_band'].upper()}: {data['why']}"
    for phrase, data in ANCHORS.items()
)


def _cap(value: float, maximum: int) -> int:
    return max(0, min(maximum, int(value)))


# --- dimensions --------------------------------------------------------------


def score_demand(signals: list[Signal]) -> int:
    demand = [s for s in signals if s.dimension == "demand"]
    if not demand:
        return 0
    return _cap(sum(s.weight for s in demand), 4)


def score_supply_weakness(run: Run) -> tuple[int, str]:
    """Returns (score, explanation).

    Requirement 2: supply is judged on quality and fit, not existence. Only a
    polished, affordable, actively-maintained, correctly-targeted product
    counts as strong supply -- and one of those is enough to close the niche.
    """
    if run.polished_competitors:
        names = ", ".join(c.name[:40] for c in run.polished_competitors[:3])
        return 0, f"polished on-target competitor(s) present: {names}"

    if not run.competitors:
        return 4, "no dedicated product found on page one"

    load = sum(SUPPLY_WEIGHT.get(c.klass, 1.0) for c in run.competitors)
    breakdown = ", ".join(f"{c.klass}" for c in run.competitors[:6])

    if load < 0.5:
        return 4, f"only trivial supply ({breakdown})"
    if load < 1.2:
        return 3, f"one weak/mismatched product ({breakdown})"
    if load < 2.0:
        return 2, f"a couple of imperfect products ({breakdown})"
    if load < 3.5:
        return 1, f"several imperfect products ({breakdown})"
    return 0, f"crowded with dedicated products ({breakdown})"


def score_pay(signals: list[Signal]) -> int:
    pay = [s for s in signals if s.dimension == "pay"]
    if not pay:
        return 0
    return _cap(sum(s.weight for s in pay), 3)


def score_reach(run: Run) -> int:
    venues = len(run.reach_venues)
    if venues == 0:
        return 0
    if venues == 1:
        return 1
    if venues <= 3:
        return 2
    return 3


def score_buildable(run: Run) -> int:
    """Only evidenced, mandatory blockers kill. See `classify.detect_blockers`."""
    return 0 if run.hard_blockers else 1


# --- argument-first ----------------------------------------------------------


def build_argument_against(run: Run) -> str:
    """Requirement 3: write the strongest case *against* before scoring.

    Called by the verifier before `score_run`, never after, so the score is
    formed in the presence of the objection rather than justified afterwards.
    """
    points: list[str] = []

    if run.polished_competitors:
        top = run.polished_competitors[0]
        points.append(
            f"{top.name[:60]} ({top.domain}) already serves this exact task and "
            f"looks polished{f' at {top.pricing}' if top.pricing else ''}. "
            "A second entrant needs a reason to exist that this research has not found."
        )
    elif run.competitors:
        points.append(
            f"{len(run.competitors)} dedicated product(s) already occupy this space "
            f"({', '.join(c.klass for c in run.competitors[:4])}). Even imperfect "
            "incumbents own the search results and the switching inertia."
        )

    if not run.evidence_for("pay"):
        points.append(
            "No evidence anyone spends money here. Search demand is not revenue; "
            "a free workaround or a spreadsheet is the real competitor."
        )

    if not run.reach_venues:
        points.append(
            "No identifiable place this audience gathers, so there is no cheap "
            "route to the first ten customers."
        )

    if run.hard_blockers:
        points.append(
            "Hard blocker: "
            + "; ".join(b.claim for b in run.hard_blockers)
            + ". This disqualifies the niche outright regardless of how good "
            "the gap looks."
        )

    if run.demand_hits <= 1:
        points.append(
            "Demand evidence is thin -- this may be a phrase the generator "
            "invented rather than one people actually type."
        )

    if not points:
        points.append(
            "The clearest risk is that this looks good only because retrieval was "
            "shallow. A wider sweep usually surfaces an incumbent that page one hid."
        )

    return " ".join(points)


# --- composite ---------------------------------------------------------------


def score_run(run: Run) -> Score:
    """Compute the composite. Assumes `run.argument_against` is already set."""
    if not run.verified:
        return Score(unverified=True)

    supply, _ = score_supply_weakness(run)
    return Score(
        demand=score_demand(run.signals),
        supply_weak=supply,
        pay=score_pay(run.signals),
        reach=score_reach(run),
        buildable=score_buildable(run),
        unverified=False,
    )


def band_for(run: Run) -> str:
    if not run.verified or run.score.unverified:
        return BAND_UNVERIFIED

    score = run.score
    if run.polished_competitors or score.composite == 0:
        return BAND_REJECT
    if (
        score.composite >= PURSUE_MIN
        and score.demand >= 3
        and score.supply_weak >= 3
        and score.pay >= 1
    ):
        return BAND_PURSUE
    if score.composite >= INVESTIGATE_MIN:
        return BAND_INVESTIGATE
    return BAND_REJECT


def kill_reason(run: Run) -> str:
    """One line explaining the verdict. Always populated, including for wins."""
    if not run.verified:
        n = run.n_retrievals
        if n == 0:
            return "UNVERIFIED: no retrieval attempted"
        if run.n_productive > 0 and not run.supply_assessed:
            # The dangerous case: demand evidence but nobody looked at page one.
            # Scoring supply here would read "saturated" as "wide open".
            return (
                "UNVERIFIED: demand sources returned results but no web search "
                "ran, so supply was never assessed. Set SERPER_API_KEY, "
                "BRAVE_API_KEY or SERPAPI_API_KEY."
            )
        errors = {f.message for f in run.failures} or {
            r.error for r in run.retrievals if r.error
        }
        detail = "; ".join(sorted(e for e in errors if e))[:160]
        return f"UNVERIFIED: {n} retrievals, none productive. {detail}".strip()

    if run.polished_competitors:
        top = run.polished_competitors[0]
        return f"polished incumbent: {top.name[:50]} ({top.domain})"
    if run.score.buildable == 0:
        return f"hard blocker: {run.hard_blockers[0].claim[:100]}"
    if run.score.pay == 0:
        return "no evidence anyone pays in this area"
    if run.score.reach == 0:
        return "no identifiable place to reach this audience"
    if run.score.demand == 0:
        return "no demand evidence found"

    supply_score, why = score_supply_weakness(run)
    if supply_score <= 1:
        return f"supply already adequate: {why[:100]}"
    if run.band == "pursue":
        return f"survived: ratio {run.ratio}, no polished incumbent"
    return f"partial gap: {why[:100]}"


def finalise(run: Run) -> Run:
    """Argument first, score second, band and kill reason last."""
    run.argument_against = build_argument_against(run)
    run.score = score_run(run)
    run.band = band_for(run)
    run.kill_reason = kill_reason(run)
    return run


def calibration_warning(runs: list[Run]) -> str | None:
    """A run of 30 candidates returning >2-3 pursues is a bug, not a goldmine."""
    verified = [r for r in runs if r.verified]
    if len(verified) < 10:
        return None
    pursues = [r for r in verified if r.band == BAND_PURSUE]
    allowed = max(3, int(len(verified) * 0.1))
    if len(pursues) > allowed:
        return (
            f"CALIBRATION: {len(pursues)}/{len(verified)} candidates scored "
            f"'{BAND_PURSUE}' (expected <= {allowed}). Treat this as a bug in the "
            "thresholds or the supply classifier, not as a discovery. Tighten "
            "PURSUE_MIN or check whether listicles are being counted as products."
        )
    return None
