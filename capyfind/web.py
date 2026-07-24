"""Local web app: launch searches and work the results in a browser.

Architecture is deliberately split so the front end can move to Azure Static
Web Apps later untouched:

- The front end (`PAGE` below) is a single static HTML/CSS/JS document that only
  ever talks to the API over fetch. Nothing is server-rendered.
- The API is a small JSON service. Locally it is this stdlib http.server; on
  Azure the same routes would become Functions. The front end would not change.

Sweeps take time and spend credits, so they run in a background thread and the
page polls for progress. The server is threaded so those polls are answered
while a sweep is still running.
"""

from __future__ import annotations

import json
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .discover import discover
from .generate import build_candidates
from .models import Run, run_from_dict
from .providers import build_providers
from .report import render_report, to_csv
from .store import LEAD_STATUSES, Store
from .verify import verify


class JobManager:
    """Runs discover/verify in background threads and tracks their progress."""

    def __init__(self, store: Store, providers) -> None:
        self.store = store
        self.providers = providers
        self._jobs: dict[str, dict] = {}
        self._lock = threading.Lock()

    def _new(self, kind: str, target: str) -> str:
        job_id = uuid.uuid4().hex[:12]
        with self._lock:
            self._jobs[job_id] = {
                "id": job_id,
                "kind": kind,
                "target": target,
                "status": "running",
                "done": 0,
                "total": 1,
                "last": "",
                "error": "",
                "survivors": 0,
            }
        return job_id

    def _update(self, job_id: str, **fields) -> None:
        with self._lock:
            if job_id in self._jobs:
                self._jobs[job_id].update(fields)

    def get(self, job_id: str) -> dict | None:
        with self._lock:
            job = self._jobs.get(job_id)
            return dict(job) if job else None

    def start_discover(
        self, seed: str, limit: int, country: str, buyer_size: str
    ) -> str:
        job_id = self._new("discover", seed)

        def progress(index: int, total: int, run: Run | None, note: str) -> None:
            self._update(
                job_id,
                done=index,
                total=total,
                last=(run.candidate if run else note),
            )

        def work() -> None:
            try:
                batch = discover(
                    seed,
                    self.providers,
                    limit=limit,
                    country=country,
                    buyer_size=buyer_size,
                    store=self.store,
                    # Re-score every time from the browser, so a re-run always
                    # reflects the current logic instead of showing stale stored
                    # verdicts. Search results are still cached, so this does not
                    # re-spend credits.
                    resume=False,
                    on_progress=progress,
                )
                self._update(
                    job_id,
                    status="done",
                    survivors=len(batch.survivors),
                    stopped=batch.stopped_early,
                    warning=batch.warning or "",
                )
            except Exception as exc:  # surfaced to the page, never swallowed
                self._update(job_id, status="error", error=f"{type(exc).__name__}: {exc}")

        threading.Thread(target=work, daemon=True).start()
        return job_id

    def start_verify(self, candidate: str, country: str, buyer_size: str) -> str:
        job_id = self._new("verify", candidate)

        def work() -> None:
            try:
                self._update(job_id, total=1, last=candidate)
                run = verify(
                    candidate,
                    self.providers,
                    depth="deep",
                    country=country,
                    buyer_size=buyer_size,
                    store=self.store,
                )
                self._update(
                    job_id,
                    status="done",
                    done=1,
                    survivors=1 if (run.verified and run.score.composite > 0) else 0,
                )
            except Exception as exc:
                self._update(job_id, status="error", error=f"{type(exc).__name__}: {exc}")

        threading.Thread(target=work, daemon=True).start()
        return job_id

    def start_list(
        self, phrases: list[str], country: str, buyer_size: str
    ) -> str:
        """Rate a user-supplied list of exact phrases -- the self-serve path."""
        job_id = self._new("list", f"{len(phrases)} phrases")

        def progress(index: int, total: int, run: Run | None, note: str) -> None:
            self._update(
                job_id, done=index, total=total,
                last=(run.candidate if run else note),
            )

        def work() -> None:
            try:
                batch = discover(
                    "my list",
                    self.providers,
                    country=country,
                    buyer_size=buyer_size,
                    store=self.store,
                    resume=False,
                    candidates=phrases,
                    on_progress=progress,
                )
                self._update(job_id, status="done", survivors=len(batch.survivors))
            except Exception as exc:
                self._update(job_id, status="error", error=f"{type(exc).__name__}: {exc}")

        threading.Thread(target=work, daemon=True).start()
        return job_id


