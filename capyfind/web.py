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
from .generate import generate_candidates
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
                    {"candidates": generate_candidates(seed, limit=limit)}
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
    --paper:#f5f7f6; --card:#ffffff; --ink:#17241f; --muted:#5c6b66; --faint:#8b9893;
    --line:#e2e8e5; --accent:#0f6b5c; --accent-soft:#e4f0ec; --accent-ink:#0b4c41;
    --good:#1a7a4b; --good-soft:#e2f3e9;
    --maybe:#9a6a00; --maybe-soft:#fbefd6;
    --taken:#8a8f8c; --taken-soft:#eef1f0;
    --unknown:#6a5bb0; --unknown-soft:#ece8f8;
    --sans:"Segoe UI Variable Text","Segoe UI",system-ui,-apple-system,sans-serif;
    --mono:ui-monospace,"Cascadia Mono",Consolas,menlo,monospace;
  }
  @media (prefers-color-scheme:dark){:root{
    --paper:#0f1613; --card:#18211d; --ink:#e7ece9; --muted:#9aa8a3; --faint:#67756f;
    --line:#26322d; --accent:#5fc7b0; --accent-soft:#15332c; --accent-ink:#a6e5d8;
    --good:#5fc98a; --good-soft:#14301f;
    --maybe:#e0b552; --maybe-soft:#33260a;
    --taken:#8b9893; --taken-soft:#222926;
    --unknown:#b3a5e6; --unknown-soft:#241d3d;
  }}
  :root[data-theme="dark"]{
    --paper:#0f1613; --card:#18211d; --ink:#e7ece9; --muted:#9aa8a3; --faint:#67756f;
    --line:#26322d; --accent:#5fc7b0; --accent-soft:#15332c; --accent-ink:#a6e5d8;
    --good:#5fc98a; --good-soft:#14301f; --maybe:#e0b552; --maybe-soft:#33260a;
    --taken:#8b9893; --taken-soft:#222926; --unknown:#b3a5e6; --unknown-soft:#241d3d;
  }
  :root[data-theme="light"]{
    --paper:#f5f7f6; --card:#ffffff; --ink:#17241f; --muted:#5c6b66; --faint:#8b9893;
    --line:#e2e8e5; --accent:#0f6b5c; --accent-soft:#e4f0ec; --accent-ink:#0b4c41;
    --good:#1a7a4b; --good-soft:#e2f3e9; --maybe:#9a6a00; --maybe-soft:#fbefd6;
    --taken:#8a8f8c; --taken-soft:#eef1f0; --unknown:#6a5bb0; --unknown-soft:#ece8f8;
  }
  *{box-sizing:border-box;}
  body{margin:0;background:var(--paper);color:var(--ink);font-family:var(--sans);
       font-size:16px;line-height:1.55;-webkit-font-smoothing:antialiased;}
  .wrap{max-width:760px;margin:0 auto;padding:24px 18px 96px;}

  h1{font-size:22px;margin:0 0 2px;letter-spacing:-.01em;}
  .status{font-size:13px;color:var(--faint);margin-bottom:22px;}
  .status b{color:var(--good);font-weight:600;}
  .status.warn b{color:var(--maybe);}

  /* search card */
  .search{background:var(--card);border:1px solid var(--line);border-radius:14px;
          padding:20px;box-shadow:0 1px 2px rgba(0,0,0,.04);}
  .search h2{font-size:17px;margin:0 0 4px;}
  .search p.lead{margin:0 0 16px;color:var(--muted);font-size:14.5px;}
  .searchrow{display:flex;gap:10px;flex-wrap:wrap;}
  #seed{flex:1;min-width:220px;font:inherit;font-size:16px;padding:13px 15px;
        border:1.5px solid var(--line);border-radius:10px;background:var(--paper);
        color:var(--ink);}
  #seed:focus{outline:none;border-color:var(--accent);}
  .go{font:inherit;font-size:16px;font-weight:600;border:0;border-radius:10px;
      padding:13px 22px;background:var(--accent);color:#fff;cursor:pointer;}
  .go:hover{filter:brightness(1.07);}
  .go:disabled{opacity:.55;cursor:not-allowed;}
  :root[data-theme="dark"] .go, :root:not([data-theme]) .go{color:#06231d;}
  .chips{display:flex;gap:8px;flex-wrap:wrap;margin-top:13px;align-items:center;}
  .chips .lbl{font-size:13px;color:var(--faint);}
  .chip{font:inherit;font-size:13px;padding:5px 12px;border:1px solid var(--line);
        border-radius:20px;background:var(--paper);color:var(--muted);cursor:pointer;}
  .chip:hover{border-color:var(--accent);color:var(--accent);}

  .moretoggle{margin-top:14px;font-size:13.5px;color:var(--accent);cursor:pointer;
              background:none;border:0;padding:0;font-family:inherit;}
  .more{display:none;margin-top:14px;gap:14px;flex-wrap:wrap;
        padding-top:14px;border-top:1px solid var(--line);}
  .more.on{display:flex;}
  .more .f{display:flex;flex-direction:column;gap:5px;}
  .more label{font-size:12px;color:var(--faint);text-transform:uppercase;letter-spacing:.05em;}
  .more input,.more select{font:inherit;font-size:14px;padding:8px 10px;border:1px solid var(--line);
       border-radius:8px;background:var(--paper);color:var(--ink);}

  /* progress */
  .prog{display:none;margin-top:16px;padding:14px 16px;background:var(--accent-soft);
        border-radius:10px;}
  .prog.on{display:block;}
  .prog .msg{font-size:14px;color:var(--accent-ink);margin-bottom:8px;}
  .track{height:7px;border-radius:4px;background:rgba(0,0,0,.09);overflow:hidden;}
  .track > i{display:block;height:100%;background:var(--accent);width:0;transition:width .3s;}
  .done-msg{display:none;margin-top:16px;padding:13px 16px;border-radius:10px;
            font-size:14.5px;background:var(--accent-soft);color:var(--accent-ink);}
  .done-msg.err{background:var(--maybe-soft);color:var(--maybe);}
  .done-msg.on{display:block;}

  /* results */
  .resulthead{display:flex;align-items:center;gap:12px;margin:30px 0 12px;flex-wrap:wrap;}
  .resulthead h2{font-size:16px;margin:0;}
  .filters{display:flex;gap:7px;flex-wrap:wrap;margin-bottom:16px;}
  .filt{font:inherit;font-size:13px;padding:6px 13px;border:1px solid var(--line);
        border-radius:20px;background:var(--card);color:var(--muted);cursor:pointer;}
  .filt.on{background:var(--ink);color:var(--paper);border-color:var(--ink);}
  .rightlink{margin-left:auto;display:flex;gap:12px;align-items:center;}
  .rightlink a,.rightlink select{font-size:13px;color:var(--accent);text-decoration:none;
       background:none;border:0;font-family:inherit;cursor:pointer;}
  .rightlink select{border:1px solid var(--line);border-radius:7px;padding:5px 8px;color:var(--ink);}

  .card{background:var(--card);border:1px solid var(--line);border-radius:12px;
        padding:16px 18px;margin-bottom:12px;border-left:4px solid var(--edge,var(--line));}
  .card.good{--edge:var(--good);} .card.maybe{--edge:var(--maybe);}
  .card.taken{--edge:var(--taken);} .card.unknown{--edge:var(--unknown);}
  .card .toprow{display:flex;align-items:flex-start;gap:12px;}
  .card .name{font-size:16.5px;font-weight:600;flex:1;line-height:1.3;}
  .pill{font-size:12px;font-weight:700;padding:4px 11px;border-radius:20px;white-space:nowrap;}
  .pill.good{background:var(--good-soft);color:var(--good);}
  .pill.maybe{background:var(--maybe-soft);color:var(--maybe);}
  .pill.taken{background:var(--taken-soft);color:var(--taken);}
  .pill.unknown{background:var(--unknown-soft);color:var(--unknown);}
  .card .blurb{margin:9px 0 0;color:var(--muted);font-size:14.5px;}
  .card .actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:14px;align-items:center;}
  .leadbtns{display:flex;gap:4px;flex-wrap:wrap;}
  .leadbtn{font:inherit;font-size:12.5px;padding:5px 11px;border:1px solid var(--line);
           border-radius:16px;background:var(--paper);color:var(--muted);cursor:pointer;}
  .leadbtn.on{background:var(--accent-soft);border-color:var(--accent);color:var(--accent-ink);font-weight:600;}
  .seemore{margin-left:auto;font:inherit;font-size:13px;color:var(--accent);
           background:none;border:0;cursor:pointer;padding:0;}
  .detail{display:none;margin-top:14px;padding-top:14px;border-top:1px solid var(--line);}
  .detail.on{display:block;}
  .scores{display:grid;grid-template-columns:1fr 1fr;gap:9px 18px;margin-bottom:14px;}
  .score{font-size:13px;}
  .score .lab{display:flex;justify-content:space-between;color:var(--muted);margin-bottom:3px;}
  .score .meter{height:6px;background:var(--line);border-radius:4px;overflow:hidden;}
  .score .meter > i{display:block;height:100%;background:var(--accent);}
  .detail pre{white-space:pre-wrap;font-family:var(--mono);font-size:12px;line-height:1.5;
       background:var(--paper);border:1px solid var(--line);border-radius:8px;padding:12px;
       overflow-x:auto;max-height:340px;overflow-y:auto;margin:0 0 12px;}
  .detail pre a{color:var(--accent);}
  .notes{width:100%;font:inherit;font-size:14px;padding:10px;border:1px solid var(--line);
         border-radius:8px;background:var(--paper);color:var(--ink);resize:vertical;min-height:60px;}
  .noterow{display:flex;align-items:center;gap:10px;margin-top:6px;}
  .noterow .lab{font-size:12px;color:var(--faint);text-transform:uppercase;letter-spacing:.05em;}
  .saved{font-size:12px;color:var(--good);display:none;}
  .empty{text-align:center;color:var(--muted);padding:40px 16px;font-size:14.5px;}
  .empty b{color:var(--ink);}
</style>
</head>
<body>
<div class="wrap">
  <h1>Capybara Finder</h1>
  <div class="status" id="status">checking…</div>

  <div class="search">
    <h2>Find a gap</h2>
    <p class="lead">Type an industry or kind of customer. It tests lots of specific
      things people search for, then tells you which look <b>already taken</b>
      (most will) and which might be a real <b>gap</b> (rare — that's the point).</p>
    <div class="searchrow">
      <input id="seed" placeholder="e.g. small landlords" autocomplete="off">
      <button class="go" id="go">Find gaps</button>
    </div>
    <div class="chips">
      <span class="lbl">Try:</span>
      <button class="chip">UK trades</button>
      <button class="chip">small landlords</button>
      <button class="chip">childminders</button>
      <button class="chip">dog groomers</button>
      <button class="chip">driving instructors</button>
      <button class="chip">caterers</button>
      <button class="chip">home care</button>
      <button class="chip">holiday lets</button>
    </div>
    <button class="moretoggle" id="moretoggle">More options ▾</button>
    <div class="more" id="more">
      <div class="f">
        <label>What to do</label>
        <select id="mode">
          <option value="discover">Explore an industry</option>
          <option value="verify">Check one exact idea</option>
        </select>
      </div>
      <div class="f" id="howmanyf">
        <label>Ideas to test</label>
        <input id="limit" type="number" value="20" min="1" max="200" style="width:90px;">
      </div>
      <div class="f">
        <label>Country</label>
        <input id="country" value="UK" style="width:80px;">
      </div>
      <div class="f" style="justify-content:flex-end;">
        <button class="chip" id="preview" style="padding:8px 14px;">Preview ideas first (free)</button>
      </div>
    </div>
    <div class="prog" id="prog">
      <div class="msg" id="progmsg">Starting…</div>
      <div class="track"><i id="trackfill"></i></div>
    </div>
    <div class="done-msg" id="donemsg"></div>
  </div>

  <div class="resulthead">
    <h2>Results</h2>
    <div class="rightlink">
      <select id="seedfilter"><option value="">all searches</option></select>
      <a id="csv" href="/export.csv">Download CSV</a>
    </div>
  </div>
  <div class="filters" id="filters">
    <button class="filt on" data-f="all">All</button>
    <button class="filt" data-f="good">Possible gaps</button>
    <button class="filt" data-f="maybe">Worth a look</button>
    <button class="filt" data-f="taken">Already taken</button>
    <button class="filt" data-f="unknown">Couldn't check</button>
  </div>
  <div id="cards"></div>
  <div class="empty" id="empty"><b>No results yet.</b><br>Type an industry above and hit “Find gaps”.</div>
</div>

<script>
const $=(id)=>document.getElementById(id);
let DATA=[], FILT="all", poll=null;

function esc(s){return String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));}
function linkify(s){return s.replace(/(https?:\/\/[^\s<]+)/g,'<a href="$1" target="_blank" rel="noopener noreferrer">$1</a>');}

