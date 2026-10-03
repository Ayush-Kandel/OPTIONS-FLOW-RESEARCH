"""Live dashboard: http://localhost:8050 (only reachable from this PC).

Started by the logger in a background thread; reads flow.db read-only and refreshes every 15 s.
Shows what every bot is doing: Scout (capture + credits), Herald (pings), Judges, Arena,
Simulator, Analyst and Auditor.
"""

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).parent
DB = HERE / "flow.db"
PORT = 8050
MY_TICKERS = None  # filled from pipeline at start


def _rows(db, sql, args=()):
    cur = db.execute(sql, args)
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _latest_report(db, bot):
    try:
        r = db.execute("SELECT run_utc, data_json FROM bot_reports WHERE bot = ? ORDER BY id DESC LIMIT 1",
                       (bot,)).fetchone()
    except sqlite3.OperationalError:  # bots haven't run yet
        return None
    return {"run_utc": r[0], "data": json.loads(r[1])} if r else None


def state():
    import pipeline as pl
    db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=10)
    try:
        from flow_logger import now_et
        et = now_et()
        today = et.date().isoformat()
        last_poll = db.execute("SELECT MAX(started_utc) FROM polls").fetchone()[0]
        age = ((datetime.now(timezone.utc) - datetime.fromisoformat(last_poll)).total_seconds()
               if last_poll else None)
        since = (datetime.now(timezone.utc) - timedelta(hours=12)).isoformat(timespec="seconds")
        credits = _rows(db, "SELECT substr(started_utc, 12, 2) AS hour_utc, SUM(credits_charged) AS credits, "
                            "COUNT(*) AS calls FROM polls WHERE started_utc >= ? GROUP BY substr(started_utc, 1, 13) "
                            "ORDER BY substr(started_utc, 1, 13)", (since,))
        marks = ",".join("?" * len(pl.MY_TICKERS))
        picks = _rows(db, f"SELECT trade_time_et AS time, ticker, contract, put_call, fill_price AS fill, size, "
                          f"score, iv, hermes_expected_gain AS hermes, qwen_expected_gain AS qwen, "
                          f"pred_max_gain AS model, pinged_utc IS NOT NULL AS pinged, max_gain_pct AS best, "
                          f"close_pct AS close FROM picks WHERE trade_date = ? AND ticker IN ({marks}) "
                          f"ORDER BY trade_time_et DESC", (today, *sorted(pl.MY_TICKERS)))
        others = db.execute(f"SELECT COUNT(*) FROM picks WHERE trade_date = ? AND ticker NOT IN ({marks})",
                            (today, *sorted(pl.MY_TICKERS))).fetchone()[0]
        pings = _rows(db, "SELECT trade_date AS day, trade_time_et AS time, ticker, contract, fill_price AS fill, "
                          "pred_max_gain AS model, max_gain_pct AS best, close_pct AS close FROM picks "
                          "WHERE pinged_utc IS NOT NULL ORDER BY pinged_utc DESC LIMIT 12")
        runs = _rows(db, "SELECT run_utc, model_kind AS kind, n_rows AS n, cv_spearman AS rank, "
                         "cv_hit_rate AS hit, ping_threshold AS bar, base_rate_hit AS base, leaderboard "
                         "FROM model_runs WHERE status = 'trained' ORDER BY id")
        arena = None
        if runs:
            last = runs[-1]
            arena = {"latest": {k: v for k, v in last.items() if k != "leaderboard"},
                     "leaderboard": json.loads(last["leaderboard"] or "[]")[:8],
                     "history": [{k: r[k] for k in ("run_utc", "kind", "n", "rank", "hit", "base")} for r in runs]}
        check_run = db.execute("SELECT MAX(run_utc) FROM integrity_checks WHERE check_name NOT LIKE 'ai_%'").fetchone()[0]
        checks = _rows(db, "SELECT check_name AS name, severity, message FROM integrity_checks WHERE run_utc = ? "
                           "AND check_name NOT LIKE 'ai_%' ORDER BY severity DESC, check_name", (check_run,)) if check_run else []
        audit = _rows(db, "SELECT run_utc, check_name AS kind, message FROM integrity_checks "
                          "WHERE check_name LIKE 'ai_%' ORDER BY id DESC LIMIT 8")
        try:
            activity = _rows(db, "SELECT bot, MAX(run_utc) AS last_run FROM bot_reports GROUP BY bot")
        except sqlite3.OperationalError:
            activity = []
        log_tail = []
        log = HERE / "logger_console.log"
        if log.exists():
            with open(log, "rb") as f:
                f.seek(max(0, log.stat().st_size - 6000))
                raw = f.read()
            # Windows PowerShell's Tee-Object writes UTF-16; strip its zero bytes and BOMs
            text = raw.replace(b"\x00", b"").decode("utf-8", errors="replace")
            log_tail = [ln.replace("﻿", "").replace("�", "") for ln in text.splitlines()
                        if ln.strip()][-18:]
        return {
            "now_et": et.strftime("%a %b %d %H:%M:%S ET"),
            "logger": {"last_poll_utc": last_poll, "age_sec": age},
            "credits": credits, "picks_today": picks, "others_today": others, "pings": pings,
            "arena": arena, "simulator": _latest_report(db, "simulator"), "analyst": _latest_report(db, "analyst"),
            "judges": _latest_report(db, "judges"), "checks": checks, "check_run": check_run, "audit": audit,
            "activity": activity, "log": log_tail,
        }
    finally:
        db.close()