def make_handler(store: Store, jobs: JobManager, providers):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args) -> None:
            pass

        # --- helpers -----------------------------------------------------

        def _send(self, body: bytes, ctype: str, status: int = 200) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, status: int = 200) -> None:
            self._send(
                json.dumps(obj).encode(), "application/json; charset=utf-8", status
            )

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length", 0))
            if not length:
                return {}
            try:
                return json.loads(self.rfile.read(length).decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                return {}

        # --- GET ---------------------------------------------------------

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)
            path = parsed.path

            if path == "/":
                self._send(PAGE.encode(), "text/html; charset=utf-8")

            elif path == "/api/runs":
                self._json(
                    store.list_runs(
                        seed=(query.get("seed") or [None])[0],
                        survivors_only=bool(query.get("survivors")),
                    )
                )

            elif path == "/api/seeds":
                with store._connect() as conn:
                    rows = conn.execute(
                        "SELECT DISTINCT seed FROM runs WHERE seed IS NOT NULL "
                        "ORDER BY seed"
                    ).fetchall()
                self._json([r["seed"] for r in rows])

            elif path == "/api/providers":
                self._json(
                    {
                        "lines": providers.describe(),
                        "any_live": providers.any_live,
                        "fixture": providers.fixture_mode,
                    }
                )

            elif path == "/api/report":
                candidate = (query.get("candidate") or [""])[0]
                depth = (query.get("depth") or ["shallow"])[0]
                data = store.get_run(candidate, depth)
                if data is None:
                    self._send(b"not found", "text/plain; charset=utf-8", 404)
                    return
                text = render_report(run_from_dict(data), show_anchors=False)
                self._send(text.encode(), "text/plain; charset=utf-8")

            elif path == "/api/job":
                job = jobs.get((query.get("id") or [""])[0])
                if job is None:
                    self._json({"error": "unknown job"}, 404)
                else:
                    self._json(job)

            elif path == "/export.csv":
                runs = [
                    run_from_dict(d)
                    for d in store.list_runs(seed=(query.get("seed") or [None])[0])
                ]
                self.send_response(200)
                self.send_header("Content-Type", "text/csv; charset=utf-8")
                self.send_header(
                    "Content-Disposition", 'attachment; filename="capyfind.csv"'
                )
                payload = to_csv(runs).encode()
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            else:
                self._send(b"not found", "text/plain; charset=utf-8", 404)

        # --- POST --------------------------------------------------------

        def do_POST(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            body = self._body()

            if path == "/api/run":
                mode = body.get("mode", "discover")
                country = (body.get("country") or "UK").strip()
                buyer = (body.get("buyer_size") or "sole trader").strip()
                if mode == "verify":
                    candidate = (body.get("candidate") or "").strip()
                    if not candidate:
                        self._json({"error": "candidate is required"}, 400)
                        return
                    self._json({"job_id": jobs.start_verify(candidate, country, buyer)})
                elif mode == "list":
                    raw = body.get("phrases") or ""
                    phrases = [
                        p.strip()
                        for p in raw.replace(",", "\n").splitlines()
                        if p.strip()
                    ]
                    # de-dup, preserve order, cap generously
                    seen: set[str] = set()
                    phrases = [
                        p for p in phrases
                        if not (p.lower() in seen or seen.add(p.lower()))
                    ][:200]
                    if not phrases:
                        self._json({"error": "add at least one phrase"}, 400)
                        return
                    self._json({"job_id": jobs.start_list(phrases, country, buyer)})
                else:
                    seed = (body.get("seed") or "").strip()
                    if not seed:
                        self._json({"error": "seed is required"}, 400)
                        return
                    try:
                        limit = max(1, min(200, int(body.get("limit", 20))))
                    except (TypeError, ValueError):
                        limit = 20
                    self._json(
                        {"job_id": jobs.start_discover(seed, limit, country, buyer)}
                    )

            elif path == "/api/preview":
                seed = (body.get("seed") or "").strip()
                if not seed:
                    self._json({"error": "seed is required"}, 400)
                    return
                try:
                    limit = max(1, min(200, int(body.get("limit", 20))))
                except (TypeError, ValueError):
                    limit = 20
                self._json(
                    {"candidates": build_candidates(seed, providers=providers, limit=limit)}
                )

            elif path == "/api/lead":
                candidate = (body.get("candidate") or "").strip()
                if not candidate:
                    self._json({"error": "candidate is required"}, 400)
                    return
                status = body.get("status")
                if status is not None and status not in LEAD_STATUSES:
                    self._json({"error": f"bad status {status!r}"}, 400)
                    return
                store.set_mark(candidate, status=status, notes=body.get("notes"))
                self._json({"ok": True, **store.get_mark(candidate)})

            else:
                self._json({"error": "not found"}, 404)

    return Handler


def serve(db: str = "capyfind.db", port: int = 8000, host: str = "127.0.0.1") -> None:
    store = Store(db)
    providers = build_providers(store)
    jobs = JobManager(store, providers)
    server = ThreadingHTTPServer((host, port), make_handler(store, jobs, providers))

    banner = "LIVE" if providers.any_live else "no live web provider -- runs will be UNVERIFIED"
    print(f"capyfind UI  ->  http://{host}:{port}   ({banner})")
    print("ctrl-c to stop")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        server.server_close()


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Capybara Finder</title>
<style>
  :root{
    --paper:#eef1ec; --card:#ffffff; --ink:#22301f; --muted:#5c6b57; --faint:#8b9883;
    --line:#d5dcd1; --accent:#3e6b4f; --accent-soft:#e3ede5; --accent-ink:#254433;
    --pelt:#8a6440; --pelt-soft:#f0e7dc;
    --good:#3e6b4f; --good-soft:#e3ede5;
    --maybe:#b07a2e; --maybe-soft:#f5ecda;
    --taken:#7c837a; --taken-soft:#eef1ee;
    --unknown:#6a5bb0; --unknown-soft:#ece8f8;
    --sans:"Inter","Segoe UI",system-ui,-apple-system,sans-serif;
    --serif:"Fraunces",Georgia,serif;
    --mono:ui-monospace,"Cascadia Mono",Consolas,menlo,monospace;
  }
  @media (prefers-color-scheme:dark){:root{
    --paper:#131a14; --card:#1b241b; --ink:#e7ece7; --muted:#9aa895; --faint:#69766a;
    --line:#2a352a; --accent:#6fb489; --accent-soft:#183025; --accent-ink:#a9e0c0;
    --pelt:#c79a6e; --pelt-soft:#2e2418;
    --good:#6fb489; --good-soft:#183025; --maybe:#d6a24e; --maybe-soft:#332813;
    --taken:#8b988f; --taken-soft:#232922; --unknown:#b3a5e6; --unknown-soft:#241d3d;
  }}
  :root[data-theme="dark"]{
    --paper:#131a14; --card:#1b241b; --ink:#e7ece7; --muted:#9aa895; --faint:#69766a;
    --line:#2a352a; --accent:#6fb489; --accent-soft:#183025; --accent-ink:#a9e0c0;
    --pelt:#c79a6e; --pelt-soft:#2e2418; --good:#6fb489; --good-soft:#183025;
    --maybe:#d6a24e; --maybe-soft:#332813; --taken:#8b988f; --taken-soft:#232922;
    --unknown:#b3a5e6; --unknown-soft:#241d3d;
  }
  :root[data-theme="light"]{
    --paper:#eef1ec; --card:#ffffff; --ink:#22301f; --muted:#5c6b57; --faint:#8b9883;
    --line:#d5dcd1; --accent:#3e6b4f; --accent-soft:#e3ede5; --accent-ink:#254433;
    --pelt:#8a6440; --pelt-soft:#f0e7dc; --good:#3e6b4f; --good-soft:#e3ede5;
    --maybe:#b07a2e; --maybe-soft:#f5ecda; --taken:#7c837a; --taken-soft:#eef1ee;
    --unknown:#6a5bb0; --unknown-soft:#ece8f8;
  }
  *{box-sizing:border-box;}
  body{margin:0;background:var(--paper);color:var(--ink);font-family:var(--sans);
       font-size:15.5px;line-height:1.55;-webkit-font-smoothing:antialiased;}
  .wrap{max-width:800px;margin:0 auto;padding:26px 16px 90px;}
  a{color:var(--accent);}

  header .eyebrow{font-size:12px;letter-spacing:.12em;text-transform:uppercase;
                  color:var(--pelt);font-weight:600;}
  header h1{font-family:var(--serif);font-size:34px;margin:5px 0 6px;line-height:1.05;}
  header p{margin:0;color:var(--muted);max-width:60ch;font-size:15px;}
  .status{margin-top:10px;font-size:13px;color:var(--faint);}
  .status b{color:var(--good);}
  .status.warn b{color:var(--maybe);}

  .card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:18px;}
  .search{margin-top:22px;}
  .tabs{display:flex;gap:6px;margin-bottom:16px;flex-wrap:wrap;}
  .tab{font:inherit;font-size:14px;font-weight:600;padding:8px 14px;border-radius:9px;
       border:1px solid var(--line);background:var(--paper);color:var(--muted);cursor:pointer;}
  .tab.on{background:var(--accent);color:#fff;border-color:var(--accent);}
  :root[data-theme="dark"] .tab.on{color:#06231d;}
  .tabhint{color:var(--muted);font-size:13.5px;margin:0 0 14px;}

  .searchrow{display:flex;gap:10px;flex-wrap:wrap;}
  #topic,#one{flex:1;min-width:220px;font:inherit;font-size:16px;padding:12px 14px;
       border:1.5px solid var(--line);border-radius:10px;background:var(--paper);color:var(--ink);}
  #list{width:100%;font:inherit;font-size:15px;padding:12px 14px;border:1.5px solid var(--line);
        border-radius:10px;background:var(--paper);color:var(--ink);min-height:120px;resize:vertical;}
  #topic:focus,#one:focus,#list:focus{outline:none;border-color:var(--accent);}
  .go{font:inherit;font-size:16px;font-weight:600;border:0;border-radius:10px;padding:12px 22px;
      background:var(--accent);color:#fff;cursor:pointer;white-space:nowrap;}
  :root[data-theme="dark"] .go{color:#06231d;}
  .go:hover{filter:brightness(1.07);}
  .go:disabled{opacity:.55;cursor:not-allowed;}
  .listrow{display:flex;justify-content:flex-end;margin-top:10px;}
  .chips{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px;align-items:center;}
  .chips .lbl{font-size:13px;color:var(--faint);}
  .chip{font:inherit;font-size:13px;padding:5px 12px;border:1px solid var(--line);border-radius:20px;
        background:var(--paper);color:var(--muted);cursor:pointer;}
  .chip:hover{border-color:var(--accent);color:var(--accent);}

  .opts{display:flex;gap:14px;flex-wrap:wrap;margin-top:14px;padding-top:14px;
        border-top:1px solid var(--line);}
  .opts .f{display:flex;flex-direction:column;gap:5px;}
  .opts label{font-size:11px;color:var(--faint);text-transform:uppercase;letter-spacing:.05em;}
  .opts input{font:inherit;font-size:14px;padding:8px 10px;border:1px solid var(--line);
       border-radius:8px;background:var(--paper);color:var(--ink);}

  .prog{display:none;margin-top:16px;padding:13px 15px;background:var(--accent-soft);border-radius:10px;}
  .prog.on{display:block;}
  .prog .msg{font-size:14px;color:var(--accent-ink);margin-bottom:8px;}
  .track{height:7px;border-radius:4px;background:rgba(0,0,0,.09);overflow:hidden;}
  .track>i{display:block;height:100%;background:var(--accent);width:0;transition:width .3s;}
  .done-msg{display:none;margin-top:14px;padding:12px 15px;border-radius:10px;font-size:14px;
            background:var(--accent-soft);color:var(--accent-ink);}
  .done-msg.err{background:var(--maybe-soft);color:var(--maybe);}
  .done-msg.on{display:block;}

  /* how-it-works */
  details.how{margin-top:16px;border:1px solid var(--line);border-radius:12px;background:var(--card);}
  details.how>summary{cursor:pointer;padding:13px 16px;font-weight:600;font-size:14.5px;list-style:none;
                      display:flex;align-items:center;gap:8px;}
  details.how>summary::-webkit-details-marker{display:none;}
  details.how>summary::before{content:"?";display:inline-grid;place-items:center;width:20px;height:20px;
       border-radius:50%;background:var(--accent-soft);color:var(--accent);font-size:12px;font-weight:700;}
  .howbody{padding:0 16px 16px;color:var(--muted);font-size:14px;}
  .howbody h4{margin:14px 0 6px;color:var(--ink);font-size:13.5px;}
  .dim{display:flex;gap:10px;padding:5px 0;align-items:baseline;}
  .dim b{color:var(--ink);min-width:150px;display:inline-block;}
  .vk{display:inline-block;font-size:11.5px;font-weight:700;padding:2px 9px;border-radius:20px;margin-right:6px;}

  /* results */
  .resulthead{display:flex;align-items:center;gap:12px;margin:28px 0 12px;flex-wrap:wrap;}
  .resulthead h2{font-family:var(--serif);font-size:22px;margin:0;}
  .rightlink{margin-left:auto;display:flex;gap:12px;align-items:center;}
  .rightlink a{font-size:13px;}.rightlink select{font:inherit;font-size:13px;padding:5px 8px;
       border:1px solid var(--line);border-radius:7px;background:var(--card);color:var(--ink);}
  .filters{display:flex;gap:7px;flex-wrap:wrap;margin-bottom:16px;}
  .filt{font:inherit;font-size:13px;padding:6px 13px;border:1px solid var(--line);border-radius:20px;
        background:var(--card);color:var(--muted);cursor:pointer;}
  .filt.on{background:var(--ink);color:var(--paper);border-color:var(--ink);}

  .item{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:15px 17px;
        margin-bottom:11px;border-left:4px solid var(--edge,var(--line));}
  .item.good{--edge:var(--good);} .item.maybe{--edge:var(--maybe);}
  .item.taken{--edge:var(--taken);} .item.unknown{--edge:var(--unknown);}
  .item .toprow{display:flex;align-items:flex-start;gap:12px;}
  .item .name{font-size:16px;font-weight:600;flex:1;line-height:1.3;}
  .pill{font-size:12px;font-weight:700;padding:4px 11px;border-radius:20px;white-space:nowrap;}
  .pill.good{background:var(--good-soft);color:var(--good);} .pill.maybe{background:var(--maybe-soft);color:var(--maybe);}
  .pill.taken{background:var(--taken-soft);color:var(--taken);} .pill.unknown{background:var(--unknown-soft);color:var(--unknown);}
  .item .blurb{margin:9px 0 0;color:var(--muted);font-size:14.5px;}
  .item .actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:13px;align-items:center;}
  .leadbtns{display:flex;gap:4px;flex-wrap:wrap;}
  .leadbtn{font:inherit;font-size:12.5px;padding:5px 11px;border:1px solid var(--line);border-radius:16px;
           background:var(--paper);color:var(--muted);cursor:pointer;}
  .leadbtn.on{background:var(--accent-soft);border-color:var(--accent);color:var(--accent-ink);font-weight:600;}
  .seemore{margin-left:auto;font:inherit;font-size:13px;color:var(--accent);background:none;border:0;cursor:pointer;padding:0;}
  .detail{display:none;margin-top:13px;padding-top:13px;border-top:1px solid var(--line);}
  .detail.on{display:block;}
  .scores{display:grid;grid-template-columns:1fr 1fr;gap:9px 18px;margin-bottom:14px;}
  .score{font-size:13px;} .score .lab{display:flex;justify-content:space-between;color:var(--muted);margin-bottom:3px;}
  .score .meter{height:6px;background:var(--line);border-radius:4px;overflow:hidden;}
  .score .meter>i{display:block;height:100%;background:var(--accent);}
  .detail pre{white-space:pre-wrap;font-family:var(--mono);font-size:12px;line-height:1.5;background:var(--paper);
       border:1px solid var(--line);border-radius:8px;padding:12px;overflow-x:auto;max-height:320px;overflow-y:auto;margin:0 0 12px;}
  .detail pre a{color:var(--accent);}
  .notes{width:100%;font:inherit;font-size:14px;padding:10px;border:1px solid var(--line);border-radius:8px;
         background:var(--paper);color:var(--ink);resize:vertical;min-height:58px;}
  .saved{font-size:12px;color:var(--good);display:none;margin-left:8px;}
  .empty{text-align:center;color:var(--muted);padding:38px 16px;font-size:14.5px;}
  .empty b{color:var(--ink);}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div class="eyebrow">demand high · supply low · evidence required</div>
    <h1>Capybara Finder</h1>
    <p>Hunt niches lots of people search for but nobody serves well. You drive it —
      explore a topic, check one idea, or paste your own list. Every result is
      backed by real searches, or it's marked "couldn't check" rather than guessed.</p>
    <div class="status" id="status">checking…</div>
  </header>

  <div class="card search">
    <div class="tabs">
      <button class="tab on" data-mode="discover">Explore a topic</button>
      <button class="tab" data-mode="verify">Check one idea</button>
      <button class="tab" data-mode="list">Paste my own list</button>
    </div>

    <p class="tabhint" id="tabhint"></p>

    <div id="pane-discover">
      <div class="searchrow">
        <input id="topic" placeholder="e.g. small landlords" autocomplete="off">
        <button class="go" data-run>Find gaps</button>
      </div>
      <div class="chips">
        <span class="lbl">Try:</span>
        <button class="chip">UK trades</button><button class="chip">small landlords</button>
        <button class="chip">childminders</button><button class="chip">dog groomers</button>
        <button class="chip">florists</button><button class="chip">wedding venues</button>
      </div>
    </div>

    <div id="pane-verify" style="display:none;">
      <div class="searchrow">
        <input id="one" placeholder="e.g. eicr expiry reminder app" autocomplete="off">
        <button class="go" data-run>Check it</button>
      </div>
    </div>

    <div id="pane-list" style="display:none;">
      <textarea id="list" placeholder="One idea per line, e.g.
scaffold inspection reminder app
convert supplier invoice to xero
allergen matrix generator for cafes"></textarea>
      <div class="listrow"><button class="go" data-run>Rate my list</button></div>
    </div>

    <div class="opts">
      <div class="f" id="limitf">
        <label>How many to test</label>
        <input id="limit" type="number" value="30" min="1" max="200" style="width:90px;">
      </div>
      <div class="f"><label>Country</label><input id="country" value="UK" style="width:80px;"></div>
    </div>

    <div class="prog" id="prog">
      <div class="msg" id="progmsg">Starting…</div>
      <div class="track"><i id="trackfill"></i></div>
    </div>
    <div class="done-msg" id="donemsg"></div>
  </div>

  <details class="how">
    <summary>How the scoring works &amp; what the colours mean</summary>
    <div class="howbody">
      <p>Each idea gets five scores. They're <b>multiplied</b> — so a zero in any one
        of them makes the whole thing zero. That's on purpose: a niche nobody pays
        for, or that you can't reach, isn't a business, however much people search it.</p>
      <h4>The five things it measures</h4>
      <div class="dim"><b>People searching (0–4)</b><span>How many people actually type or ask for this — autocomplete, forum threads, whether it repeats over years.</span></div>
      <div class="dim"><b>Gap in competition (0–4)</b><span>How badly served it is. 4 = nobody good does it. 0 = a polished, affordable tool already owns it.</span></div>
      <div class="dim"><b>Signs people pay (0–3)</b><span>Evidence money moves here — paid rivals, people moaning about a price, "cheaper alternative to X".</span></div>
      <div class="dim"><b>Easy to reach them (0–3)</b><span>Is there an obvious place these people gather (a subreddit, a trade forum)? No route to them kills a good idea.</span></div>
      <div class="dim"><b>Nothing blocking (0 or 1)</b><span>0 if a licence or regulation means you legally can't build it (common in trade certificates).</span></div>
      <h4>What the verdicts mean</h4>
      <div style="padding:4px 0"><span class="vk" style="background:var(--good-soft);color:var(--good)">Possible gap</span>Rare. Real demand, weak supply. Worth testing with real people.</div>
      <div style="padding:4px 0"><span class="vk" style="background:var(--maybe-soft);color:var(--maybe)">Worth a look</span>Something's there but not a clear win. Dig a little.</div>
      <div style="padding:4px 0"><span class="vk" style="background:var(--taken-soft);color:var(--taken)">Already taken</span>Someone does this well, or it's a commodity category. Skip it. (Most results land here — that's the tool doing its job.)</div>
      <div style="padding:4px 0"><span class="vk" style="background:var(--unknown-soft);color:var(--unknown)">Couldn't check</span>Not enough came back to judge — usually no search key, or a dead phrase.</div>
    </div>
  </details>

  <div class="resulthead">
    <h2>Results</h2>
    <div class="rightlink">
      <select id="seedfilter"><option value="">all searches</option></select>
      <a id="csv" href="/export.csv">Download CSV</a>
    </div>
  </div>
  <div class="filters">
    <button class="filt on" data-f="all">All</button>
    <button class="filt" data-f="good">Possible gaps</button>
    <button class="filt" data-f="maybe">Worth a look</button>
    <button class="filt" data-f="taken">Already taken</button>
    <button class="filt" data-f="unknown">Couldn't check</button>
  </div>
  <div id="items"></div>
  <div class="empty" id="empty"><b>No results yet.</b><br>Pick a mode above and run it.</div>