/* turn the engine's verdict into plain English */
function verdict(run){
  const kr=(run.kill_reason||"").toLowerCase();
  if(run.band==="pursue")
    return {k:"good",label:"Possible gap",blurb:"Looks like a real gap. Worth testing with real people before building."};
  if(run.band==="investigate")
    return {k:"maybe",label:"Worth a look",blurb:"Nobody's clearly winning here — it could be worth a closer look."};
  if(run.band==="unverified"){
    let b="Couldn't check this one.";
    if(kr.includes("never assessed")||kr.includes("no web")) b="Couldn't check — the web search key isn't set for this run.";
    else if(kr.includes("none productive")||kr.includes("no ")) b="Couldn't check — no results came back for this phrase.";
    return {k:"unknown",label:"Couldn't check",blurb:b};
  }
  // reject
  let b="The market already looks well served.";
  if(kr.includes("polished incumbent")){
    const who=(run.kill_reason.split(":")[1]||"").trim();
    b="A strong competitor already does this"+(who?": "+who:".");
  } else if(kr.includes("hard blocker")){
    b="Blocked by a rule or licence you'd need to build it.";
  } else if(kr.includes("pays")||kr.includes("pay ")){
    b="No sign that anyone actually pays in this area.";
  } else if(kr.includes("reach")){
    b="No obvious place to find and reach these customers.";
  } else if(kr.includes("supply already")||kr.includes("adequate")){
    b="Several tools already cover this well enough.";
  }
  return {k:"taken",label:"Already taken",blurb:b};
}

