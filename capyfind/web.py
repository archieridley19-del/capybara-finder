"""Local web UI for reading results.

Deliberately thin: the CLI does the work, this just makes the evidence trail
pleasant to read. Stdlib http.server, no framework, no build step, binds to
localhost only.
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

from .models import run_from_dict
from .report import render_report, to_csv
from .store import Store

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>capyfind</title>
<style>
  :root { color-scheme: light dark; --bg:#fff; --fg:#1a1a1a; --muted:#666;
          --line:#e3e3e3; --card:#fafafa; --accent:#2d6a4f; }
  @media (prefers-color-scheme: dark) {
    :root { --bg:#151515; --fg:#e8e8e8; --muted:#999; --line:#2e2e2e;
            --card:#1e1e1e; --accent:#74c69d; }
  }
  * { box-sizing: border-box; }
  body { margin:0; padding:24px; background:var(--bg); color:var(--fg);
         font:15px/1.5 ui-sans-serif, system-ui, -apple-system, sans-serif; }
  header { max-width:1100px; margin:0 auto 20px; }
  h1 { font-size:20px; margin:0 0 4px; }
  .sub { color:var(--muted); font-size:13px; }
  .controls { max-width:1100px; margin:0 auto 16px; display:flex;
              gap:12px; align-items:center; flex-wrap:wrap; }
  .wrap { max-width:1100px; margin:0 auto; }
  table { width:100%; border-collapse:collapse; font-size:13px; }
  th { text-align:left; padding:8px 6px; border-bottom:2px solid var(--line);
       font-weight:600; color:var(--muted); font-size:11px;
       text-transform:uppercase; letter-spacing:.04em; }
  td { padding:8px 6px; border-bottom:1px solid var(--line);
       vertical-align:top; }
  tr.row { cursor:pointer; }
  tr.row:hover td { background:var(--card); }
  .band { font-size:11px; font-weight:700; padding:2px 7px; border-radius:3px;
          white-space:nowrap; }
  .pursue { background:#d8f3dc; color:#1b4332; }
  .investigate { background:#fff3cd; color:#664d03; }
  .reject { background:#f1f1f1; color:#555; }
  .unverified { background:#e7e0f7; color:#3d2a66; }
  @media (prefers-color-scheme: dark) {
    .pursue{background:#1b4332;color:#d8f3dc}
    .investigate{background:#4a3a05;color:#ffe8a3}
    .reject{background:#2a2a2a;color:#aaa}
    .unverified{background:#332a55;color:#d9ccff}
  }
  .num { font-variant-numeric: tabular-nums; text-align:right; }
  .kill { color:var(--muted); font-size:12px; }
  .detail td { background:var(--card); }
  pre { white-space:pre-wrap; font:12px/1.45 ui-monospace, monospace;
        margin:0; overflow-x:auto; }
  .empty { padding:40px; text-align:center; color:var(--muted); }
  button, select { font:inherit; padding:5px 10px; border:1px solid var(--line);
           border-radius:5px; background:var(--card); color:var(--fg);
           cursor:pointer; }
  a { color:var(--accent); }
</style>
</head>
<body>
<header class="wrap">
  <h1>capyfind</h1>
  <div class="sub">Ranked by composite score. Unverified rows sort last and
  never show a number. Click any row for its full evidence trail.</div>
</header>
<div class="controls">
  <label><input type="checkbox" id="survivors"> show only survivors</label>
  <select id="seed"><option value="">all seeds</option></select>
  <a href="/export.csv"><button>export CSV</button></a>
  <span class="sub" id="count"></span>
</div>
<div class="wrap"><table>
  <thead><tr>
    <th>candidate</th><th class="num">dem</th><th class="num">sup</th>
    <th class="num">pay</th><th class="num">rch</th><th class="num">bld</th>
    <th class="num">ratio</th><th class="num">score</th><th>verdict</th>
  </tr></thead>
  <tbody id="rows"></tbody>
</table>
<div class="empty" id="empty" hidden>No results yet. Run
<code>capyfind discover "UK trades"</code> first.</div>
</div>
<script>
let DATA = [];
const q = (id) => document.getElementById(id);

async function load() {
  const params = new URLSearchParams();
  if (q('survivors').checked) params.set('survivors', '1');
  if (q('seed').value) params.set('seed', q('seed').value);
  const res = await fetch('/api/runs?' + params);
  DATA = await res.json();
  render();
}

function cell(v, unverified) { return unverified ? '-' : v; }

function render() {
  const body = q('rows');
  body.innerHTML = '';
  q('empty').hidden = DATA.length > 0;
  q('count').textContent = DATA.length + ' results';

  DATA.forEach((run, i) => {
    const s = run.score, un = s.unverified;
    const tr = document.createElement('tr');
    tr.className = 'row';
    tr.innerHTML = `
      <td>${esc(run.candidate)}<div class="kill">${esc(run.kill_reason)}</div></td>
      <td class="num">${cell(s.demand, un)}</td>
      <td class="num">${cell(s.supply_weak, un)}</td>
      <td class="num">${cell(s.pay, un)}</td>
      <td class="num">${cell(s.reach, un)}</td>
      <td class="num">${cell(s.buildable, un)}</td>
      <td class="num">${run.ratio}</td>
      <td class="num"><strong>${esc(run.composite_display)}</strong></td>
      <td><span class="band ${run.band}">${run.band}</span></td>`;
    tr.onclick = () => toggle(i, tr);
    body.appendChild(tr);
  });
}

async function toggle(i, tr) {
  const next = tr.nextElementSibling;
  if (next && next.classList.contains('detail')) { next.remove(); return; }
  const run = DATA[i];
  const res = await fetch('/api/report?candidate=' +
    encodeURIComponent(run.candidate) + '&depth=' + encodeURIComponent(run.depth));
  const text = await res.text();
  const row = document.createElement('tr');
  row.className = 'detail';
  row.innerHTML = `<td colspan="9"><pre>${linkify(esc(text))}</pre></td>`;
  tr.after(row);
}

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => (
    {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
function linkify(s) {
  return s.replace(/(https?:\\/\\/[^\\s<]+)/g,
    '<a href="$1" target="_blank" rel="noopener noreferrer">$1</a>');
}

q('survivors').onchange = load;
q('seed').onchange = load;
fetch('/api/seeds').then(r => r.json()).then(seeds => {
  seeds.forEach(s => {
    const o = document.createElement('option');
    o.value = o.textContent = s;
    q('seed').appendChild(o);
  });
});
load();
</script>
</body>
</html>
"""