</div>

<script>
const $=(id)=>document.getElementById(id);
let DATA=[], FILT="all", MODE="discover", poll=null;

function esc(s){return String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));}
function linkify(s){return s.replace(/(https?:\/\/[^\s<]+)/g,'<a href="$1" target="_blank" rel="noopener noreferrer">$1</a>');}

function verdict(run){
  const kr=(run.kill_reason||"").toLowerCase();
  if(run.band==="pursue") return {k:"good",label:"Possible gap",blurb:"Looks like a real gap — real demand, weak supply. Worth testing with real people before building."};
  if(run.band==="investigate") return {k:"maybe",label:"Worth a look",blurb:"Nobody's clearly winning here. Could be worth a closer look."};
  if(run.band==="unverified"){
    let b="Couldn't check this one.";
    if(kr.includes("never assessed")||kr.includes("no web")) b="Couldn't check — no web search key is set, so nobody looked at the competition.";
    else if(kr.includes("none productive")||kr.includes("no ")) b="Couldn't check — no useful results came back for this phrase.";
    return {k:"unknown",label:"Couldn't check",blurb:b};
  }
  let b="The market already looks well served.";
  if(kr.includes("commodity")){const w=run.kill_reason.split("dominated by")[1]; b="A crowded commodity category — big players already own it"+(w?" ("+w.trim()+")":".");}
  else if(kr.includes("polished incumbent")){const who=(run.kill_reason.split(":")[1]||"").trim(); b="A strong competitor already does this"+(who?": "+who:".");}
  else if(kr.includes("hard blocker")) b="Blocked by a rule or licence you'd need to build it.";
  else if(kr.includes("pays")) b="No sign that anyone actually pays in this area.";
  else if(kr.includes("reach")) b="No obvious place to find and reach these customers.";
  else if(kr.includes("adequate")||kr.includes("supply already")) b="A live competitor already covers this.";
  return {k:"taken",label:"Already taken",blurb:b};
}