async function loadStatus(){
  try{
    const p=await (await fetch("/api/providers")).json();
    const el=$("status");
    if(p.fixture){el.innerHTML="Demo mode — showing sample data, not live results";el.classList.add("warn");}
    else if(!p.any_live){el.innerHTML="No search key set — results will say “couldn't check”. ";el.classList.add("warn");}
    else{el.innerHTML="Search is <b>live</b> — ready to go.";}
  }catch(e){}
}
async function loadSeeds(){
  const s=await (await fetch("/api/seeds")).json();
  const sel=$("seedfilter"),cur=sel.value;
  sel.innerHTML='<option value="">all searches</option>';
  s.forEach(x=>{const o=document.createElement("option");o.value=o.textContent=x;sel.appendChild(o);});
  sel.value=cur;
}
async function load(){
  const p=new URLSearchParams();
  if($("seedfilter").value)p.set("seed",$("seedfilter").value);
  DATA=await (await fetch("/api/runs?"+p)).json();
  render();
  $("csv").href="/export.csv"+($("seedfilter").value?("?seed="+encodeURIComponent($("seedfilter").value)):"");
}

function render(){
  const box=$("cards");box.innerHTML="";
  const rows=DATA.filter(r=>FILT==="all"||verdict(r).k===FILT);
  $("empty").style.display=DATA.length?"none":"block";
  if(DATA.length && !rows.length){
    box.innerHTML='<div class="empty">Nothing in this group.</div>';return;
  }
  rows.forEach(run=>box.appendChild(card(run)));
}