PAGE = r"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Flow Lab</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<style>
:root{--bg:#0f1218;--card:#171c25;--line:#262e3b;--text:#e6e9ef;--muted:#8b95a7;--good:#3fb950;--bad:#f85149;--warn:#d29922;--accent:#58a6ff}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 system-ui,Segoe UI,sans-serif}
header{display:flex;justify-content:space-between;align-items:center;padding:14px 20px;border-bottom:1px solid var(--line)}
h1{font-size:18px;margin:0}h2{font-size:14px;margin:0 0 10px;color:var(--muted);font-weight:600;letter-spacing:.02em}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(420px,1fr));gap:14px;padding:14px 20px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px;overflow:auto}
.wide{grid-column:1/-1}table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:4px 6px;border-bottom:1px solid var(--line);white-space:nowrap}th{color:var(--muted);font-weight:500}
.pill{display:inline-block;padding:1px 8px;border-radius:99px;font-size:12px;font-weight:600}
.ok{background:#1f3a26;color:var(--good)}.warn{background:#3a2f12;color:var(--warn)}.fail{background:#401a1a;color:var(--bad)}
.pos{color:var(--good)}.neg{color:var(--bad)}.muted{color:var(--muted)}
pre{margin:0;font:12px/1.4 ui-monospace,Consolas,monospace;color:var(--muted);white-space:pre-wrap}
.stat{font-size:22px;font-weight:700}.row{display:flex;gap:24px;flex-wrap:wrap}
@media (max-width:520px){.grid{grid-template-columns:1fr;padding:10px}header{padding:10px}}
</style></head><body>
<header><h1>🧪 Flow Lab</h1><div class="muted" id="now"></div></header>
<div class="grid">
 <div class="card"><h2>🛰️ SCOUT - capture & credits</h2><div class="row" id="scout"></div><canvas id="creditChart" height="120"></canvas></div>
 <div class="card"><h2>🛡️ AUDITOR - data integrity</h2><div id="auditor"></div></div>
 <div class="card wide"><h2>📣 HERALD - today's picks on your tickers</h2><div id="picks"></div></div>
 <div class="card"><h2>🏆 ARENA - model contest</h2><div id="arena"></div><canvas id="arenaChart" height="130"></canvas></div>
 <div class="card"><h2>💰 SIMULATOR - backtest (exit at close, cumulative return)</h2><canvas id="simChart" height="150"></canvas><div id="sim"></div></div>
 <div class="card"><h2>⚖️ JUDGES - Hermes & Qwen</h2><div id="judges"></div></div>
 <div class="card"><h2>🔬 ANALYST - what predicts gains</h2><div id="analyst"></div></div>
 <div class="card"><h2>📣 HERALD - recent pings</h2><div id="pings"></div></div>
 <div class="card"><h2>🖥️ Logger output (live)</h2><pre id="log"></pre></div>
</div>
<script>
const pct=v=>v==null?'–':`<span class="${v>=0?'pos':'neg'}">${(v>=0?'+':'')+(v*100).toFixed(0)}%</span>`;
const esc=s=>String(s??'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
const table=(cols,rows)=>rows.length?`<table><tr>${cols.map(c=>`<th>${c[0]}</th>`).join('')}</tr>${rows.map(r=>`<tr>${cols.map(c=>`<td>${c[1](r)}</td>`).join('')}</tr>`).join('')}</table>`:'<div class="muted">nothing yet</div>';
const charts={};
function chart(id,cfg){if(charts[id]){charts[id].data=cfg.data;charts[id].update('none');return}
 Chart.defaults.color='#8b95a7';Chart.defaults.borderColor='#262e3b';charts[id]=new Chart(document.getElementById(id),cfg)}
async function refresh(){
 let s;try{s=await (await fetch('/api/state')).json()}catch(e){document.getElementById('now').textContent='dashboard offline';return}
 document.getElementById('now').textContent=s.now_et;
 const age=s.logger.age_sec, alive=age!=null&&age<900;
 document.getElementById('scout').innerHTML=`<div><div class="stat">${alive?'🟢':'⚪'} ${age==null?'–':Math.round(age/60)+' min'}</div><div class="muted">since last Trade Echo call</div></div>
  <div><div class="stat">${s.credits.length?s.credits[s.credits.length-1].credits:0}/150</div><div class="muted">credits this hour (UTC ${s.credits.length?s.credits[s.credits.length-1].hour_utc:''})</div></div>
  <div><div class="stat">${s.picks_today.length} / ${s.picks_today.length+s.others_today}</div><div class="muted">picks today: yours / all</div></div>`;
 chart('creditChart',{type:'bar',data:{labels:s.credits.map(c=>c.hour_utc+'h'),datasets:[{label:'credits / hour (UTC)',data:s.credits.map(c=>c.credits),backgroundColor:'#58a6ff'}]},options:{scales:{y:{max:100,beginAtZero:true}},plugins:{legend:{display:false}}}});
 const sev={OK:'ok',WARN:'warn',FAIL:'fail'};
 const bad=s.checks.filter(c=>c.severity!='OK');
 document.getElementById('auditor').innerHTML=`<div class="muted">last run ${esc(s.check_run)} - ${s.checks.length-bad.length}/${s.checks.length} passed</div>`+
  table([['',c=>`<span class="pill ${sev[c.severity]}">${c.severity}</span>`],['check',c=>esc(c.name)],['detail',c=>esc(c.message)]],bad.length?bad:s.checks.slice(0,6))+
  (s.audit.length?'<h2 style="margin-top:10px">🤖 local AI audit</h2>'+table([['when',a=>esc(a.run_utc.slice(5,16))],['type',a=>a.kind=='ai_confirmed'?'🛑 confirmed':'❔ look'],['finding',a=>esc(a.message)]],s.audit):'');
 document.getElementById('picks').innerHTML=table([['time',p=>p.time],['contract',p=>`<b>${esc(p.ticker)}</b> ${esc(p.contract)}`],['fill',p=>'$'+p.fill],['per contract',p=>'$'+Math.round(p.fill*100).toLocaleString()],['size',p=>p.size??'–'],['score',p=>p.score],['IV',p=>p.iv?Math.round(p.iv*100)+'%':'–'],['Hermes',p=>pct(p.hermes)],['Qwen',p=>pct(p.qwen)],['model',p=>pct(p.model)],['pinged',p=>p.pinged?'📣':''],['best',p=>pct(p.best)],['close',p=>pct(p.close)]],s.picks_today);
 if(s.arena){const a=s.arena.latest;
  document.getElementById('arena').innerHTML=`<div class="row"><div><div class="stat">${esc(a.kind)}</div><div class="muted">tonight's winner (${a.n} picks)</div></div><div><div class="stat">${a.rank==null?'–':a.rank.toFixed(2)}</div><div class="muted">replay rank agreement</div></div><div><div class="stat">${a.hit==null?'–':Math.round(a.hit*100)+'%'} <span class="muted" style="font-size:13px">vs ${Math.round((a.base||0)*100)}% all</span></div><div class="muted">pings reaching +30%</div></div></div>`+
   table([['method',r=>esc(r.kind)],['rank',r=>r.spearman==null?'–':r.spearman.toFixed(2)],['ping hit',r=>r.hit==null?'–':Math.round(r.hit*100)+'% of '+r.n_pings],['bar',r=>'+'+Math.round(r.threshold*100)+'%']],s.arena.leaderboard);
  chart('arenaChart',{type:'line',data:{labels:s.arena.history.map(h=>h.run_utc.slice(5,16)),datasets:[{label:'winner rank agreement',data:s.arena.history.map(h=>h.rank),borderColor:'#3fb950'},{label:'training picks / 100',data:s.arena.history.map(h=>h.n/100),borderColor:'#58a6ff'}]},options:{plugins:{legend:{position:'bottom'}}}});}
 if(s.simulator){const d=s.simulator.data,cols=['#58a6ff','#3fb950','#d29922','#f85149'];
  chart('simChart',{type:'line',data:{datasets:Object.entries(d.curves||{}).map(([k,v],i)=>({label:k,data:v.map((p,j)=>({x:j+1,y:p[1]*100})),borderColor:cols[i%4],pointRadius:0}))},options:{parsing:false,scales:{x:{type:'linear',title:{display:true,text:'trade #'}},y:{title:{display:true,text:'cumulative % (sum)'}}},plugins:{legend:{position:'bottom'}}}});
  const rows=[];for(const [g,ex] of Object.entries(d.groups||{})){if(!ex)continue;for(const [e,st] of Object.entries(ex))rows.push({g,e,...st})}
  document.getElementById('sim').innerHTML=table([['group',r=>esc(r.g)],['exit',r=>esc(r.e)],['n',r=>r.n],['won',r=>Math.round(r.win_rate*100)+'%'],['avg',r=>pct(r.avg)],['$/contract',r=>'$'+Math.round(r.avg_dollars_1_contract)]],rows);}
 if(s.judges){document.getElementById('judges').innerHTML=table([['judge',j=>j[0]],['n',j=>j[1].n],['rank (all)',j=>j[1].rank_all.toFixed(2)],['rank (3 days)',j=>j[1].rank_recent==null?'–':j[1].rank_recent.toFixed(2)],['avg est',j=>pct(j[1].avg_est)],['avg real',j=>pct(j[1].avg_act)],['take hit',j=>j[1].take_hit==null?'–':Math.round(j[1].take_hit*100)+'%']],Object.entries(s.judges.data))}
 if(s.analyst&&s.analyst.data.feature_corr){document.getElementById('analyst').innerHTML=table([['input',f=>esc(f[0])],['rank agreement',f=>`<span class="${f[1][0]>=0?'pos':'neg'}">${f[1][0].toFixed(2)}</span>`],['n',f=>f[1][1]]],Object.entries(s.analyst.data.feature_corr).slice(0,10))}
 document.getElementById('pings').innerHTML=table([['day',p=>p.day.slice(5)],['time',p=>p.time],['contract',p=>`<b>${esc(p.ticker)}</b> ${esc(p.contract)}`],['model',p=>pct(p.model)],['best',p=>pct(p.best)],['close',p=>pct(p.close)]],s.pings);
 document.getElementById('log').textContent=s.log.join('\n');
}
refresh();setInterval(refresh,15000);
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            if self.path.startswith("/api/state"):
                body, ctype = json.dumps(state(), default=str).encode("utf-8"), "application/json"
            elif self.path in ("/", "/index.html"):
                body, ctype = PAGE.encode("utf-8"), "text/html; charset=utf-8"
            else:
                self.send_error(404)
                return
        except Exception as e:  # never let a dashboard error touch the logger
            body, ctype = json.dumps({"error": str(e)}).encode("utf-8"), "application/json"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # keep the logger console clean
        pass


def start_in_background(port=PORT):
    """Serve the dashboard on 127.0.0.1 only (not visible to other devices)."""
    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError as e:
        print(f"Dashboard not started (port {port} busy?): {e}")
        return None
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"Dashboard running at http://localhost:{port}")
    return server


if __name__ == "__main__":
    print(f"Dashboard at http://localhost:{PORT} (Ctrl+C to stop)")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