async function loadStatus(){
  try{const p=await(await fetch("/api/providers")).json();const el=$("status");
    if(p.fixture){el.innerHTML="Demo mode — sample data, not live results";el.classList.add("warn");}
    else if(!p.any_live){el.innerHTML="No search key set — results will say “couldn't check”.";el.classList.add("warn");}
    else el.innerHTML="Search is <b>live</b> — ready to go.";
  }catch(e){}
}
async function loadSeeds(){const s=await(await fetch("/api/seeds")).json();const sel=$("seedfilter"),cur=sel.value;
  sel.innerHTML='<option value="">all searches</option>';
  s.forEach(x=>{const o=document.createElement("option");o.value=o.textContent=x;sel.appendChild(o);});sel.value=cur;}
async function load(){const p=new URLSearchParams();if($("seedfilter").value)p.set("seed",$("seedfilter").value);
  DATA=await(await fetch("/api/runs?"+p)).json();render();
  $("csv").href="/export.csv"+($("seedfilter").value?("?seed="+encodeURIComponent($("seedfilter").value)):"");}

function render(){const box=$("items");box.innerHTML="";
  const rows=DATA.filter(r=>FILT==="all"||verdict(r).k===FILT);
  $("empty").style.display=DATA.length?"none":"block";
  if(DATA.length&&!rows.length){box.innerHTML='<div class="empty">Nothing in this group.</div>';return;}
  rows.forEach(run=>box.appendChild(item(run)));}

