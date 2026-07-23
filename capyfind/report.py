"""Rendering: plain-English reports, ranked tables, CSV export.

The report deliberately puts the case *against* the niche above the score, so a
reader meets the objection before the number.
"""

from __future__ import annotations

import csv
import io

from .models import Run
from .scoring import ANCHOR_PROMPT, score_supply_weakness

BAND_MARK = {
    "pursue": "PURSUE",
    "investigate": "INVESTIGATE",
    "reject": "REJECT",
    "unverified": "UNVERIFIED",
}


def _rule(char: str = "-", width: int = 78) -> str:
    return char * width


def render_report(run: Run, show_anchors: bool = True) -> str:
    """The Part 2 written breakdown."""
    out: list[str] = []
    add = out.append

    add(_rule("="))
    add(f"  {run.candidate.upper()}")
    add(_rule("="))
    add("")

    # --- retrieval proof, before anything else --------------------------
    add("RETRIEVAL PROOF")
    add(
        f"  {run.n_retrievals} calls, {run.n_productive} productive, "
        f"{len(run.results)} unique sources"
    )
    for retrieval in run.retrievals:
        status = "ok" if retrieval.ok else "FAILED"
        cached = " (cached)" if retrieval.from_cache else ""
        add(
            f"    [{status}] {retrieval.provider:<12} {retrieval.n_results:>3} results"
            f"{cached}  {retrieval.query[:44]!r}"
        )
        if retrieval.error:
            add(f"             error: {retrieval.error[:100]}")
    add("")

    if not run.verified:
        add("VERDICT: UNVERIFIED")
        for line in _wrap(run.kill_reason, 74):
            add(f"  {line}")
        add("")
        if run.n_productive > 0 and not run.supply_assessed:
            add(
                "  No score is shown. Demand evidence alone cannot carry a\n"
                "  verdict: with no web search, page one was never looked at,\n"
                "  so 'no competitor found' would only mean 'nobody looked'.\n"
                "  That reads a saturated niche as a wide-open one."
            )
        else:
            add(
                "  No score is shown. Nothing usable was retrieved, so any\n"
                "  judgement here would be the model guessing -- the exact\n"
                "  failure this tool exists to prevent."
            )
        return "\n".join(out)

    # --- argument first --------------------------------------------------
    add("STRONGEST ARGUMENT AGAINST  (written before scoring)")
    for line in _wrap(run.argument_against, 74):
        add(f"  {line}")
    add("")

    # --- competitor teardown ---------------------------------------------
    add("COMPETITOR TEARDOWN")
    if not run.competitors:
        add("  No dedicated product found. Listicles and directories on page one")
        add("  were excluded and are not counted as supply.")
    for comp in run.competitors:
        flag = "**" if comp.is_strong_supply else "  "
        add(f"{flag}[{comp.klass.upper()}] {comp.name[:60]}")
        add(f"      {comp.url}")
        details = [
            f"pricing: {comp.pricing or 'none found'}",
            f"target: {comp.target_user or 'unclear'}",
            f"country: {comp.country or 'unclear'}",
            f"last signal: {comp.last_updated or 'unknown'}",
            f"dedicated: {'yes' if comp.dedicated else 'no (generic suite)'}",
        ]
        add(f"      {' | '.join(details)}")
        for reason in comp.reasons[:3]:
            add(f"      - {reason}")
    add("")

    # --- evidence by dimension -------------------------------------------
    for dimension, heading in (
        ("demand", "DEMAND EVIDENCE"),
        ("pay", "PAYMENT EVIDENCE"),
    ):
        add(heading)
        signals = [s for s in run.signals if s.dimension == dimension]
        if not signals:
            add("  none found")
        for signal in signals:
            add(f"  + {signal.label}")
            for ev in signal.evidence[:3]:
                add(f"      {ev.claim[:70]}")
                add(f"      {ev.url}")
        add("")

    add("REACHABILITY")
    if not run.reach_venues:
        add("  No specific venue identified. An audience with no gathering place")
        add("  cannot be reached cheaply -- this alone can kill a good idea.")
    for venue in run.reach_venues[:8]:
        add(f"  + {venue.claim}  {venue.url}")
    add("")

    add("BUILDABILITY")
    if run.blockers:
        for blocker in run.blockers:
            label = "HARD BLOCKER" if blocker.severity == "hard" else "possible blocker"
            add(f"  {label}: {blocker.claim}")
            add(f"      {blocker.evidence.url}")
        if run.hard_blockers:
            add("  Scope: irrelevant until the hard blocker above is resolved.")
        else:
            add("  None of these are confirmed. They came from unofficial sources")
            add("  and do not reduce the score -- verify them by hand.")
    else:
        add("  No hard blocker detected in retrieved text.")
        add("  Note: absence of evidence is not proof. Accreditation requirements")
        add("  are often invisible in search results -- confirm with the scheme")
        add("  body before building.")
    add("")

    if run.failures:
        add("FAILURES")
        for failure in run.failures:
            add(f"  [{failure.stage}] {failure.message[:100]}")
        add("")

    # --- score, last -----------------------------------------------------
    supply_score, supply_why = score_supply_weakness(run)
    add("SCORE")
    components = run.score.components()
    add(
        f"  demand {components['demand']}/4   "
        f"supply_weak {components['supply_weak']}/4   "
        f"pay {components['pay']}/3   "
        f"reach {components['reach']}/3   "
        f"buildable {components['buildable']}/1"
    )
    add(f"  supply note: {supply_why}")
    add(
        f"  composite = {components['demand']} x {components['supply_weak']} x "
        f"{components['pay']} x {components['reach']} x {components['buildable']} "
        f"= {run.score.display()} / {run.score.max_composite}"
    )
    add(f"  demand-to-dedicated-supply ratio: {run.ratio} "
        f"({run.demand_hits} demand signals / {run.dedicated_supply} dedicated products)")
    add("")
    add(f"VERDICT: {BAND_MARK.get(run.band, run.band.upper())}")
    add(f"  {run.kill_reason}")
    add("")

    if show_anchors:
        add("CALIBRATION ANCHORS")
        for line in ANCHOR_PROMPT.splitlines():
            add(f"  {line}")
        add("")

    add("NEXT ACTION")
    for line in _wrap(_next_action(run), 74):
        add(f"  {line}")
    add("")
    add(_rule())
    add(
        "This output is a shortlist, not a decision. The only real validation is\n"
        "a priced landing page shown to real users. No amount of searching\n"
        "replaces that step."
    )
    return "\n".join(out)