function card(run){
  const v=verdict(run);
  const el=document.createElement("div");
  el.className="card "+v.k;
  el.innerHTML=
    '<div class="toprow"><div class="name">'+esc(run.candidate)+'</div>'+
    '<span class="pill '+v.k+'">'+v.label+'</span></div>'+
    '<p class="blurb">'+esc(v.blurb)+'</p>'+
    '<div class="actions"><div class="leadbtns"></div>'+
    '<button class="seemore">See details ▾</button></div>'+
    '<div class="detail"></div>';
  el.querySelector(".leadbtns").appendChild(leadButtons(run));
  const more=el.querySelector(".seemore"), det=el.querySelector(".detail");
  more.onclick=()=>{
    if(det.classList.contains("on")){det.classList.remove("on");more.textContent="See details ▾";return;}
    more.textContent="Hide details ▴";
    if(!det.dataset.loaded){fillDetail(run,det);det.dataset.loaded="1";}
    det.classList.add("on");
  };
  return el;
}

const STATUSES=[["new","New"],["shortlist","Shortlist"],["chasing","Chasing"],["rejected","Rejected"]];
function leadButtons(run){
  const wrap=document.createElement("div");wrap.className="leadbtns";
  STATUSES.forEach(([val,lab])=>{
    const b=document.createElement("button");
    b.className="leadbtn"+(((run.lead_status||"new")===val)?" on":"");
    b.textContent=lab;
    b.onclick=async()=>{
      await fetch("/api/lead",{method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({candidate:run.candidate,status:val})});
      run.lead_status=val;
      wrap.querySelectorAll(".leadbtn").forEach(x=>x.classList.remove("on"));
      b.classList.add("on");
    };
    wrap.appendChild(b);
  });
  return wrap;
}