function item(run){const v=verdict(run);const el=document.createElement("div");el.className="item "+v.k;
  el.innerHTML='<div class="toprow"><div class="name">'+esc(run.candidate)+'</div><span class="pill '+v.k+'">'+v.label+'</span></div>'+
    '<p class="blurb">'+esc(v.blurb)+'</p><div class="actions"><div class="leadbtns"></div>'+
    '<button class="seemore">See details ▾</button></div><div class="detail"></div>';
  el.querySelector(".leadbtns").appendChild(leadButtons(run));
  const more=el.querySelector(".seemore"),det=el.querySelector(".detail");
  more.onclick=()=>{if(det.classList.contains("on")){det.classList.remove("on");more.textContent="See details ▾";return;}
    more.textContent="Hide details ▴";if(!det.dataset.loaded){fillDetail(run,det);det.dataset.loaded="1";}det.classList.add("on");};
  return el;}

const STATUSES=[["new","New"],["shortlist","Shortlist"],["chasing","Chasing"],["rejected","Rejected"]];
function leadButtons(run){const wrap=document.createElement("div");wrap.className="leadbtns";
  STATUSES.forEach(([val,lab])=>{const b=document.createElement("button");
    b.className="leadbtn"+(((run.lead_status||"new")===val)?" on":"");b.textContent=lab;
    b.onclick=async()=>{await fetch("/api/lead",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({candidate:run.candidate,status:val})});run.lead_status=val;
      wrap.querySelectorAll(".leadbtn").forEach(x=>x.classList.remove("on"));b.classList.add("on");};
    wrap.appendChild(b);});return wrap;}

