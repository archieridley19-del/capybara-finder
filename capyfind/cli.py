"""Command line interface.

    capyfind providers                       show which data sources are live
    capyfind generate "UK trades"            preview candidate phrases
    capyfind verify "eicr expiry reminder"   deep-dive one niche
    capyfind discover "UK trades"            wide sweep, ranked shortlist
    capyfind list --survivors                re-read stored results
    capyfind export results.csv              CSV of everything stored
    capyfind anchors                         calibration self-test
    capyfind web                             local UI on :8000
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .discover import discover
from .generate import generate_candidates, match_vertical
from .models import Run, run_from_dict
from .providers import build_providers
from .report import render_report, render_table, to_csv
from .scoring import ANCHORS
from .store import DEFAULT_DB, Store
from .verify import verify


def _providers_banner(providers, stream=sys.stderr) -> None:
    print("data sources:", file=stream)
    for line in providers.describe():
        print(line, file=stream)
    if providers.fixture_mode:
        print(
            "  NOTE: fixture mode -- results are recorded samples, not live "
            "retrieval. Not valid as findings.",
            file=stream,
        )
    elif not providers.any_live:
        print(
            "  WARNING: no live web provider. Runs will be UNVERIFIED until a "
            "search API key is set (SERPER_API_KEY / BRAVE_API_KEY / SERPAPI_API_KEY).",
            file=stream,
        )
    print("", file=stream)


def cmd_providers(args) -> int:
    store = Store(args.db)
    providers = build_providers(store, use_fixtures=args.fixtures)
    _providers_banner(providers, stream=sys.stdout)
    return 0


def cmd_generate(args) -> int:
    name, _ = match_vertical(args.seed)
    phrases = generate_candidates(
        args.seed,
        limit=args.limit,
        country=args.country,
        tasks_file=Path(args.tasks_file) if args.tasks_file else None,
    )
    print(f"lexicon: {name}   candidates: {len(phrases)}\n")
    for i, phrase in enumerate(phrases, 1):
        print(f"{i:>4}. {phrase}")
    return 0


def cmd_verify(args) -> int:
    store = Store(args.db)
    providers = build_providers(store, use_fixtures=args.fixtures)
    _providers_banner(providers)

    run = verify(
        args.candidate,
        providers,
        depth=args.depth,
        country=args.country,
        buyer_size=args.buyer_size,
        store=store,
    )
    if args.json:
        print(run.to_json())
    else:
        print(render_report(run, show_anchors=not args.no_anchors))
    return 0 if run.verified else 2


def cmd_discover(args) -> int:
    store = Store(args.db)
    providers = build_providers(store, use_fixtures=args.fixtures)
    _providers_banner(providers)

    def progress(index: int, total: int, run: Run | None, note: str) -> None:
        if run is None:
            print(f"[{index}/{total}] {note}", file=sys.stderr)
            return
        print(
            f"[{index}/{total}] {run.candidate[:44]:<44} "
            f"{run.score.display():>10}  {run.band}",
            file=sys.stderr,
        )

    batch = discover(
        args.seed,
        providers,
        limit=args.limit,
        country=args.country,
        buyer_size=args.buyer_size,
        store=store,
        resume=not args.no_resume,
        tasks_file=Path(args.tasks_file) if args.tasks_file else None,
        failure_threshold=args.failure_threshold,
        on_progress=progress if not args.quiet else None,
    )

    print("")
    print(render_table(batch.ranked(), survivors_only=args.survivors))
    print("")
    print(
        f"{len(batch.runs)} tested, {batch.skipped} skipped (cached), "
        f"{len(batch.survivors)} survived"
    )
    if batch.stopped_early:
        print(f"\nSTOPPED EARLY: {batch.stopped_early}")
    if batch.warning:
        print(f"\n{batch.warning}")
    return 0


def cmd_list(args) -> int:
    store = Store(args.db)
    runs = [run_from_dict(d) for d in store.list_runs(seed=args.seed, survivors_only=args.survivors)]
    print(render_table(runs, survivors_only=args.survivors))
    print(f"\n{len(runs)} stored results")
    return 0


def cmd_export(args) -> int:
    store = Store(args.db)
    runs = [run_from_dict(d) for d in store.list_runs(seed=args.seed)]
    Path(args.path).write_text(to_csv(runs), encoding="utf-8")
    print(f"wrote {len(runs)} rows to {args.path}")
    return 0


def cmd_anchors(args) -> int:
    """Self-test: the anchors must reject. If they do not, the tool is broken."""
    store = Store(args.db)
    providers = build_providers(store, use_fixtures=True)
    print("Running calibration anchors against recorded fixtures.\n")

    failures = 0
    for phrase, expected in ANCHORS.items():
        run = verify(phrase, providers, depth="deep", store=None)
        ok = run.band == expected["expected_band"]
        status = "PASS" if ok else "FAIL"
        if not ok:
            failures += 1
        print(f"[{status}] {phrase}")
        print(f"        expected {expected['expected_band']}, got {run.band} "
              f"(composite {run.score.display()})")
        print(f"        {run.kill_reason}")
        print("")

    if failures:
        print(f"{failures} anchor(s) failed. The verifier is not working yet.")
        return 1
    print("All anchors rejected as expected.")
    return 0


def cmd_web(args) -> int:
    from .web import serve

    serve(db=args.db, port=args.port)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="capyfind",
        description="Find micro-niches with high demand and weak supply. "
        "Optimised for trustworthy negatives.",
    )
    parser.add_argument("--db", default=str(DEFAULT_DB), help="sqlite path")
    parser.add_argument(
        "--fixtures", action="store_true",
        help="use recorded fixtures instead of live APIs (offline, not findings)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("providers", help="show which data sources are live")
    p.set_defaults(func=cmd_providers)

    p = sub.add_parser("generate", help="preview candidate phrases")
    p.add_argument("seed")
    p.add_argument("--limit", type=int, default=120)
    p.add_argument("--country", default="UK")
    p.add_argument("--tasks-file")
    p.set_defaults(func=cmd_generate)

    p = sub.add_parser("verify", help="deep-dive a single niche")
    p.add_argument("candidate")
    p.add_argument("--depth", choices=("shallow", "deep"), default="deep")
    p.add_argument("--country", default="UK")
    p.add_argument("--buyer-size", default="sole trader")
    p.add_argument("--json", action="store_true")
    p.add_argument("--no-anchors", action="store_true")
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("discover", help="wide sweep across generated candidates")
    p.add_argument("seed")
    p.add_argument("--limit", type=int, default=40)
    p.add_argument("--country", default="UK")
    p.add_argument("--buyer-size", default="sole trader")
    p.add_argument("--survivors", action="store_true", help="show survivors only")
    p.add_argument("--no-resume", action="store_true")
    p.add_argument("--tasks-file")
    p.add_argument("--failure-threshold", type=int, default=3)
    p.add_argument("--quiet", action="store_true")
    p.set_defaults(func=cmd_discover)

    p = sub.add_parser("list", help="re-read stored results")
    p.add_argument("--seed")
    p.add_argument("--survivors", action="store_true")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("export", help="write stored results to CSV")
    p.add_argument("path")
    p.add_argument("--seed")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("anchors", help="calibration self-test (must reject)")
    p.set_defaults(func=cmd_anchors)

    p = sub.add_parser("web", help="local web UI")
    p.add_argument("--port", type=int, default=8000)
    p.set_defaults(func=cmd_web)

    return parser


def main(argv: list[str] | None = None) -> int:
    # Reports carry £ and € signs; the Windows console defaults to cp1252 and
    # mangles them into replacement characters.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass

    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\ninterrupted; stored results are preserved", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
