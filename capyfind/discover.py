"""Part 1 -- wide, cheap sweep across many candidates.

Runs the same verifier as Part 2 at shallow depth. Sharing the pipeline matters:
discovery and verification cannot drift apart and start disagreeing about what
counts as a competitor.

Batch behaviour:
  - already-researched candidates are skipped on re-run (resumable)
  - the batch stops after N consecutive failures instead of burning the list
  - every failure is recorded against its candidate and shown in the output
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from .generate import build_candidates
from .http import CircuitBreaker, CircuitOpen
from .models import Run
from .providers import ProviderSet
from .scoring import calibration_warning
from .store import Store
from .verify import verify


@dataclass
class BatchResult:
    seed: str
    runs: list[Run] = field(default_factory=list)
    skipped: int = 0
    stopped_early: str = ""
    warning: str | None = None

    @property
    def survivors(self) -> list[Run]:
        return [r for r in self.runs if r.verified and r.score.composite > 0]

    def ranked(self) -> list[Run]:
        """Verified first, then by composite. Unverified never floats to the top."""
        return sorted(
            self.runs,
            key=lambda r: (r.verified, r.score.composite, r.ratio),
            reverse=True,
        )


def discover(
    seed: str,
    providers: ProviderSet,
    *,
    limit: int = 60,
    country: str = "UK",
    buyer_size: str = "sole trader",
    store: Store | None = None,
    resume: bool = True,
    tasks_file: Path | None = None,
    failure_threshold: int = 3,
    on_progress: Callable[[int, int, Run | None, str], None] | None = None,
    candidates: Iterable[str] | None = None,
) -> BatchResult:
    phrases = list(candidates) if candidates is not None else build_candidates(
        seed, providers=providers, limit=limit, country=country, tasks_file=tasks_file
    )
    batch = BatchResult(seed=seed)
    breaker = CircuitBreaker(threshold=failure_threshold)
    total = len(phrases)

    for index, phrase in enumerate(phrases, start=1):
        if resume and store is not None and store.has_run(phrase, "shallow"):
            batch.skipped += 1
            if on_progress:
                on_progress(index, total, None, "skipped (cached)")
            continue

        try:
            breaker.check()
        except CircuitOpen as exc:
            batch.stopped_early = str(exc)
            break

        run = verify(
            phrase,
            providers,
            depth="shallow",
            country=country,
            buyer_size=buyer_size,
            store=None,
        )

        # A run that retrieved nothing at all counts as an infrastructure
        # failure, not as a boring niche.
        if run.n_productive == 0:
            breaker.record_failure(run.kill_reason or "no productive retrieval")
        else:
            breaker.record_success()

        batch.runs.append(run)
        if store is not None:
            store.save_run(run, seed=seed)
        if on_progress:
            on_progress(index, total, run, "")

    batch.warning = calibration_warning(batch.runs)
    return batch