const SCORE_LABELS=[["demand","People searching",4],["supply_weak","Gap in competition",4],
  ["pay","Signs people pay",3],["reach","Easy to reach them",3],["buildable","Nothing blocking",1]];
async function fillDetail(run,det){let html="";
  if(!run.score.unverified){html+='<div class="scores">';
    SCORE_LABELS.forEach(([k,lab,max])=>{const v=run.score[k],pct=Math.round(100*v/max);
      html+='<div class="score"><div class="lab"><span>'+lab+'</span><span>'+v+' / '+max+'</span></div>'+
            '<div class="meter"><i style="width:'+pct+'%"></i></div></div>';});html+='</div>';}
  det.innerHTML=html+'<pre>loading the full write-up…</pre>'+
    '<div style="font-size:11px;color:var(--faint);text-transform:uppercase;letter-spacing:.05em;margin-bottom:4px;">Your notes<span class="saved">saved ✓</span></div>'+
    '<textarea class="notes" placeholder="Jot anything here — saved automatically"></textarea>';
  const pre=det.querySelector("pre");
  const text=await(await fetch("/api/report?candidate="+encodeURIComponent(run.candidate)+"&depth="+encodeURIComponent(run.depth))).text();
  pre.innerHTML=linkify(esc(text));
  const ta=det.querySelector(".notes");ta.value=run.lead_notes||"";
  ta.onblur=async()=>{await fetch("/api/lead",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({candidate:run.candidate,notes:ta.value})});run.lead_notes=ta.value;
    const s=det.querySelector(".saved");s.style.display="inline";setTimeout(()=>s.style.display="none",1400);};}