def _next_action(run: Run) -> str:
    if run.band == "reject":
        return (
            f"Drop it. {run.kill_reason}. Move to the next candidate rather than "
            "hunting for an angle that rescues this one -- that instinct is what "
            "produced the false positives in v1."
        )
    if run.band == "investigate":
        weakest = min(run.score.components(), key=lambda k: run.score.components()[k])
        return (
            f"Do not build yet. The binding constraint is '{weakest}' "
            f"({run.score.components()[weakest]}). Go and disprove that one "
            "dimension by hand before spending any more compute here."
        )
    if run.band == "pursue":
        return (
            "Rare enough to be suspicious. Manually open the top 10 results and "
            "confirm no polished incumbent was missed, then put up a priced "
            "landing page and take it to the venues listed above. If 30 candidates "
            "produced more than 2-3 of these, tighten the thresholds first."
        )
    return "Fix retrieval and re-run. There is nothing to judge yet."


def _wrap(text: str, width: int) -> list[str]:
    import textwrap

    lines: list[str] = []
    for para in text.split("\n"):
        lines.extend(textwrap.wrap(para, width) or [""])
    return lines


# --- tables ------------------------------------------------------------------

TABLE_COLUMNS = (
    ("candidate", 38),
    ("dem", 3),
    ("sup", 3),
    ("pay", 3),
    ("rch", 3),
    ("bld", 3),
    ("ratio", 6),
    ("score", 6),
    ("verdict", 12),
)


def render_table(runs: list[Run], survivors_only: bool = False) -> str:
    rows = [r for r in runs if not survivors_only or (r.verified and r.score.composite > 0)]
    if not rows:
        return "no candidates to show"

    header = " ".join(name.ljust(width) for name, width in TABLE_COLUMNS)
    out = [header, "-" * len(header)]

    for run in rows:
        components = run.score.components()
        cells = [
            run.candidate[:38].ljust(38),
            ("-" if run.score.unverified else str(components["demand"])).ljust(3),
            ("-" if run.score.unverified else str(components["supply_weak"])).ljust(3),
            ("-" if run.score.unverified else str(components["pay"])).ljust(3),
            ("-" if run.score.unverified else str(components["reach"])).ljust(3),
            ("-" if run.score.unverified else str(components["buildable"])).ljust(3),
            str(run.ratio).ljust(6),
            run.score.display()[:6].ljust(6),
            BAND_MARK.get(run.band, run.band)[:12].ljust(12),
        ]
        out.append(" ".join(cells))
        out.append(f"    ^ {run.kill_reason[:90]}")

    return "\n".join(out)


def to_csv(runs: list[Run]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "candidate", "verdict", "composite", "demand", "supply_weak", "pay",
            "reach", "buildable", "ratio", "demand_signals", "dedicated_supply",
            "polished_competitors", "retrievals", "productive_retrievals",
            "kill_reason", "argument_against", "top_competitor_urls",
        ]
    )
    for run in runs:
        components = run.score.components()
        writer.writerow(
            [
                run.candidate,
                run.band,
                run.score.display(),
                components["demand"],
                components["supply_weak"],
                components["pay"],
                components["reach"],
                components["buildable"],
                run.ratio,
                run.demand_hits,
                run.dedicated_supply,
                len(run.polished_competitors),
                run.n_retrievals,
                run.n_productive,
                run.kill_reason,
                run.argument_against,
                " | ".join(c.url for c in run.competitors[:5]),
            ]
        )
    return buffer.getvalue()