const SCORE_LABELS=[
  ["demand","People searching",4],
  ["supply_weak","Gap in competition",4],
  ["pay","Signs people pay",3],
  ["reach","Easy to reach them",3],
  ["buildable","Nothing blocking",1],
];
async function fillDetail(run,det){
  let html="";
  if(!run.score.unverified){
    html+='<div class="scores">';
    SCORE_LABELS.forEach(([k,lab,max])=>{
      const v=run.score[k], pct=Math.round(100*v/max);
      html+='<div class="score"><div class="lab"><span>'+lab+'</span><span>'+v+' / '+max+'</span></div>'+
            '<div class="meter"><i style="width:'+pct+'%"></i></div></div>';
    });
    html+='</div>';
  }
  det.innerHTML=html+'<pre>loading the full write-up…</pre>'+
    '<div class="noterow"><span class="lab">Your notes</span><span class="saved">saved ✓</span></div>'+
    '<textarea class="notes" placeholder="Jot anything here — saved automatically"></textarea>';
  const pre=det.querySelector("pre");
  const text=await (await fetch("/api/report?candidate="+encodeURIComponent(run.candidate)+"&depth="+encodeURIComponent(run.depth))).text();
  pre.innerHTML=linkify(esc(text));
  const ta=det.querySelector(".notes"); ta.value=run.lead_notes||"";
  ta.onblur=async()=>{
    await fetch("/api/lead",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({candidate:run.candidate,notes:ta.value})});
    run.lead_notes=ta.value;
    const s=det.querySelector(".saved");s.style.display="inline";setTimeout(()=>s.style.display="none",1400);
  };
}

/* launch */
function setMode(){
  const v=$("mode").value==="verify";
  $("howmanyf").style.display=v?"none":"flex";
  $("seed").placeholder=v?"e.g. eicr expiry reminder app":"e.g. small landlords";
}
async function run(){
  const seed=$("seed").value.trim();
  if(!seed){$("seed").focus();return;}
  const mode=$("mode").value;
  const payload=mode==="verify"
    ?{mode:"verify",candidate:seed,country:$("country").value}
    :{mode:"discover",seed:seed,limit:parseInt($("limit").value)||20,country:$("country").value};
  $("go").disabled=true;$("donemsg").classList.remove("on");
  const res=await (await fetch("/api/run",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(payload)})).json();
  if(res.error){done(res.error,true);$("go").disabled=false;return;}
  $("prog").classList.add("on");watch(res.job_id);
}
function watch(id){
  if(poll)clearInterval(poll);
  poll=setInterval(async()=>{
    const j=await (await fetch("/api/job?id="+id)).json();
    if(j.error){clearInterval(poll);return;}
    const pct=j.total?Math.round(100*j.done/j.total):0;
    $("trackfill").style.width=pct+"%";
    $("progmsg").textContent=j.status==="running"
      ? ("Testing ideas… "+j.done+" of "+j.total+(j.last?"  ·  checking “"+j.last+"”":""))
      : "Finishing…";
    if(j.status==="done"||j.status==="error"){
      clearInterval(poll);$("go").disabled=false;
      setTimeout(()=>$("prog").classList.remove("on"),800);
      if(j.status==="error"){done("Something went wrong: "+j.error,true);}
      else{
        let m = j.kind==="verify" ? "Done — see the result below."
          : ("Done — tested "+j.done+", found "+j.survivors+" worth a look. (Most being “taken” is normal.)");
        done(m,false);
      }
      await loadSeeds();await load();
    }
  },1000);
}
function done(msg,err){const d=$("donemsg");d.textContent=msg;d.className="done-msg on"+(err?" err":"");}

async function preview(){
  const seed=$("seed").value.trim();if(!seed){$("seed").focus();return;}
  const res=await (await fetch("/api/preview",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({seed:seed,limit:parseInt($("limit").value)||20})})).json();
  if(res.error){done(res.error,true);return;}
  done("It would test "+res.candidates.length+" ideas, like: "+res.candidates.slice(0,6).join(" · ")+(res.candidates.length>6?" …":"")+". Hit “Find gaps” to run them.",false);
}

document.querySelectorAll(".chip").forEach(c=>{
  if(c.id) return;
  c.onclick=()=>{$("seed").value=c.textContent;$("seed").focus();};
});
$("go").onclick=run;
$("preview").onclick=preview;
$("mode").onchange=setMode;
$("moretoggle").onclick=()=>{$("more").classList.toggle("on");
  $("moretoggle").textContent=$("more").classList.contains("on")?"Fewer options ▴":"More options ▾";};
$("seedfilter").onchange=load;
$("seed").addEventListener("keydown",e=>{if(e.key==="Enter")run();});
document.querySelectorAll(".filt").forEach(b=>b.onclick=()=>{
  document.querySelectorAll(".filt").forEach(x=>x.classList.remove("on"));
  b.classList.add("on");FILT=b.dataset.f;render();
});
setMode();loadStatus();loadSeeds();load();
</script>
</body>
</html>
"""
