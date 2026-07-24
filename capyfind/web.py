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
<title>capyfind</title>
<style>
  :root {
    --paper:#f4f7f6; --card:#fff; --ink:#16211f; --muted:#5f6d6a; --faint:#8a9793;
    --line:#dbe3e0; --accent:#0f6b5c; --accent-dim:#e0efeb;
    --pursue:#1b7a4b; --investigate:#8a6000; --reject:#64716e; --unverified:#5b4b8a;
    --mono:ui-monospace,"Cascadia Mono",Consolas,"SF Mono",Menlo,monospace;
    --sans:"Segoe UI Variable Text","Segoe UI",system-ui,-apple-system,sans-serif;
  }
  @media (prefers-color-scheme:dark){:root{
    --paper:#101816; --card:#17211f; --ink:#e6ece9; --muted:#9aa8a4; --faint:#6d7b77;
    --line:#26332f; --accent:#5ec6b0; --accent-dim:#14302b;
    --pursue:#5cc98a; --investigate:#d8a63c; --reject:#8b9894; --unverified:#a493d8;
  }}
  :root[data-theme="dark"]{
    --paper:#101816; --card:#17211f; --ink:#e6ece9; --muted:#9aa8a4; --faint:#6d7b77;
    --line:#26332f; --accent:#5ec6b0; --accent-dim:#14302b;
    --pursue:#5cc98a; --investigate:#d8a63c; --reject:#8b9894; --unverified:#a493d8;
  }
  :root[data-theme="light"]{
    --paper:#f4f7f6; --card:#fff; --ink:#16211f; --muted:#5f6d6a; --faint:#8a9793;
    --line:#dbe3e0; --accent:#0f6b5c; --accent-dim:#e0efeb;
    --pursue:#1b7a4b; --investigate:#8a6000; --reject:#64716e; --unverified:#5b4b8a;
  }
  *{box-sizing:border-box;}
  body{margin:0;background:var(--paper);color:var(--ink);font-family:var(--sans);
       font-size:14px;line-height:1.55;-webkit-font-smoothing:antialiased;}
  header.top{border-bottom:2px solid var(--ink);padding:16px clamp(14px,3vw,28px);
             display:flex;align-items:baseline;gap:14px;flex-wrap:wrap;}
  header.top h1{margin:0;font-size:18px;letter-spacing:-.01em;}
  header.top .tag{font-family:var(--mono);font-size:11px;letter-spacing:.14em;
                  text-transform:uppercase;color:var(--accent);}
  .provider{margin-left:auto;font-family:var(--mono);font-size:11px;
            color:var(--faint);}
  .wrap{max-width:1180px;margin:0 auto;padding:clamp(16px,3vw,28px);
        display:flex;flex-direction:column;gap:22px;}

  /* launch panel */
  .launch{background:var(--card);border:1px solid var(--line);border-radius:6px;
          padding:18px;display:flex;flex-direction:column;gap:14px;}
  .launch .row{display:flex;gap:12px;flex-wrap:wrap;align-items:flex-end;}
  .field{display:flex;flex-direction:column;gap:5px;}
  .field label{font-family:var(--mono);font-size:11px;letter-spacing:.08em;
               text-transform:uppercase;color:var(--faint);}
  .field input,.field select{font:inherit;padding:8px 10px;border:1px solid var(--line);
       border-radius:5px;background:var(--paper);color:var(--ink);min-width:0;}
  .field input:focus,.field select:focus{outline:2px solid var(--accent);
       outline-offset:-1px;border-color:var(--accent);}
  #seed{min-width:260px;flex:1;}
  .btn{font:inherit;font-weight:600;border:0;border-radius:5px;padding:9px 18px;
       background:var(--accent);color:var(--card);cursor:pointer;transition:.15s;}
  .btn:hover{filter:brightness(1.08);}
  .btn:disabled{opacity:.5;cursor:not-allowed;}
  .btn.ghost{background:transparent;color:var(--accent);
             border:1px solid var(--line);}
  .hint{color:var(--muted);font-size:12.5px;}
  .hint code{font-family:var(--mono);font-size:12px;}

  /* progress */
  .progress{display:none;flex-direction:column;gap:8px;}
  .progress.on{display:flex;}
  .bar{height:8px;border-radius:5px;background:var(--accent-dim);overflow:hidden;}
  .bar > i{display:block;height:100%;background:var(--accent);width:0;
           transition:width .3s;}
  .pstat{font-family:var(--mono);font-size:12px;color:var(--muted);
         display:flex;justify-content:space-between;gap:10px;}
  .banner{padding:9px 12px;border-radius:5px;font-size:12.5px;}
  .banner.warn{background:var(--accent-dim);color:var(--accent);}
  .banner.err{background:#fde8e6;color:#8a2018;}
  @media (prefers-color-scheme:dark){.banner.err{background:#3a1512;color:#f0b3aa;}}

  /* controls */
  .controls{display:flex;gap:14px;align-items:center;flex-wrap:wrap;
            font-size:13px;}
  .controls label{display:flex;gap:6px;align-items:center;color:var(--muted);}
  .controls select{font:inherit;padding:5px 9px;border:1px solid var(--line);
                   border-radius:5px;background:var(--card);color:var(--ink);}
  .spacer{flex:1;}
  .count{font-family:var(--mono);font-size:12px;color:var(--faint);}

  /* table */
  .tablewrap{overflow-x:auto;border:1px solid var(--line);border-radius:6px;
             background:var(--card);}
  table{width:100%;border-collapse:collapse;font-size:13px;}
  th{text-align:left;font-family:var(--mono);font-size:10.5px;letter-spacing:.09em;
     text-transform:uppercase;color:var(--faint);font-weight:700;padding:11px 12px;
     border-bottom:1px solid var(--line);white-space:nowrap;position:sticky;top:0;
     background:var(--card);}
  td{padding:10px 12px;border-bottom:1px solid var(--line);vertical-align:top;}
  tbody tr.row:hover td{background:var(--accent-dim);}
  tbody tr.row{cursor:pointer;}
  .cand{font-weight:560;}
  .kill{color:var(--muted);font-size:12px;margin-top:2px;max-width:52ch;}
  .num{font-variant-numeric:tabular-nums;text-align:right;white-space:nowrap;}
  .band{font-family:var(--mono);font-size:10.5px;font-weight:700;letter-spacing:.08em;
        text-transform:uppercase;padding:3px 8px;border-radius:3px;white-space:nowrap;}
  .band.pursue{background:var(--pursue);color:var(--card);}
  .band.investigate{background:var(--investigate);color:var(--card);}
  .band.reject{background:transparent;color:var(--reject);border:1px solid var(--line);}
  .band.unverified{background:transparent;color:var(--unverified);
                   border:1px solid var(--line);}
  .lead-sel{font:inherit;font-size:12px;padding:4px 6px;border:1px solid var(--line);
            border-radius:4px;background:var(--paper);color:var(--ink);}
  .detail td{background:var(--paper);}
  .detail pre{white-space:pre-wrap;font-family:var(--mono);font-size:12px;
              margin:0 0 12px;overflow-x:auto;line-height:1.5;}
  .detail a{color:var(--accent);}
  .notes{width:100%;font:inherit;font-size:12.5px;padding:8px;border:1px solid var(--line);
         border-radius:5px;background:var(--card);color:var(--ink);resize:vertical;
         min-height:52px;}
  .empty{padding:44px;text-align:center;color:var(--muted);}
  .savednote{font-family:var(--mono);font-size:11px;color:var(--pursue);margin-left:8px;}
</style>
</head>
<body>
<header class="top">
  <h1>capyfind</h1>
  <span class="tag">micro-niche gap finder</span>
  <span class="provider" id="provider">checking sources...</span>
</header>

<div class="wrap">

  <div class="launch">
    <div class="row">
      <div class="field" style="flex:1;">
        <label for="seed" id="seedlabel">Seed — an industry or theme</label>
        <input id="seed" placeholder="e.g. UK trades" autocomplete="off">
      </div>
      <div class="field">
        <label for="mode">Mode</label>
        <select id="mode">
          <option value="discover">Discover — wide sweep</option>
          <option value="verify">Verify — one niche, deep</option>
        </select>
      </div>
      <div class="field" id="limitfield">
        <label for="limit">How many</label>
        <input id="limit" type="number" value="20" min="1" max="200" style="width:90px;">
      </div>
      <div class="field">
        <label for="country">Country</label>
        <input id="country" value="UK" style="width:80px;">
      </div>
      <button class="btn" id="go">Run</button>
      <button class="btn ghost" id="preview">Preview ideas</button>
    </div>
    <div class="hint" id="hint">
      Discover generates and tests many candidate phrases. Verify does one deep
      dive — type the exact phrase in the seed box and switch mode to Verify.
    </div>
    <div class="progress" id="progress">
      <div class="bar"><i id="barfill"></i></div>
      <div class="pstat"><span id="pmsg">starting...</span><span id="pcount"></span></div>
    </div>
    <div id="runbanner"></div>
  </div>

  <div class="controls">
    <label><input type="checkbox" id="survivors"> survivors only</label>
    <label>seed
      <select id="seedfilter"><option value="">all</option></select>
    </label>
    <label>status
      <select id="statusfilter">
        <option value="">any</option>
        <option value="new">new</option>
        <option value="shortlist">shortlist</option>
        <option value="chasing">chasing</option>
        <option value="rejected">rejected</option>
      </select>
    </label>
    <div class="spacer"></div>
    <span class="count" id="count"></span>
    <a class="btn ghost" id="csv" href="/export.csv">Export CSV</a>
  </div>

  <div class="tablewrap">
    <table>
      <thead><tr>
        <th>candidate</th><th class="num">dem</th><th class="num">sup</th>
        <th class="num">pay</th><th class="num">rch</th><th class="num">bld</th>
        <th class="num">ratio</th><th class="num">score</th>
        <th>verdict</th><th>lead</th>
      </tr></thead>
      <tbody id="rows"></tbody>
    </table>
    <div class="empty" id="empty">Nothing yet. Run a sweep above to get started.</div>
  </div>
</div>

<script>
const $ = (id) => document.getElementById(id);
let DATA = [], poll = null;

function esc(s){return String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));}
function linkify(s){return s.replace(/(https?:\/\/[^\s<]+)/g,'<a href="$1" target="_blank" rel="noopener noreferrer">$1</a>');}

async function loadProviders(){
  try{
    const p = await (await fetch("/api/providers")).json();
    const el = $("provider");
    if(p.fixture){ el.textContent = "FIXTURE MODE — not live findings"; el.style.color="var(--investigate)"; }
    else if(!p.any_live){ el.textContent = "no web key — runs will be UNVERIFIED"; el.style.color="var(--unverified)"; }
    else { el.textContent = "web search live"; el.style.color="var(--pursue)"; }
  }catch(e){ $("provider").textContent=""; }
}

async function loadSeeds(){
  const seeds = await (await fetch("/api/seeds")).json();
  const sel = $("seedfilter"); const cur = sel.value;
  sel.innerHTML = '<option value="">all</option>';
  seeds.forEach(s=>{const o=document.createElement("option");o.value=o.textContent=s;sel.appendChild(o);});
  sel.value = cur;
}

async function load(){
  const p = new URLSearchParams();
  if($("survivors").checked) p.set("survivors","1");
  if($("seedfilter").value) p.set("seed",$("seedfilter").value);
  DATA = await (await fetch("/api/runs?"+p)).json();
  const sf = $("statusfilter").value;
  const rows = sf ? DATA.filter(r=>(r.lead_status||"new")===sf) : DATA;
  render(rows);
  $("csv").href = "/export.csv" + ($("seedfilter").value?("?seed="+encodeURIComponent($("seedfilter").value)):"");
}

function cell(v,un){return un?"-":v;}

function render(rows){
  const body = $("rows");
  body.innerHTML = "";
  $("empty").style.display = rows.length ? "none" : "block";
  $("count").textContent = rows.length + " shown";
  rows.forEach((run,i)=>{
    const s = run.score, un = s.unverified;
    const tr = document.createElement("tr");
    tr.className = "row";
    tr.innerHTML =
      '<td><div class="cand">'+esc(run.candidate)+'</div><div class="kill">'+esc(run.kill_reason)+'</div></td>'+
      '<td class="num">'+cell(s.demand,un)+'</td>'+
      '<td class="num">'+cell(s.supply_weak,un)+'</td>'+
      '<td class="num">'+cell(s.pay,un)+'</td>'+
      '<td class="num">'+cell(s.reach,un)+'</td>'+
      '<td class="num">'+cell(s.buildable,un)+'</td>'+
      '<td class="num">'+run.ratio+'</td>'+
      '<td class="num"><strong>'+esc(run.composite_display)+'</strong></td>'+
      '<td><span class="band '+run.band+'">'+run.band+'</span></td>'+
      '<td></td>';
    tr.lastChild.appendChild(leadSelect(run));
    tr.querySelectorAll("td").forEach((td,idx)=>{ if(idx<9) td.onclick=()=>toggle(run,tr); });
    body.appendChild(tr);
  });
}

function leadSelect(run){
  const sel = document.createElement("select");
  sel.className = "lead-sel";
  ["new","shortlist","chasing","rejected"].forEach(st=>{
    const o=document.createElement("option");o.value=o.textContent=st;
    if((run.lead_status||"new")===st)o.selected=true;sel.appendChild(o);
  });
  sel.onclick = (e)=>e.stopPropagation();
  sel.onchange = async ()=>{
    await fetch("/api/lead",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({candidate:run.candidate,status:sel.value})});
    run.lead_status = sel.value;
    if($("statusfilter").value) load();
  };
  return sel;
}