function setMode(m){MODE=m;
  document.querySelectorAll(".tab").forEach(t=>t.classList.toggle("on",t.dataset.mode===m));
  $("pane-discover").style.display=m==="discover"?"block":"none";
  $("pane-verify").style.display=m==="verify"?"block":"none";
  $("pane-list").style.display=m==="list"?"block":"none";
  $("limitf").style.display=m==="discover"?"flex":"none";
  const hints={discover:"Type a topic. It pulls the real phrases people search for it and tests each one.",
    verify:"Check one exact idea in depth. Type the precise phrase.",
    list:"Paste your own ideas, one per line (or comma-separated). It rates every one."};
  $("tabhint").textContent=hints[m];}

async function run(){
  let payload,ok=true;
  const country=$("country").value;
  if(MODE==="verify"){const c=$("one").value.trim();if(!c){$("one").focus();return;}payload={mode:"verify",candidate:c,country};}
  else if(MODE==="list"){const raw=$("list").value.trim();if(!raw){$("list").focus();return;}payload={mode:"list",phrases:raw,country};}
  else{const s=$("topic").value.trim();if(!s){$("topic").focus();return;}payload={mode:"discover",seed:s,limit:parseInt($("limit").value)||30,country};}
  document.querySelectorAll("[data-run]").forEach(b=>b.disabled=true);$("donemsg").classList.remove("on");
  const res=await(await fetch("/api/run",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(payload)})).json();
  if(res.error){done(res.error,true);document.querySelectorAll("[data-run]").forEach(b=>b.disabled=false);return;}
  $("prog").classList.add("on");watch(res.job_id);}

