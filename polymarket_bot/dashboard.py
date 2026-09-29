"""Minimal localhost dashboard. Toggle is the only control; no order placement."""

from __future__ import annotations

import json
import os
import secrets
import time
from datetime import datetime, timezone
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from polymarket_bot.config import AppConfig
from polymarket_bot.logging_utils import json_default
from polymarket_bot.pnl import env_name, history_path, load_history, snapshot_from_state, summarize
from polymarket_bot.trading import set_trading_enabled, trading_status

PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<title>PredictiveBot</title>
<style>
body { font-family: ui-sans-serif, system-ui, sans-serif; margin: 1rem 1.25rem; background:#0f1419; color:#e7ecf1; }
h1 { font-size: 1.2rem; margin: 0 0 .5rem; }
h2 { font-size: .95rem; margin: 1.2rem 0 .4rem; border-bottom: 1px solid #2a3440; padding-bottom: .2rem; }
.row { display:flex; flex-wrap:wrap; gap: .75rem; align-items:center; }
.card { background:#1a222c; padding:.6rem .75rem; border-radius:8px; min-width:10rem; }
.live { background:#4a1515; color:#ffd4d4; font-weight:700; padding:.35rem .6rem; border-radius:4px; letter-spacing:.02em; }
.demo { background:#153a24; color:#c6f5d5; padding:.25rem .5rem; border-radius:4px; }
.switch { position:relative; display:inline-block; width:46px; height:26px; vertical-align:middle; }
.switch input { opacity:0; width:0; height:0; }
.slider { position:absolute; cursor:pointer; inset:0; background:#4b5563; border-radius:26px; transition:.2s; }
.slider:before { content:""; position:absolute; height:20px; width:20px; left:3px; bottom:3px; background:white; border-radius:50%; transition:.2s; }
.switch input:checked + .slider { background:#2d6cdf; }
.switch input:checked + .slider:before { transform:translateX(20px); }
table { width:100%; border-collapse:collapse; font-size:.85rem; }
td,th { text-align:left; padding:.2rem .35rem; border-bottom:1px solid #2a3440; vertical-align:top; }
.muted { color:#8b98a5; font-size:.8rem; }
button { background:#2d6cdf; color:white; border:0; padding:.35rem .7rem; border-radius:6px; cursor:pointer; }
button.off { background:#8a2d2d; }
#err { color:#ff8a8a; }
svg { background:#12181f; border-radius:6px; }
</style>
</head>
<body>
<h1>PredictiveBot</h1>
<div class="row">
  <div id="env"></div>
  <label class="switch" title="Trading on/off">
    <input type="checkbox" id="toggle"/>
    <span class="slider"></span>
  </label>
  <span>Trading</span>
  <span id="toggleReason" class="muted"></span>
  <span class="muted" id="updated"></span>
</div>
<p id="err"></p>
<h2>Account</h2>
<div class="row" id="account"></div>
<h2>P&amp;L (net of fees / rebates)</h2>
<div class="row" id="pnl"></div>
<div id="chart"></div>
<div id="pnlBreak"></div>
<h2>Exposure</h2>
<div id="exposure"></div>
<h2>Positions</h2>
<div id="positions"></div>
<h2>Resting orders</h2>
<div id="orders"></div>
<h2>Recent fills</h2>
<div id="fills"></div>
<h2>Watched markets</h2>
<div id="markets"></div>
<h2>Strategy</h2>
<div id="strategy"></div>
<h2>Alerts / errors</h2>
<div id="alerts"></div>
<script>
const refreshMs = %REFRESH_MS%;
let toggling = false;
function money(v){ if(v===undefined||v===null||v==='') return '—'; const n=Number(v); return isNaN(n)?String(v):'$'+n.toFixed(2); }
function pct(v){ if(v===undefined||v===null||v==='') return '—'; const n=Number(v); return isNaN(n)?String(v):(n<=1? (n*100).toFixed(1)+'%' : n.toFixed(1)+'%'); }
function el(id, html){ document.getElementById(id).innerHTML = html; }
function table(rows, headers){
  if(!rows || !rows.length) return '<p class="muted">(none)</p>';
  const h = '<tr>'+headers.map(x=>'<th>'+x+'</th>').join('')+'</tr>';
  const b = rows.map(r=>'<tr>'+r.map(c=>'<td>'+c+'</td>').join('')+'</tr>').join('');
  return '<table>'+h+b+'</table>';
}
function spark(points){
  if(!points || points.length<2) return '<p class="muted">No equity history yet.</p>';
  const xs = points.map(p=>Number(p.equity));
  const min=Math.min(...xs), max=Math.max(...xs), w=520, h=90, p=6;
  const span = (max-min)||1;
  const d = points.map((pt,i)=>{
    const x = p + (w-2*p)*i/(points.length-1);
    const y = h-p - (h-2*p)*((Number(pt.equity)-min)/span);
    return x+','+y;
  }).join(' ');
  return '<svg width="'+w+'" height="'+h+'" viewBox="0 0 '+w+' '+h+'"><polyline fill="none" stroke="#5aa7ff" stroke-width="2" points="'+d+'"/></svg>';
}
async function load(){
  if (toggling) return;
  const r = await fetch('/api/snapshot');
  const s = await r.json();
  el('env', '<span class="demo">'+(s.environment||'PAPER')+'</span> <span class="live">LIVE TRADING DISABLED</span>');
  if (s.exchange && s.exchange.error) document.getElementById('err').textContent = 'Exchange: '+s.exchange.error;
  document.getElementById('toggle').checked = s.trading.effective === 'on';
  el('toggleReason', s.trading.effective === 'on' ? 'on' : 'off');
  el('updated', 'Updated '+ (s.updated_at || ''));
  el('account', '<div class="card">Value<br><b>'+money(s.account_value)+'</b></div><div class="card">Cash<br><b>'+money(s.cash)+'</b></div><div class="card">P&amp;L today<br><b>'+money(s.pnl && s.pnl.today && s.pnl.today.net_pnl)+'</b></div><div class="card">P&amp;L total<br><b>'+money(s.pnl && s.pnl.all_time && s.pnl.all_time.net_pnl)+'</b></div>');
  const p = s.pnl || {};
  function box(title, w){
    w = w || {};
    return '<div class="card">'+title+'<br>net '+money(w.net_pnl)+'<br><span class="muted">real '+money(w.realized)+' · unreal '+money(w.unrealized)+' · fees '+money(w.fees)+'</span></div>';
  }
  el('pnl', box('Today', p.today)+box('Last 7 days', p.last_7d)+box('All time', p.all_time)+'<div class="card">Drawdown / W-L<br>'+money(p.all_time && p.all_time.max_drawdown)+'<br><span class="muted">'+(p.all_time && p.all_time.wins || 0)+'W / '+(p.all_time && p.all_time.losses || 0)+'L</span></div><div class="card">Fees (signed)<br><b>'+money(p.all_time && p.all_time.fees)+'</b></div>');
  el('chart', spark(p.equity_points||[])+'<p class="muted">History file ('+(p.env||'')+'): '+(p.path||'')+' — demo/live/paper never mix</p>');
  const stratRows = Object.entries(p.strategies||{}).map(([k,v])=>[k, money(v.net_pnl), money(v.realized), money(v.unrealized), money(v.fees)]);
  const mktRows = Object.entries(p.markets||{}).map(([k,v])=>[k, v.strategy||'', v.qty, money(v.realized), money(v.unrealized)]);
  el('pnlBreak', '<p class="muted">Per strategy</p>'+table(stratRows,['strategy','net','realized','unreal','fees'])+'<p class="muted">Per market</p>'+table(mktRows,['market','strategy','qty','realized','unreal']));
  const exp = s.exposure || {};
  el('exposure', 'Account exposure <b>'+(exp.pct_display||pct(exp.pct))+'</b> of cap '+(exp.cap_display||'')+'');
  el('positions', table((s.positions||[]).map(p=>[p.market, p.qty, p.avg_price]), ['market','qty','avg']));
  el('orders', table((s.resting_orders||[]).map(o=>[o.market||o.ticker, o.side, o.price, o.qty||o.count]), ['market','side','price','qty']));
  el('fills', table((s.fills||[]).slice(0,20).map(f=>[f.market||f.ticker, f.side, f.price||f.yes_price_dollars, f.qty||f.count, f.strategy||'']), ['market','side','price','qty','strategy']));
  el('markets', table((s.markets||[]).map(m=>{
    const why = Object.entries(m.components||{}).map(([k,v])=>k+'='+(Number(v)<=1?(Number(v)*100).toFixed(0)+'%':v)).join(', ');
    const over = m.over_threshold ? ' OVER' : '';
    return [m.slug, (m.score_display||'')+over, why];
  }), ['market','risk','components']));
  el('strategy', '<pre>'+JSON.stringify({...(s.strategy||{}), exchange:s.exchange||null},null,2)+'</pre>');
  el('alerts', table((s.alerts||[]).map(a=>[a.ts||'', a.action||'', a.error||a.reason||JSON.stringify(a)]), ['ts','action','detail']));
}
document.getElementById('toggle').addEventListener('change', async (ev)=>{
  if (toggling) return;
  const want = ev.target.checked;
  toggling = true;
  try {
    if(!confirm('Turn trading '+(want?'ON':'OFF')+'? This writes the same state file as pmbot trading.')){
      ev.target.checked = !want;
      return;
    }
    const r = await fetch('/api/trading', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({enabled: want, confirm: true})});
    if(!r.ok){
      document.getElementById('err').textContent = await r.text();
      ev.target.checked = !want;
      return;
    }
    toggling = false;
    await load();
  } finally {
    toggling = false;
  }
});
load();
setInterval(load, refreshMs);
</script>
</body>
</html>
"""


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text() or "{}")
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _tail_jsonl(path: Path, limit: int = 40) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    lines = path.read_text().splitlines()[-limit:]
    out: list[dict[str, Any]] = []
    for line in lines:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            out.append(row)
    return out


def _dec(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


_EXCHANGE_CACHE: dict[str, Any] = {"ts": 0.0, "data": {"hours": None, "quotes": {}, "error": None}}


def _read_exchange(config: AppConfig, slugs: list[str]) -> dict[str, Any]:
    """Public, read-only venue calls. Never places or cancels orders."""
    now = time.monotonic()
    if now - float(_EXCHANGE_CACHE["ts"] or 0) < 20:
        return dict(_EXCHANGE_CACHE["data"])
    out: dict[str, Any] = {"hours": None, "quotes": {}, "error": None}
    try:
        from polymarket_bot.exchanges.factory import build_client

        client = build_client(config, source="live", exchange=config.exchange)
        try:
            getter = getattr(client, "trading_hours", None)
            if callable(getter):
                out["hours"] = getter()
            last_fn = getattr(client, "last_trade", None)
            for slug in slugs[:6]:
                if not slug or not callable(last_fn):
                    continue
                try:
                    last = last_fn(slug)
                except Exception:
                    continue
                if last is not None:
                    out["quotes"][slug] = {"last_trade": last}
        finally:
            client.close()
    except Exception as exc:
        out["error"] = str(exc)
    _EXCHANGE_CACHE["ts"] = now
    _EXCHANGE_CACHE["data"] = out
    return out


def _pick_state(config: AppConfig) -> tuple[dict[str, Any], str]:
    paper = _read_json(config.logging.state_path)
    demo = _read_json(Path("logs/demo_state.json"))
    if demo and (not paper or demo.get("demo")):
        if demo.get("ts") or demo.get("ending_cash") or demo.get("quotes_placed") is not None:
            if not paper or bool(demo.get("demo")):
                return demo, "demo"
    if paper:
        return paper, "paper"
    if demo:
        return demo, "demo"
    return {}, "paper"


def build_snapshot(
    config: AppConfig,
    *,
    now: datetime | None = None,
    include_exchange: bool = True,
) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    state, source = _pick_state(config)
    live_enabled = bool(config.live_trading_enabled)
    environment = "LIVE" if live_enabled or bool(state.get("live")) else ("DEMO" if state.get("demo") or source == "demo" else "PAPER")
    risk = state.get("account_risk") or {}
    if not risk and isinstance(state.get("maker"), dict):
        risk = (state.get("maker") or {}).get("account_risk") or {}
    maker = state.get("maker") or {}
    positions = []
    for slug, pos in (maker.get("positions") or {}).items():
        positions.append({"market": slug, "qty": pos.get("qty"), "avg_price": pos.get("avg_price")})
    if not positions:
        raw_pos = state.get("positions") or {}
        rows = raw_pos.get("market_positions") if isinstance(raw_pos, dict) else raw_pos
        if isinstance(rows, list):
            for row in rows:
                positions.append(
                    {
                        "market": row.get("ticker") or row.get("market_ticker"),
                        "qty": row.get("position") or row.get("qty"),
                        "avg_price": row.get("average_price") or row.get("avg_price"),
                    }
                )
    fills = list(maker.get("fills") or state.get("fills") or [])
    scores = state.get("market_risk") or {}
    cap = _dec(config.paper.risk.max_market_risk_score)
    markets = []
    for slug, payload in scores.items():
        comps = (payload or {}).get("components") or {}
        score = _dec((payload or {}).get("score"))
        markets.append(
            {
                "slug": slug,
                "score": score,
                "score_display": (payload or {}).get("score_display"),
                "components": comps,
                "over_threshold": bool(score is not None and cap is not None and score >= cap),
            }
        )
    alerts = []
    for path in (config.logging.jsonl_path, Path("logs/demo_decisions.jsonl")):
        for row in _tail_jsonl(Path(path), 80):
            action = str(row.get("action") or "")
            if action.startswith("ALERT") or row.get("alert") or "error" in action:
                alerts.append(row)
    alerts = alerts[-15:]
    env = env_name(demo=bool(state.get("demo") or source == "demo"), live=False)
    rows = load_history(config.dashboard.pnl_path, env)
    if not rows and state:
        rows = [snapshot_from_state(state, now=now)]
    pnl = summarize(rows, now=now)
    pnl["env"] = env
    pnl["path"] = str(history_path(config.dashboard.pnl_path, env))
    equity = _dec((maker.get("equity") if maker else None) or state.get("ending_cash") or state.get("starting_cash"))
    cash = _dec(maker.get("cash") or state.get("ending_cash") or state.get("starting_cash"))
    exchange = (
        _read_exchange(config, [m["slug"] for m in markets])
        if include_exchange
        else {"hours": None, "quotes": {}, "error": None}
    )
    return {
        "updated_at": now.isoformat(),
        "environment": environment,
        "live_trading_enabled": False,
        "exchange": exchange,
        "source": source,
        "trading": trading_status(config),
        "account_value": equity,
        "cash": cash,
        "exposure": {
            "pct": risk.get("pct"),
            "pct_display": risk.get("pct_display"),
            "cap": risk.get("cap"),
            "cap_display": risk.get("cap_display") or state.get("account_risk_cap"),
        },
        "positions": positions,
        "resting_orders": list(state.get("resting_leftover") or []),
        "fills": fills[-20:],
        "markets": markets,
        "market_risk_cap": state.get("market_risk_cap"),
        "strategy": {
            "trading": state.get("trading"),
            "ticks": state.get("ticks"),
            "quotes_placed": state.get("quotes_placed"),
            "maker_fills": maker.get("fill_count"),
            "near_fills": (state.get("near_resolution") or {}).get("fill_count"),
        },
        "alerts": alerts,
        "pnl": pnl,
    }


def _localhost(host: str) -> bool:
    return host in {"127.0.0.1", "localhost", "::1", "0:0:0:0:0:0:0:1"}


def _token_ok(handler: BaseHTTPRequestHandler, required: str | None) -> bool:
    if not required:
        return True
    header = handler.headers.get("Authorization") or ""
    if header.startswith("Bearer ") and secrets.compare_digest(header[7:], required):
        return True
    parsed = urlparse(handler.path)
    qs = parse_qs(parsed.query)
    got = (qs.get("token") or [None])[0]
    return bool(got and secrets.compare_digest(got, required))


def make_handler(config: AppConfig) -> type[BaseHTTPRequestHandler]:
    token = os.environ.get("DASHBOARD_TOKEN") or None
    require = token if not _localhost(config.dashboard.host) else None

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            return

        def _send(self, code: int, body: bytes, content_type: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            if require and not _token_ok(self, require):
                self._send(401, b"DASHBOARD_TOKEN required", "text/plain")
                return
            path = urlparse(self.path).path
            if path in {"/", "/index.html"}:
                html = PAGE.replace("%REFRESH_MS%", str(int(config.dashboard.refresh_seconds * 1000)))
                self._send(200, html.encode(), "text/html; charset=utf-8")
                return
            if path == "/api/snapshot":
                payload = json.dumps(build_snapshot(config), default=json_default).encode()
                self._send(200, payload, "application/json")
                return
            self._send(404, b"not found", "text/plain")

        def do_POST(self) -> None:  # noqa: N802
            if require and not _token_ok(self, require):
                self._send(401, b"DASHBOARD_TOKEN required", "text/plain")
                return
            path = urlparse(self.path).path
            if path != "/api/trading":
                self._send(404, b"not found", "text/plain")
                return
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                body = json.loads(raw.decode() or "{}")
            except json.JSONDecodeError:
                self._send(400, b"invalid json", "text/plain")
                return
            if not body.get("confirm"):
                self._send(400, b"confirm required", "text/plain")
                return
            enabled = bool(body.get("enabled"))
            set_trading_enabled(config.trading.toggle_path, enabled)
            payload = json.dumps(trading_status(config)).encode()
            self._send(200, payload, "application/json")

    return Handler


def serve_dashboard(config: AppConfig) -> None:
    handler = make_handler(config)
    server = ThreadingHTTPServer((config.dashboard.host, config.dashboard.port), handler)
    print(
        f"Dashboard http://{config.dashboard.host}:{config.dashboard.port} "
        f"(toggle only; no order placement). Environment bind={config.dashboard.host}"
    )
    if not _localhost(config.dashboard.host) and not os.environ.get("DASHBOARD_TOKEN"):
        print("WARNING: bound beyond localhost without DASHBOARD_TOKEN")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()