async function toggle(run,tr){
  const nx = tr.nextElementSibling;
  if(nx && nx.classList.contains("detail")){ nx.remove(); return; }
  const text = await (await fetch("/api/report?candidate="+encodeURIComponent(run.candidate)+"&depth="+encodeURIComponent(run.depth))).text();
  const row = document.createElement("tr");
  row.className = "detail";
  row.innerHTML = '<td colspan="10"><pre>'+linkify(esc(text))+'</pre>'+
    '<label class="field"><span style="font-family:var(--mono);font-size:11px;color:var(--faint);text-transform:uppercase;letter-spacing:.08em;">Your notes</span></label>'+
    '<textarea class="notes" placeholder="Add a note — saved when you click away"></textarea>'+
    '<span class="savednote" style="display:none;">saved</span></td>';
  const ta = row.querySelector(".notes");
  ta.value = run.lead_notes || "";
  ta.onblur = async ()=>{
    await fetch("/api/lead",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({candidate:run.candidate,notes:ta.value})});
    run.lead_notes = ta.value;
    const s = row.querySelector(".savednote"); s.style.display="inline";
    setTimeout(()=>s.style.display="none",1400);
  };
  tr.after(row);
}

function setMode(){
  const verify = $("mode").value==="verify";
  $("limitfield").style.display = verify ? "none":"flex";
  $("seedlabel").textContent = verify ? "Niche — the exact phrase to deep-dive" : "Seed — an industry or theme";
  $("seed").placeholder = verify ? "e.g. eicr expiry reminder app" : "e.g. UK trades";
}