function watch(id){if(poll)clearInterval(poll);
  poll=setInterval(async()=>{const j=await(await fetch("/api/job?id="+id)).json();
    if(j.error){clearInterval(poll);return;}
    const pct=j.total?Math.round(100*j.done/j.total):0;$("trackfill").style.width=pct+"%";
    $("progmsg").textContent=j.status==="running"?("Testing… "+j.done+" of "+j.total+(j.last?"  ·  “"+j.last+"”":"")):"Finishing…";
    if(j.status==="done"||j.status==="error"){clearInterval(poll);
      document.querySelectorAll("[data-run]").forEach(b=>b.disabled=false);
      setTimeout(()=>$("prog").classList.remove("on"),800);
      if(j.status==="error")done("Something went wrong: "+j.error,true);
      else{let m=j.kind==="verify"?"Done — see the result below.":("Done — tested "+j.done+", "+j.survivors+" worth a look. (Most being “taken” is normal and healthy.)");done(m,false);}
      await loadSeeds();await load();}
  },1000);}
function done(msg,err){const d=$("donemsg");d.textContent=msg;d.className="done-msg on"+(err?" err":"");}

document.querySelectorAll(".tab").forEach(t=>t.onclick=()=>setMode(t.dataset.mode));
document.querySelectorAll(".chip").forEach(c=>c.onclick=()=>{$("topic").value=c.textContent;$("topic").focus();});
document.querySelectorAll("[data-run]").forEach(b=>b.onclick=run);
$("topic").addEventListener("keydown",e=>{if(e.key==="Enter")run();});
$("one").addEventListener("keydown",e=>{if(e.key==="Enter")run();});
$("seedfilter").onchange=load;
document.querySelectorAll(".filt").forEach(b=>b.onclick=()=>{
  document.querySelectorAll(".filt").forEach(x=>x.classList.remove("on"));b.classList.add("on");FILT=b.dataset.f;render();});
setMode("discover");loadStatus();loadSeeds();load();
</script>
</body>
</html>
"""