def make_handler(store: Store):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args) -> None:  # keep the console readable
            pass

        def _send(self, body: bytes, ctype: str, status: int = 200) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)
            seed = (query.get("seed") or [None])[0]
            survivors = bool(query.get("survivors"))

            if parsed.path == "/":
                self._send(PAGE.encode(), "text/html; charset=utf-8")

            elif parsed.path == "/api/runs":
                runs = store.list_runs(seed=seed, survivors_only=survivors)
                self._send(
                    json.dumps(runs).encode(), "application/json; charset=utf-8"
                )

            elif parsed.path == "/api/seeds":
                with store._connect() as conn:
                    rows = conn.execute(
                        "SELECT DISTINCT seed FROM runs WHERE seed IS NOT NULL"
                    ).fetchall()
                self._send(
                    json.dumps([r["seed"] for r in rows]).encode(),
                    "application/json; charset=utf-8",
                )

            elif parsed.path == "/api/report":
                candidate = (query.get("candidate") or [""])[0]
                depth = (query.get("depth") or ["shallow"])[0]
                data = store.get_run(candidate, depth)
                if data is None:
                    self._send(b"not found", "text/plain; charset=utf-8", 404)
                    return
                text = render_report(run_from_dict(data), show_anchors=False)
                self._send(text.encode(), "text/plain; charset=utf-8")

            elif parsed.path == "/export.csv":
                runs = [run_from_dict(d) for d in store.list_runs(seed=seed)]
                self._send(
                    to_csv(runs).encode(),
                    "text/csv; charset=utf-8",
                )

            else:
                self._send(b"not found", "text/plain; charset=utf-8", 404)

    return Handler


def serve(db: str = "capyfind.db", port: int = 8000, host: str = "127.0.0.1") -> None:
    store = Store(db)
    server = HTTPServer((host, port), make_handler(store))
    print(f"capyfind UI on http://{host}:{port}  (ctrl-c to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        server.server_close()