async function run(){
  const seed = $("seed").value.trim();
  if(!seed){ $("seed").focus(); return; }
  const mode = $("mode").value;
  const payload = mode==="verify"
    ? {mode:"verify", candidate:seed, country:$("country").value}
    : {mode:"discover", seed:seed, limit:parseInt($("limit").value)||20, country:$("country").value};
  $("go").disabled = true; $("preview").disabled = true;
  $("runbanner").innerHTML = "";
  const res = await (await fetch("/api/run",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(payload)})).json();
  if(res.error){ banner(res.error,"err"); $("go").disabled=false; $("preview").disabled=false; return; }
  $("progress").classList.add("on");
  watch(res.job_id);
}

function watch(id){
  if(poll) clearInterval(poll);
  poll = setInterval(async ()=>{
    const j = await (await fetch("/api/job?id="+id)).json();
    if(j.error){ clearInterval(poll); return; }
    const pct = j.total ? Math.round(100*j.done/j.total) : 0;
    $("barfill").style.width = pct+"%";
    $("pmsg").textContent = j.status==="running" ? ("testing: "+(j.last||"...")) : j.status;
    $("pcount").textContent = j.done+" / "+j.total;
    if(j.status==="done" || j.status==="error"){
      clearInterval(poll);
      $("go").disabled=false; $("preview").disabled=false;
      setTimeout(()=>$("progress").classList.remove("on"), 1200);
      if(j.status==="error"){ banner("Run failed: "+j.error,"err"); }
      else {
        let msg = j.kind==="verify" ? "Done." : ("Done — "+j.survivors+" survived of "+j.done+" tested.");
        if(j.stopped) msg += " Stopped early: "+j.stopped;
        if(j.warning) msg += " "+j.warning;
        banner(msg,"warn");
      }
      await loadSeeds(); await load();
    }
  }, 1000);
}

function banner(text,kind){ $("runbanner").innerHTML = '<div class="banner '+kind+'">'+esc(text)+'</div>'; }

async function preview(){
  const seed = $("seed").value.trim();
  if(!seed){ $("seed").focus(); return; }
  const res = await (await fetch("/api/preview",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({seed:seed, limit:parseInt($("limit").value)||30})})).json();
  if(res.error){ banner(res.error,"err"); return; }
  banner("Would test "+res.candidates.length+" phrases: "+res.candidates.slice(0,8).join(" · ")+(res.candidates.length>8?" …":""),"warn");
}

$("go").onclick = run;
$("preview").onclick = preview;
$("mode").onchange = setMode;
$("survivors").onchange = load;
$("seedfilter").onchange = load;
$("statusfilter").onchange = load;
$("seed").addEventListener("keydown",e=>{if(e.key==="Enter")run();});
setMode(); loadProviders(); loadSeeds(); load();
</script>
</body>
</html>
"""
