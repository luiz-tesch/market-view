"""
Polymarket Paper Bot v3.1 — Mercados Reais + Simulação Fiel
Run: python app.py
Open: http://localhost:5001

Correções v3.1:
- warmup_remaining surfacado do snapshot e exibido no painel
- Dashboard totalmente redesenhado: minimalista, profissional, painéis ocultáveis
"""
from __future__ import annotations
import json, queue, threading, time
from flask import Flask, Response, jsonify, render_template_string, request

from src.signals.engine import PriceBuffer, compute_signal
from src.feed.polymarket import (
    fetch_gamma_markets,
    fetch_short_term_markets,
    enrich_prices_background,
    ClobFeed, MockClobFeed,
    BinanceFeed, MockBinanceFeed,
    PolyMarket,
)
try:
    from src.data.collector import DataCollector
    _collector_available = True
except ImportError:
    _collector_available = False
    DataCollector = None  # type: ignore

from src.execution.simulator import TradingSimulator

app = Flask(__name__)

# ── Globals ────────────────────────────────────────────────────────────────────
SYMBOLS = ["BTC", "ETH", "SOL", "BNB", "MATIC", "DOGE", "XRP"]
price_buffers: dict[str, PriceBuffer] = {s: PriceBuffer(maxlen=400) for s in SYMBOLS}
# ── Fase 1: DataCollector ────────────────────────────────────────────────
collector = DataCollector() if _collector_available else None

simulator    = TradingSimulator(price_buffers, collector=collector)
all_markets:   list[PolyMarket] = []
short_markets: list[PolyMarket] = []
binance_feed  = None
clob_feed     = None
IS_LIVE_BIN   = False
IS_LIVE_CLOB  = False

_sse_clients: list[queue.Queue] = []
_sse_lock = threading.Lock()


# ── Callbacks ──────────────────────────────────────────────────────────────────

def on_binance(sym: str, price: float, qty: float):
    if collector:
        try:
            collector.record_tick(sym, time.time(), price, qty)
        except Exception:
            pass
    price_buffers[sym].push(price, qty)
    _sse("tick", {"sym": sym, "price": price, "ts": time.time()})


def on_clob(token_id: str, bid: float, ask: float):
    simulator.on_price_update(token_id, bid, ask)
    mid = round((bid + ask) / 2, 4)
    _sse("clob", {"tid": token_id, "bid": round(bid, 4), "ask": round(ask, 4), "mid": mid})


# ── SSE ────────────────────────────────────────────────────────────────────────

def _sse(event: str, data: dict):
    msg = f"event: {event}\ndata: {json.dumps(data)}\n\n"
    with _sse_lock:
        dead = []
        for q in _sse_clients:
            try:
                q.put_nowait(msg)
            except queue.Full:
                dead.append(q)
        for q in dead:
            _sse_clients.remove(q)


@app.route("/stream")
def stream():
    q: queue.Queue = queue.Queue(maxsize=150)
    with _sse_lock:
        _sse_clients.append(q)

    def gen():
        yield f"event: snapshot\ndata: {json.dumps(_snap())}\n\n"
        try:
            while True:
                try:
                    yield q.get(timeout=25)
                except queue.Empty:
                    yield ": ping\n\n"
        except GeneratorExit:
            with _sse_lock:
                if q in _sse_clients:
                    _sse_clients.remove(q)

    return Response(gen(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ── API ────────────────────────────────────────────────────────────────────────


@app.route("/api/stats/edge")
def api_stats_edge():
    """
    Fase 1: retorna win rate por (symbol, action) para trades com resolução real.
    Usado para acompanhar se o sinal está acumulando edge validado.
    """
    if not collector:
        return jsonify({"error": "DataCollector não disponível"}), 503
    return jsonify({
        "edge_by_symbol":   collector.get_signal_edge_by_symbol(),
        "resolution_stats": collector.get_resolution_stats(),
    })

@app.route("/api/snapshot")
def api_snap():
    return jsonify(_snap())

@app.route("/api/sim/toggle", methods=["POST"])
def sim_toggle():
    if simulator._running:
        simulator.stop()
    else:
        simulator.start()
    return jsonify({"running": simulator._running})

@app.route("/api/sim/reset", methods=["POST"])
def sim_reset():
    simulator.reset()
    return jsonify({"ok": True})

@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    threading.Thread(target=_load_markets, daemon=True).start()
    return jsonify({"ok": True})


def _snap() -> dict:
    sim     = simulator.snapshot()
    signals = {}
    bufinfo = {}

    for sym, buf in price_buffers.items():
        p = buf.latest_price()
        bufinfo[sym] = {
            "price": p, "ticks": len(buf.ticks),
            "candles": len(buf.candles_1m),
        }
        if p and len(buf.ticks) > 20:
            try:
                sig = compute_signal(sym, buf, 0.5, 10)
                signals[sym] = {
                    "direction":    sig.direction,
                    "strength":     round(sig.strength, 3),
                    "confidence":   sig.confidence,
                    "edge":         round(sig.edge, 4),
                    "momentum_1m":  round(sig.momentum_1m, 3),
                    "momentum_5m":  round(sig.momentum_5m, 3),
                    "rsi":          round(sig.rsi, 1),
                    "vwap_dev":     round(sig.vwap_dev, 3),
                    "bb_position":  round(sig.bb_position, 3),
                    "reasons":      sig.reasons,
                    "market_type":  sig.market_type,
                }
            except Exception:
                pass

    lat  = clob_feed.avg_latency_ms() if clob_feed and hasattr(clob_feed, "avg_latency_ms") else 0.0
    msgs = clob_feed.msg_count if clob_feed and hasattr(clob_feed, "msg_count") else 0

    return {
        **sim,
        "signals":          signals,
        "buffers":          bufinfo,
        "all_markets":      [m.to_dict() for m in all_markets],
        "short_markets":    [m.to_dict() for m in short_markets],
        "feed": {
            "binance_live":  IS_LIVE_BIN,
            "clob_live":     IS_LIVE_CLOB,
            "clob_lat_ms":   round(lat, 1),
            "clob_msgs":     msgs,
            "total_markets": len(all_markets),
            "short_markets": len(short_markets),
        },
    }


# ── Market loading ─────────────────────────────────────────────────────────────

def _load_markets():
    global all_markets, short_markets

    print("[App] Buscando mercados do Polymarket...")
    markets = fetch_gamma_markets(limit=400, min_liquidity=50, max_days=90)

    if not markets:
        print("[App] API indisponível — usando mercados demo")
        markets = _demo()

    enrich_prices_background(markets)

    all_markets   = markets
    short_markets = [m for m in markets if m.is_short_term()]
    print(f"[App] {len(all_markets)} mercados totais | {len(short_markets)} short-term")

    for m in short_markets:
      if m.symbol in price_buffers:
          if collector:
              try:
                  collector.record_market(m)
              except Exception:
                  pass
          from src.signals.engine import detect_market_type
          simulator.register_market(
              token_id     = m.token_id,
              question     = m.question,
              slug         = m.slug,
              symbol       = m.symbol,
              horizon_min  = max(3.0, m.mins_left()),
              yes_price    = m.yes_price,
              end_date_iso = m.end_date_iso,
              condition_id = m.condition_id,
              market_type  = detect_market_type(m.question),
            )

    if clob_feed and isinstance(clob_feed, MockClobFeed):
        clob_feed.markets = markets

    _sse("markets_loaded", {"total": len(all_markets), "short": len(short_markets)})


def _demo() -> list[PolyMarket]:
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone.utc)
    demos = [
        ("Will BTC be above $90,000 in 45 min?",         "BTC", 0.38, now + timedelta(minutes=45)),
        ("Will BTC be above $85,000 in the next hour?",   "BTC", 0.55, now + timedelta(hours=1)),
        ("Will ETH be above $3,200 in the next 30 min?",  "ETH", 0.47, now + timedelta(minutes=30)),
        ("Will ETH stay above $3,000 for 2 hours?",       "ETH", 0.62, now + timedelta(hours=2)),
        ("Will SOL hit $150 in the next hour?",            "SOL", 0.31, now + timedelta(minutes=55)),
        ("Will BTC drop below $80,000 today?",             "BTC", 0.22, now + timedelta(hours=90)),
        ("Will BNB reach $600 in 2 hours?",                "BNB", 0.18, now + timedelta(hours=2)),
        ("Will BTC close above $85k in 20 minutes?",       "BTC", 0.41, now + timedelta(minutes=20)),
    ]
    result = []
    for i, (q, sym, p, end) in enumerate(demos):
        result.append(PolyMarket(
            token_id     = f"demo_yes_{i:04d}",
            no_token_id  = f"demo_no_{i:04d}",
            condition_id = f"demo_cid_{i:04d}",
            question     = q,
            slug         = f"demo-{i}",
            symbol       = sym,
            end_date_iso = end.isoformat().replace("+00:00", "Z"),
            yes_price    = p,
            no_price     = 1 - p,
            volume       = 50000 + i * 15000,
            liquidity    = 5000 + i * 2000,
        ))
    return result


# ── Startup ────────────────────────────────────────────────────────────────────

def startup():
    global binance_feed, clob_feed, IS_LIVE_BIN, IS_LIVE_CLOB

    _load_markets()
    time.sleep(0.3)

    try:
        import websockets  # noqa
        bf = BinanceFeed(SYMBOLS, on_binance)
        bf.start()
        time.sleep(2.5)
        if bf.connected:
            binance_feed = bf
            IS_LIVE_BIN  = True
            print("[App] ✓ Binance WebSocket live")
        else:
            raise ConnectionError("Binance não conectou")
    except Exception as e:
        print(f"[App] Binance mock ({e})")
        bf = MockBinanceFeed(SYMBOLS, on_binance)
        bf.start()
        binance_feed = bf

    token_ids = [m.token_id for m in all_markets if m.token_id]
    try:
        import websockets  # noqa
        cf = ClobFeed(token_ids=token_ids, on_update=on_clob)
        cf.start()
        time.sleep(3.5)
        if cf.connected:
            clob_feed    = cf
            IS_LIVE_CLOB = True
            print("[App] ✓ CLOB WebSocket live")
        else:
            raise ConnectionError("CLOB não conectou")
    except Exception as e:
        print(f"[App] CLOB mock ({e})")
        cf = MockClobFeed(markets=all_markets, on_update=on_clob, price_buffers=price_buffers)
        cf.start()
        clob_feed = cf

    time.sleep(2)
    simulator.start()
    print("[App] ✓ Simulador iniciado (foco short-term)")

    def pusher():
        while True:
            time.sleep(2)
            try:
                _sse("snapshot", _snap())
            except Exception:
                pass

    threading.Thread(target=pusher, daemon=True).start()

    def market_refresher():
        while True:
            time.sleep(300)
            try:
                _load_markets()
            except Exception as e:
                print(f"[App] Market refresh error: {e}")

    threading.Thread(target=market_refresher, daemon=True).start()


threading.Thread(target=startup, daemon=True).start()


# ── Dashboard ──────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template_string(DASHBOARD)


DASHBOARD = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Polymarket Bot</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@300;400;500;600&family=IBM+Plex+Sans:wght@300;400;500&display=swap" rel="stylesheet">
<style>
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
:root {
  --bg:        #080a0e;
  --s1:        #0d1017;
  --s2:        #111520;
  --s3:        #161b26;
  --border:    #1e2535;
  --border2:   #252d3d;
  --text:      #4a5568;
  --text2:     #6b7a95;
  --text3:     #8fa3bf;
  --hi:        #c8daf0;
  --green:     #00e87a;
  --green2:    rgba(0,232,122,.06);
  --green3:    rgba(0,232,122,.15);
  --red:       #ff4d6a;
  --red2:      rgba(255,77,106,.06);
  --blue:      #3d9eff;
  --blue2:     rgba(61,158,255,.07);
  --amber:     #ffb547;
  --amber2:    rgba(255,181,71,.07);
  --purple:    #a78bfa;
  --purple2:   rgba(167,139,250,.07);
  --mono:      'IBM Plex Mono', monospace;
  --sans:      'IBM Plex Sans', sans-serif;
  --r:         6px;
}
html, body { height: 100%; overflow: hidden; background: var(--bg); color: var(--text2); font-family: var(--sans); font-size: 13px; line-height: 1.5; }
 
/* scrollbar */
::-webkit-scrollbar { width: 3px; height: 3px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { background: var(--border2); border-radius: 2px; }
 
/* layout */
.root { display: grid; grid-template-rows: 52px 1fr; height: 100vh; }
.body { display: grid; grid-template-columns: 220px 1fr 280px; height: 100%; overflow: hidden; min-height: 0; }
.col  { display: flex; flex-direction: column; border-right: 1px solid var(--border); min-height: 0; overflow: hidden; }
.col:last-child { border-right: none; }
.col.hidden { width: 0 !important; overflow: hidden; border: none; }
.scroll { overflow-y: auto; overflow-x: hidden; flex: 1; min-height: 0; }
 
/* topbar */
.topbar {
  display: flex; align-items: center; gap: 10px; padding: 0 16px;
  background: var(--s1); border-bottom: 1px solid var(--border);
  flex-shrink: 0;
}
.brand {
  font-family: var(--mono); font-size: 11px; font-weight: 600;
  color: var(--hi); letter-spacing: .18em;
}
.sep { width: 1px; height: 20px; background: var(--border2); margin: 0 2px; }
 
.dot { width: 6px; height: 6px; border-radius: 50%; flex-shrink: 0; }
.dot.live { background: var(--green); box-shadow: 0 0 8px var(--green); animation: pulse 2s infinite; }
.dot.mock { background: var(--amber); }
@keyframes pulse { 0%,100%{opacity:1;transform:scale(1)} 50%{opacity:.4;transform:scale(.8)} }
 
.badge {
  font-family: var(--mono); font-size: 9px; font-weight: 500; letter-spacing: .08em;
  padding: 3px 8px; border-radius: 3px; border: 1px solid; text-transform: uppercase;
}
.badge-green { background: var(--green2); color: var(--green); border-color: rgba(0,232,122,.2); }
.badge-amber { background: var(--amber2); color: var(--amber); border-color: rgba(255,181,71,.2); }
.badge-purple{ background: var(--purple2); color: var(--purple); border-color: rgba(167,139,250,.2); }
.badge-blue  { background: var(--blue2);   color: var(--blue);  border-color: rgba(61,158,255,.2); }
 
.topbar-right { display: flex; gap: 6px; align-items: center; margin-left: auto; }
 
/* metric chips in topbar */
.metric {
  display: flex; flex-direction: column; align-items: flex-end;
  padding: 4px 10px; border-radius: var(--r);
  border: 1px solid var(--border); background: var(--s2);
  min-width: 72px;
}
.metric-l { font-size: 9px; color: var(--text); letter-spacing: .1em; text-transform: uppercase; margin-bottom: 1px; }
.metric-v { font-family: var(--mono); font-size: 14px; font-weight: 500; line-height: 1; color: var(--hi); }
.metric-v.up  { color: var(--green); }
.metric-v.dn  { color: var(--red); }
.metric-v.neu { color: var(--blue); }
 
.topbar-actions { display: flex; gap: 5px; margin-left: 6px; }
.btn {
  font-family: var(--mono); font-size: 9px; letter-spacing: .1em; text-transform: uppercase;
  padding: 6px 14px; border-radius: var(--r); cursor: pointer; border: 1px solid;
  background: transparent; transition: all .15s; font-weight: 500;
}
.btn-run  { border-color: rgba(0,232,122,.3); color: var(--green); }
.btn-run:hover  { background: var(--green2); border-color: rgba(0,232,122,.5); }
.btn-stop { border-color: rgba(255,77,106,.3); color: var(--red); }
.btn-stop:hover { background: var(--red2); }
.btn-ghost { border-color: var(--border2); color: var(--text2); }
.btn-ghost:hover { border-color: var(--text3); color: var(--hi); }
 
.panel-toggles { display: flex; gap: 3px; }
.ptog {
  font-family: var(--mono); font-size: 9px; letter-spacing: .08em; text-transform: uppercase;
  padding: 5px 10px; border-radius: var(--r); cursor: pointer;
  border: 1px solid var(--border); color: var(--text); background: transparent;
  transition: all .15s;
}
.ptog.active { border-color: rgba(61,158,255,.3); color: var(--blue); background: var(--blue2); }
.ptog:hover:not(.active) { border-color: var(--border2); color: var(--text3); }
 
/* col headers */
.col-head {
  font-family: var(--mono); font-size: 9px; font-weight: 600; letter-spacing: .16em;
  color: var(--text); padding: 10px 14px; border-bottom: 1px solid var(--border);
  background: var(--s1); flex-shrink: 0; display: flex; align-items: center;
  justify-content: space-between; text-transform: uppercase;
}
.col-head-val { font-size: 9px; color: var(--text2); font-weight: 400; letter-spacing: 0; font-family: var(--sans); }
 
/* warmup */
.warmup-bar {
  display: none; padding: 8px 14px; background: rgba(255,181,71,.04);
  border-bottom: 1px solid rgba(255,181,71,.12); font-family: var(--mono);
  font-size: 9px; color: var(--amber); letter-spacing: .06em; flex-shrink: 0;
  align-items: center; gap: 10px;
}
.warmup-bar.show { display: flex; }
.wu-prog { flex: 1; height: 2px; background: rgba(255,181,71,.12); border-radius: 1px; overflow: hidden; }
.wu-fill { height: 100%; background: var(--amber); transition: width .5s linear; }
 
/* stat grid */
.stat-grid {
  display: grid; grid-template-columns: repeat(3, 1fr);
  border-bottom: 1px solid var(--border); flex-shrink: 0;
}
.stat { padding: 12px 14px; border-right: 1px solid var(--border); position: relative; }
.stat:last-child { border-right: none; }
.stat:nth-child(3) { border-right: none; }
.stat-accent { position: absolute; top: 0; left: 0; right: 0; height: 2px; border-radius: 0 0 2px 2px; }
.a-g { background: linear-gradient(90deg, var(--green), transparent); }
.a-b { background: linear-gradient(90deg, var(--blue), transparent); }
.a-a { background: linear-gradient(90deg, var(--amber), transparent); }
.a-r { background: linear-gradient(90deg, var(--red), transparent); }
.a-p { background: linear-gradient(90deg, var(--purple), transparent); }
.a-d { background: linear-gradient(90deg, var(--border2), transparent); }
.stat-l { font-size: 9px; letter-spacing: .12em; color: var(--text); text-transform: uppercase; margin-bottom: 4px; }
.stat-v { font-family: var(--mono); font-size: 20px; font-weight: 500; line-height: 1; color: var(--hi); }
.stat-sub { font-size: 10px; color: var(--text2); margin-top: 3px; font-family: var(--mono); }
 
/* equity */
.eq-wrap { padding: 12px 14px; border-bottom: 1px solid var(--border); flex-shrink: 0; }
.eq-head { display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; }
.eq-title { font-size: 9px; letter-spacing: .12em; color: var(--text); text-transform: uppercase; font-family: var(--mono); }
.eq-sub   { font-family: var(--mono); font-size: 9px; color: var(--text2); }
 
/* trades */
.pos-head {
  font-family: var(--mono); font-size: 9px; font-weight: 600; letter-spacing: .16em; color: var(--text);
  text-transform: uppercase; padding: 10px 14px; border-bottom: 1px solid var(--border);
  background: var(--s1); flex-shrink: 0; display: flex; justify-content: space-between; align-items: center;
}
.pos-cnt { color: var(--blue); font-size: 9px; }
 
.trade-card {
  padding: 10px 12px; border-bottom: 1px solid var(--border);
  border-left: 2px solid transparent; transition: background .1s;
}
.trade-card:hover { background: rgba(255,255,255,.01); }
.trade-card.yes  { border-left-color: var(--green); }
.trade-card.no   { border-left-color: var(--red); }
.trade-card.win  { border-left-color: var(--green); opacity: .45; }
.trade-card.loss { border-left-color: var(--red);   opacity: .4; }
.t-top { display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 3px; }
.t-sym { font-family: var(--mono); font-size: 13px; font-weight: 600; color: var(--hi); }
.t-pnl { font-family: var(--mono); font-size: 11px; font-weight: 500; }
.t-q   { font-size: 11px; color: var(--text3); line-height: 1.4; margin-bottom: 6px; }
.t-tags { display: flex; gap: 4px; flex-wrap: wrap; }
.tag {
  font-family: var(--mono); font-size: 9px; padding: 2px 6px;
  border-radius: 3px; border: 1px solid; letter-spacing: .04em;
}
.tag-yes  { background: var(--green2); color: var(--green); border-color: rgba(0,232,122,.2); }
.tag-no   { background: var(--red2);   color: var(--red);   border-color: rgba(255,77,106,.2); }
.tag-open { background: var(--blue2);  color: var(--blue);  border-color: rgba(61,158,255,.2); }
.tag-cls  { background: transparent; color: var(--text2); border-color: var(--border2); }
.tag-n    { background: transparent; color: var(--text2); border-color: var(--border); }
.tag-fee  { background: var(--amber2); color: var(--amber); border-color: rgba(255,181,71,.2); }
.prog     { height: 2px; background: var(--border); margin-top: 7px; overflow: hidden; border-radius: 1px; }
.prog-f   { height: 100%; background: rgba(61,158,255,.4); transition: width 1s linear; }
 
/* signal blocks */
.sig-block {
  padding: 11px 13px; border-bottom: 1px solid var(--border);
  transition: background .1s; cursor: default;
}
.sig-block:hover { background: rgba(255,255,255,.01); }
.sig-row { display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 6px; }
.sig-sym  { font-family: var(--mono); font-size: 15px; font-weight: 600; color: var(--hi); }
.sig-price{ font-family: var(--mono); font-size: 13px; color: var(--blue); }
.spark-wrap { height: 28px; margin-bottom: 7px; }
canvas.spark { width: 100%; height: 28px; display: block; }
.sig-chips { display: flex; gap: 4px; flex-wrap: wrap; margin-bottom: 7px; }
.chip {
  font-family: var(--mono); font-size: 9px; font-weight: 500; letter-spacing: .06em;
  padding: 3px 7px; border-radius: 3px; border: 1px solid;
}
.chip-yes  { background: var(--green2); color: var(--green); border-color: rgba(0,232,122,.2); }
.chip-no   { background: var(--red2);   color: var(--red);   border-color: rgba(255,77,106,.2); }
.chip-skip { background: transparent;   color: var(--text2); border-color: var(--border); }
.chip-hi   { background: var(--blue2);  color: var(--blue);  border-color: rgba(61,158,255,.2); }
.chip-med  { background: var(--amber2); color: var(--amber); border-color: rgba(255,181,71,.2); }
.chip-lo   { background: transparent;   color: var(--text);  border-color: var(--border); }
.chip-edge { background: var(--purple2);color: var(--purple);border-color: rgba(167,139,250,.2); }
.sig-inds { display: grid; grid-template-columns: 1fr 1fr; gap: 3px; }
.ind { padding: 5px 7px; background: var(--s2); border-radius: 4px; border: 1px solid var(--border); }
.ind-l { font-size: 8px; color: var(--text); letter-spacing: .1em; text-transform: uppercase; margin-bottom: 2px; }
.ind-v { font-family: var(--mono); font-size: 11px; font-weight: 500; color: var(--hi); }
.up  { color: var(--green) !important; }
.dn  { color: var(--red) !important; }
.dim { color: var(--text2) !important; }
.flash-up { animation: fg .4s; }
.flash-dn { animation: fr .4s; }
@keyframes fg { 0%{color:var(--green)} 100%{} }
@keyframes fr { 0%{color:var(--red)} 100%{} }
 
/* market list */
.mkt-tabs { display: flex; border-bottom: 1px solid var(--border); flex-shrink: 0; background: var(--s1); }
.tab {
  font-family: var(--mono); font-size: 9px; letter-spacing: .12em; text-transform: uppercase;
  padding: 9px 13px; cursor: pointer; color: var(--text);
  border-bottom: 2px solid transparent; transition: all .15s; display: flex; align-items: center; gap: 5px;
}
.tab.on { color: var(--blue); border-bottom-color: var(--blue); }
.tab-c {
  font-size: 8px; background: var(--s3); border: 1px solid var(--border2);
  color: var(--purple); padding: 1px 5px; border-radius: 10px; font-family: var(--mono);
}
.mkt-hdr {
  display: grid; grid-template-columns: 1fr 48px 40px 36px; gap: 4px;
  padding: 7px 12px; font-size: 8px; letter-spacing: .1em; color: var(--text);
  text-transform: uppercase; background: var(--s2); border-bottom: 1px solid var(--border);
  position: sticky; top: 0; z-index: 5; font-family: var(--mono);
}
.mkt-row {
  display: grid; grid-template-columns: 1fr 48px 40px 36px; gap: 4px;
  padding: 9px 12px; border-bottom: 1px solid var(--border); align-items: start;
  transition: background .1s;
}
.mkt-row:hover { background: var(--s2); }
.mkt-q { font-size: 11px; color: var(--hi); line-height: 1.4; }
.mkt-meta { font-size: 9px; color: var(--text2); margin-top: 3px; display: flex; gap: 6px; align-items: center; }
.mkt-sym { color: var(--blue); font-family: var(--mono); font-weight: 500; }
.mkt-p { font-family: var(--mono); font-size: 12px; text-align: right; font-weight: 500; }
.mkt-t { font-family: var(--mono); font-size: 10px; color: var(--text2); text-align: right; }
.mkt-sig { display: flex; justify-content: center; align-items: flex-start; padding-top: 2px; }
.vbar  { height: 2px; background: var(--border); margin-top: 4px; border-radius: 1px; }
.vfill { height: 100%; background: rgba(61,158,255,.18); border-radius: 1px; }
.sdot  { width: 5px; height: 5px; border-radius: 50%; background: var(--purple); display: inline-block; margin-right: 4px; vertical-align: middle; flex-shrink: 0; }
 
/* log */
.log-wrap { padding: 4px 0; }
.log-e {
  font-family: var(--mono); font-size: 10px; line-height: 1.6;
  padding: 5px 14px; border-left: 2px solid transparent;
  transition: background .1s;
}
.log-e:hover { background: var(--s2); }
.le-open  { border-left-color: var(--blue); }
.le-win   { border-left-color: var(--green); background: rgba(0,232,122,.02); }
.le-loss  { border-left-color: var(--red);   background: rgba(255,77,106,.02); }
.le-start { border-left-color: var(--amber); }
.le-skip  { border-left-color: transparent; opacity: .35; }
.le-real  { border-left-color: var(--purple); }
.le-pend  { border-left-color: var(--amber); background: rgba(255,181,71,.02); }
.le-ts  { color: var(--text); margin-right: 6px; }
.le-ev  { font-weight: 600; margin-right: 6px; color: var(--text3); font-size: 9px; letter-spacing: .06em; }
.le-msg { color: var(--text2); }
 
/* empty */
.empty {
  padding: 32px 16px; text-align: center; color: var(--text);
  font-size: 10px; letter-spacing: .06em; line-height: 2.6;
}
.empty-icon { font-size: 20px; display: block; margin-bottom: 8px; opacity: .3; }
 
/* pending badge */
.pending-bar {
  display: none; padding: 8px 14px; background: rgba(255,181,71,.04);
  border-bottom: 1px solid rgba(255,181,71,.12); font-family: var(--mono);
  font-size: 10px; color: var(--amber); flex-shrink: 0; align-items: center; gap: 8px;
}
.pending-bar.show { display: flex; }
</style>
</head>
<body>
<div class="root">
 
<!-- TOPBAR -->
<div class="topbar">
  <span class="dot live" id="status-dot"></span>
  <span class="brand">ALPHA-GUY</span>
  <div class="sep"></div>
  <span class="badge badge-amber" id="pill-bin">BIN MOCK</span>
  <span class="badge badge-amber" id="pill-clob">CLOB MOCK</span>
  <span class="badge badge-purple" id="pill-short" style="display:none">0 SHORT</span>
 
  <div class="topbar-right">
    <div class="metric">
      <span class="metric-l">Balance</span>
      <span class="metric-v up" id="n-bal">$100</span>
    </div>
    <div class="metric">
      <span class="metric-l">P&L</span>
      <span class="metric-v" id="n-pnl">$0</span>
    </div>
    <div class="metric">
      <span class="metric-l">Win Rate</span>
      <span class="metric-v neu" id="n-wr">—</span>
    </div>
    <div class="metric">
      <span class="metric-l">Fees</span>
      <span class="metric-v" style="color:var(--amber)" id="n-fees">$0</span>
    </div>
    <div class="metric">
      <span class="metric-l">Latency</span>
      <span class="metric-v" id="n-lat">—</span>
    </div>
 
    <div class="sep"></div>
 
    <div class="panel-toggles">
      <button class="ptog active" id="ptog-signals" onclick="togglePanel('signals')">Signals</button>
      <button class="ptog active" id="ptog-markets" onclick="togglePanel('markets')">Markets</button>
      <button class="ptog active" id="ptog-log"     onclick="togglePanel('log')">Log</button>
    </div>
 
    <div class="topbar-actions">
      <button class="btn btn-run"   id="btn-tog"  onclick="toggleSim()">▶ Start</button>
      <button class="btn btn-ghost" onclick="resetSim()"   title="Reset">↺</button>
      <button class="btn btn-ghost" onclick="refreshMkts()" title="Refresh markets">⟳</button>
    </div>
  </div>
</div>
 
<!-- BODY -->
<div class="body">
 
  <!-- LEFT: Signals + Markets -->
  <div class="col" id="col-signals">
    <div class="col-head">
      Signals
      <span class="col-head-val" id="h-bin-status">mock</span>
    </div>
    <div class="scroll" id="spot"></div>
 
    <div id="market-panel">
      <div class="mkt-tabs">
        <div class="tab on" id="tab-s" onclick="setTab('short')">
          Short <span class="tab-c" id="cnt-s">0</span>
        </div>
        <div class="tab" id="tab-a" onclick="setTab('all')">
          All <span class="tab-c" id="cnt-a">0</span>
        </div>
      </div>
      <div class="scroll" id="mkts" style="max-height:240px"></div>
    </div>
  </div>
 
  <!-- CENTER -->
  <div class="col" id="col-main" style="border-right:none">
 
    <div class="warmup-bar" id="warmup-bar">
      <span id="wu-label">Warming up…</span>
      <div class="wu-prog"><div class="wu-fill" id="wu-fill" style="width:0%"></div></div>
    </div>
 
    <div class="pending-bar" id="pending-bar">
      ⏳ <span id="pending-label">0 trades aguardando resolução oracle</span>
    </div>
 
    <div class="stat-grid">
      <div class="stat">
        <div class="stat-accent a-g"></div>
        <div class="stat-l">Balance</div>
        <div class="stat-v" id="s-bal">$100</div>
      </div>
      <div class="stat">
        <div class="stat-accent a-b"></div>
        <div class="stat-l">P&L</div>
        <div class="stat-v" id="s-pnl">$0</div>
        <div class="stat-sub" id="s-roi">0%</div>
      </div>
      <div class="stat">
        <div class="stat-accent a-a"></div>
        <div class="stat-l">Open</div>
        <div class="stat-v" id="s-open">0</div>
        <div class="stat-sub" id="s-unr"></div>
      </div>
      <div class="stat">
        <div class="stat-accent a-g"></div>
        <div class="stat-l">Wins</div>
        <div class="stat-v" id="s-wins">0</div>
        <div class="stat-sub" id="s-avgw"></div>
      </div>
      <div class="stat">
        <div class="stat-accent a-r"></div>
        <div class="stat-l">Losses</div>
        <div class="stat-v" id="s-loss">0</div>
        <div class="stat-sub" id="s-avgl"></div>
      </div>
      <div class="stat">
        <div class="stat-accent a-d"></div>
        <div class="stat-l">Max DD</div>
        <div class="stat-v" id="s-dd">0%</div>
        <div class="stat-sub" id="s-streak"></div>
      </div>
    </div>
 
    <div class="eq-wrap">
      <div class="eq-head">
        <span class="eq-title">Equity Curve</span>
        <span class="eq-sub" id="eq-sub"></span>
      </div>
      <canvas id="eq" height="60"></canvas>
    </div>
 
    <div class="pos-head">
      Positions
      <span class="pos-cnt" id="pos-cnt"></span>
    </div>
    <div class="scroll" id="trades"></div>
  </div>
 
  <!-- RIGHT: Log -->
  <div class="col" id="col-log">
    <div class="col-head">
      Event Log
      <span class="col-head-val" id="log-cnt">0 entries</span>
    </div>
    <div class="scroll">
      <div class="log-wrap" id="log"></div>
    </div>
  </div>
 
</div>
</div>
 
<script>
// ── State ──────────────────────────────────────────────────────────────
let snap = {}, eq = [{ts: Date.now()/1e3, v: 100}];
let ph = {}, prevPx = {}, maxVol = 1, activeTab = 'short';
const SYMS = ['BTC','ETH','SOL','BNB','MATIC','DOGE','XRP'];
SYMS.forEach(s => ph[s] = []);
 
const panelState = {
  signals: sessionStorage.getItem('p_signals') !== 'false',
  log:     sessionStorage.getItem('p_log')     !== 'false',
};
const panelCols = { signals: 'col-signals', log: 'col-log' };
 
function togglePanel(key) {
  if (key === 'markets') key = 'signals';
  panelState[key] = !panelState[key];
  sessionStorage.setItem('p_' + key, panelState[key]);
  const col = document.getElementById(panelCols[key]);
  const btn = document.getElementById('ptog-' + key);
  col.classList.toggle('hidden', !panelState[key]);
  btn.classList.toggle('active', panelState[key]);
  if (key === 'signals')
    document.getElementById('ptog-markets').classList.toggle('active', panelState[key]);
}
['signals','log'].forEach(k => {
  if (!panelState[k]) {
    document.getElementById(panelCols[k]).classList.add('hidden');
    document.getElementById('ptog-' + k).classList.remove('active');
    if (k === 'signals') document.getElementById('ptog-markets').classList.remove('active');
  }
});
 
// ── SSE ────────────────────────────────────────────────────────────────
(function conn() {
  const es = new EventSource('/stream');
  es.addEventListener('snapshot', e => apply(JSON.parse(e.data)));
  es.addEventListener('tick', e => {
    const d = JSON.parse(e.data);
    if (ph[d.sym]) { ph[d.sym].push(d.price); if (ph[d.sym].length > 100) ph[d.sym].shift(); }
    flashPx(d.sym, d.price);
  });
  es.onerror = () => { setTimeout(conn, 3000); es.close(); };
})();
setInterval(async () => {
  try { apply(await (await fetch('/api/snapshot')).json()); } catch {}
}, 5000);
 
// ── Apply ──────────────────────────────────────────────────────────────
function apply(d) {
  snap = d;
  const st = d.stats || {}, bal = st.balance ?? 100, pnl = st.total_pnl ?? 0;
  const w = st.wins ?? 0, l = st.losses ?? 0;
  const wr = w + l > 0 ? (w / (w + l) * 100).toFixed(0) + '%' : '—';
 
  // topbar metrics
  set('n-bal', '$' + bal.toFixed(2), bal >= 100 ? 'var(--green)' : 'var(--red)');
  setPnl('n-pnl', pnl);
  set('n-wr', wr, 'var(--blue)');
  set('n-fees', '$' + (st.total_fees || 0).toFixed(3), 'var(--amber)');
  const lat = d.feed?.clob_lat_ms ?? 0;
  set('n-lat', lat > 0 ? lat.toFixed(1) + 'ms' : '—',
      lat < 30 ? 'var(--green)' : lat < 100 ? 'var(--amber)' : 'var(--red)');
 
  // badges
  const binLive = d.feed?.binance_live, clobLive = d.feed?.clob_live;
  const pb = document.getElementById('pill-bin');
  pb.textContent = binLive ? 'BIN LIVE' : 'BIN MOCK';
  pb.className   = 'badge ' + (binLive ? 'badge-green' : 'badge-amber');
  const pc = document.getElementById('pill-clob');
  pc.textContent = clobLive ? 'CLOB LIVE' : 'CLOB MOCK';
  pc.className   = 'badge ' + (clobLive ? 'badge-green' : 'badge-amber');
  set('h-bin-status', binLive ? '● live' : '◌ mock', binLive ? 'var(--green)' : 'var(--text2)');
 
  const sc = d.feed?.short_markets ?? 0;
  const ps = document.getElementById('pill-short');
  ps.textContent = sc + ' short';
  ps.style.display = sc > 0 ? 'inline' : 'none';
 
  // run button
  const btn = document.getElementById('btn-tog');
  btn.textContent = d.running ? '■ Stop' : '▶ Start';
  btn.className   = 'btn ' + (d.running ? 'btn-stop' : 'btn-run');
 
  // warmup
  const wu = d.warmup_remaining ?? 0;
  const wuBar = document.getElementById('warmup-bar');
  if (wu > 0) {
    wuBar.classList.add('show');
    document.getElementById('wu-label').textContent = `Warming up — ${wu}s remaining`;
    document.getElementById('wu-fill').style.width = Math.max(0, (1 - wu / 30) * 100) + '%';
  } else {
    wuBar.classList.remove('show');
  }
 
  // pending bar (Fase 2)
  const pendingCount = d.pending_resolution_count ?? 0;
  const pendingBar = document.getElementById('pending-bar');
  if (pendingCount > 0) {
    pendingBar.classList.add('show');
    document.getElementById('pending-label').textContent =
      `${pendingCount} trade${pendingCount > 1 ? 's' : ''} aguardando resolução oracle`;
  } else {
    pendingBar.classList.remove('show');
  }
 
  // stats
  const pe = document.getElementById('s-pnl');
  pe.textContent = (pnl >= 0 ? '+$' : '-$') + Math.abs(pnl).toFixed(2);
  pe.style.color = pnl >= 0 ? 'var(--green)' : 'var(--red)';
  set('s-bal', '$' + bal.toFixed(0));
  set('s-roi', (((bal - 100) / 100) * 100).toFixed(1) + '% ROI');
  const ot = d.open_trades || [];
  set('s-open', ot.length);
  const unr = ot.reduce((s, t) => s + (t.unrealized_pnl || 0), 0);
  set('s-unr', (unr >= 0 ? '+' : '') + '$' + Math.abs(unr).toFixed(2) + ' unr');
  set('s-wins', w); set('s-loss', l);
  set('s-avgw', 'avg $' + (st.avg_win_usdc || 0).toFixed(2));
  set('s-avgl', 'avg $' + (st.avg_loss_usdc || 0).toFixed(2));
  set('s-dd', ((st.max_drawdown_pct || 0) * 100).toFixed(1) + '%');
  set('s-streak', st.win_streak > 1 ? `🔥 ${st.win_streak}x` : st.loss_streak > 1 ? `❌ ${st.loss_streak}x` : '');
  set('pos-cnt', ot.length ? ot.length + ' open' : '');
 
  if (d.equity_curve?.length) eq = d.equity_curve.map(([ts, v]) => ({ ts, v }));
  drawEq();
  set('eq-sub', `$${(st.total_fees || 0).toFixed(3)} fees · ${st.total_trades || 0} trades`);
 
  set('cnt-s', (d.short_markets || []).length);
  set('cnt-a', (d.all_markets || []).length);
  set('log-cnt', (d.event_log || []).length + ' entries');
 
  renderSpot(d.signals || {}, d.buffers || {});
  renderMarkets(d.short_markets || [], d.all_markets || [], d.signals || {});
  renderTrades(ot, d.closed_trades || []);
  renderLog(d.event_log || []);
}
 
// ── Helpers ────────────────────────────────────────────────────────────
function set(id, v, c) {
  const e = document.getElementById(id); if (!e) return;
  e.textContent = v; if (c) e.style.color = c;
}
function setPnl(id, v) {
  const e = document.getElementById(id); if (!e) return;
  e.textContent = (v >= 0 ? '+$' : '-$') + Math.abs(v).toFixed(2);
  e.style.color = v >= 0 ? 'var(--green)' : 'var(--red)';
}
function fPx(s, p) {
  if (!p) return '—';
  if (s === 'BTC') return '$' + Math.round(p).toLocaleString();
  if (s === 'ETH') return '$' + p.toFixed(1);
  if (s === 'XRP') return '$' + p.toFixed(4);
  return '$' + p.toFixed(3);
}
function fNum(n) {
  if (!n) return '0';
  if (n >= 1e6) return (n / 1e6).toFixed(1) + 'M';
  if (n >= 1e3) return (n / 1e3).toFixed(0) + 'k';
  return n.toFixed(0);
}
function fTime(m) {
  if (m < 1) return '<1m';
  if (m < 60) return Math.round(m) + 'm';
  if (m < 1440) return (m / 60).toFixed(1) + 'h';
  return (m / 1440).toFixed(1) + 'd';
}
function flashPx(sym, p) {
  const el = document.getElementById('px-' + sym); if (!el) return;
  const pr = prevPx[sym];
  if (pr && p !== pr) {
    el.classList.remove('flash-up', 'flash-dn');
    void el.offsetWidth;
    el.classList.add(p > pr ? 'flash-up' : 'flash-dn');
  }
  el.textContent = fPx(sym, p); prevPx[sym] = p;
}
function setTab(t) {
  activeTab = t;
  document.getElementById('tab-s').classList.toggle('on', t === 'short');
  document.getElementById('tab-a').classList.toggle('on', t === 'all');
  renderMarkets(snap.short_markets || [], snap.all_markets || [], snap.signals || {});
}
 
// ── Render: signals ────────────────────────────────────────────────────
function renderSpot(sigs, bufs) {
  const c = document.getElementById('spot');
  let h = '';
  for (const sym of SYMS) {
    const buf = bufs[sym] || {}, sig = sigs[sym] || {};
    const p = buf.price || ph[sym]?.at(-1);
    const dir = sig.direction || 'SKIP';
    const dc = dir === 'YES' ? 'chip-yes' : dir === 'NO' ? 'chip-no' : 'chip-skip';
    const cc = sig.confidence === 'high' ? 'chip-hi' : sig.confidence === 'medium' ? 'chip-med' : 'chip-lo';
    const m1 = sig.momentum_1m || 0, m5 = sig.momentum_5m || 0;
    const r = sig.rsi || 50, vd = sig.vwap_dev || 0;
    h += `<div class="sig-block">
      <div class="sig-row">
        <span class="sig-sym">${sym}</span>
        <span class="sig-price" id="px-${sym}">${fPx(sym, p)}</span>
      </div>
      <div class="spark-wrap"><canvas class="spark" id="sp-${sym}" height="28"></canvas></div>
      <div class="sig-chips">
        <span class="chip ${dc}">${dir === 'YES' ? '↑ YES' : dir === 'NO' ? '↓ NO' : '— SKIP'}</span>
        <span class="chip ${cc}">${sig.confidence || 'low'}</span>
        ${sig.edge != null ? `<span class="chip chip-edge">e${(sig.edge * 100).toFixed(1)}%</span>` : ''}
      </div>
      <div class="sig-inds">
        <div class="ind"><div class="ind-l">1m</div><div class="ind-v ${m1>0?'up':m1<0?'dn':'dim'}">${m1>=0?'+':''}${m1.toFixed(2)}%</div></div>
        <div class="ind"><div class="ind-l">5m</div><div class="ind-v ${m5>0?'up':m5<0?'dn':'dim'}">${m5>=0?'+':''}${m5.toFixed(2)}%</div></div>
        <div class="ind"><div class="ind-l">RSI</div><div class="ind-v ${r>65?'dn':r<35?'up':'dim'}">${r.toFixed(0)}</div></div>
        <div class="ind"><div class="ind-l">VWAP</div><div class="ind-v ${vd>0?'dn':vd<0?'up':'dim'}">${vd>=0?'+':''}${vd.toFixed(2)}%</div></div>
      </div>
    </div>`;
  }
  c.innerHTML = h;
  SYMS.forEach(s => {
    const cv = document.getElementById('sp-' + s);
    if (cv && ph[s]?.length > 2) drawSpark(cv, ph[s]);
  });
}
 
// ── Render: markets ────────────────────────────────────────────────────
function renderMarkets(short, all, sigs) {
  const c = document.getElementById('mkts');
  const markets = activeTab === 'short' ? short : all;
  if (!markets.length) {
    c.innerHTML = `<div class="empty"><span class="empty-icon">◎</span>${
      activeTab === 'short' ? 'No short-term markets<br>active right now' : 'Loading markets…'
    }</div>`;
    return;
  }
  const sorted = [...markets].sort((a, b) => a.mins_left - b.mins_left);
  maxVol = Math.max(...markets.map(m => m.volume || 1), 1);
  let h = `<div class="mkt-hdr"><div>Market</div><div>Prob</div><div>Sig</div><div>TTL</div></div>`;
  for (const m of sorted) {
    const p = m.yes_price || 0.5;
    const pc = p > 0.65 ? 'var(--green)' : p < 0.35 ? 'var(--red)' : 'var(--blue)';
    const sym = m.symbol === 'CRYPTO' ? 'BTC' : m.symbol;
    const sig = sigs[sym] || {};
    const dir = sig.direction || 'SKIP';
    const dc = dir === 'YES' ? 'chip chip-yes' : dir === 'NO' ? 'chip chip-no' : 'chip chip-skip';
    const ml = m.mins_left ?? 9999;
    const tc = ml < 10 ? 'var(--red)' : ml < 30 ? 'var(--amber)' : 'var(--text2)';
    const vpct = Math.min(100, (m.volume || 0) / maxVol * 100);
    h += `<div class="mkt-row">
      <div>
        <div class="mkt-q">${m.is_short ? '<span class="sdot"></span>' : ''}${m.question}</div>
        <div class="mkt-meta"><span class="mkt-sym">${m.symbol}</span><span>$${fNum(m.volume)}</span></div>
        <div class="vbar"><div class="vfill" style="width:${vpct.toFixed(0)}%"></div></div>
      </div>
      <div class="mkt-p" style="color:${pc}">${(p * 100).toFixed(1)}%</div>
      <div class="mkt-sig"><span class="${dc}" style="font-size:8px">${dir}</span></div>
      <div class="mkt-t" style="color:${tc}">${fTime(ml)}</div>
    </div>`;
  }
  c.innerHTML = h;
}
 
// ── Render: trades ─────────────────────────────────────────────────────
function renderTrades(open, closed) {
  const c = document.getElementById('trades');
  if (!open.length && !closed.length) {
    c.innerHTML = '<div class="empty"><span class="empty-icon">◈</span>No positions yet.<br>Waiting for warm-up.</div>';
    return;
  }
  const now = Date.now() / 1e3;
  function row(t, isOpen) {
    const pnl = isOpen ? t.unrealized_pnl : t.pnl_usdc;
    const pc = pnl == null ? 'var(--text2)' : pnl >= 0 ? 'var(--green)' : 'var(--red)';
    const ps = pnl == null ? '—' : (pnl >= 0 ? '+$' : '-$') + Math.abs(pnl).toFixed(2);
    const cls = isOpen ? (t.action === 'BUY_YES' ? 'yes' : 'no') : (t.won ? 'win' : 'loss');
    const prog = isOpen ? Math.min(100, (now - t.entry_ts) / (t.horizon_min * 60) * 100) : 100;
    return `<div class="trade-card ${cls}">
      <div class="t-top">
        <span class="t-sym">${t.symbol}</span>
        <span class="t-pnl" style="color:${pc}">${isOpen ? '~' : ''} ${ps}</span>
      </div>
      <div class="t-q">${t.question}</div>
      <div class="t-tags">
        <span class="tag ${t.action==='BUY_YES'?'tag-yes':'tag-no'}">${t.action==='BUY_YES'?'↑ YES':'↓ NO'}</span>
        <span class="tag ${isOpen?'tag-open':'tag-cls'}">${isOpen ? 'OPEN' : t.exit_reason || 'CLOSED'}</span>
        <span class="tag tag-n">$${(t.gross_usdc || t.amount_usdc).toFixed(2)}</span>
        <span class="tag tag-fee">fee $${(t.fee_usdc || 0).toFixed(4)}</span>
        <span class="tag tag-n">${(t.entry_price * 100).toFixed(1)}¢</span>
        <span class="tag tag-n">e${(t.signal_edge * 100).toFixed(1)}%</span>
      </div>
      ${isOpen ? `<div class="prog"><div class="prog-f" style="width:${prog.toFixed(0)}%"></div></div>` : ''}
    </div>`;
  }
  c.innerHTML = open.map(t => row(t, true)).join('') +
    [...closed].reverse().slice(0, 30).map(t => row(t, false)).join('');
}
 
// ── Render: log ────────────────────────────────────────────────────────
function renderLog(logs) {
  const c = document.getElementById('log');
  c.innerHTML = [...logs].reverse().slice(0, 120).map(e => {
    const cls = e.event === 'TRADE_OPEN' ? 'le-open' :
      e.level === 'win' ? 'le-win' : e.level === 'loss' ? 'le-loss' :
      e.event?.includes('START') || e.event?.includes('RESET') ? 'le-start' :
      e.event === 'SKIP' ? 'le-skip' :
      e.event === 'RESOLVE_REAL' ? 'le-real' :
      e.event === 'PENDING_RESOLUTION' ? 'le-pend' : '';
    return `<div class="log-e ${cls}">
      <span class="le-ts">${e.ts_str}</span>
      <span class="le-ev">${e.event}</span>
      <span class="le-msg">${e.message}</span>
    </div>`;
  }).join('');
}
 
// ── Canvas: equity ─────────────────────────────────────────────────────
function drawEq() {
  const cv = document.getElementById('eq'); if (!cv || eq.length < 2) return;
  const w = cv.offsetWidth || 600, h = 60; cv.width = w; cv.height = h;
  const ctx = cv.getContext('2d'); ctx.clearRect(0, 0, w, h);
  const vals = eq.map(p => p.v);
  const mn = Math.min(...vals) * .997, mx = Math.max(...vals) * 1.003, rng = mx - mn || 1;
  const pts = vals.map((v, i) => [i / (vals.length - 1) * w, h - ((v - mn) / rng * (h - 6) + 3)]);
  const cur = vals.at(-1), up = cur >= 100;
  const col = up ? '#00e87a' : '#ff4d6a';
  // baseline
  const bl = h - ((100 - mn) / rng * (h - 6) + 3);
  ctx.beginPath(); ctx.moveTo(0, bl); ctx.lineTo(w, bl);
  ctx.strokeStyle = 'rgba(255,255,255,.05)'; ctx.lineWidth = 1;
  ctx.setLineDash([4, 5]); ctx.stroke(); ctx.setLineDash([]);
  // fill
  const gr = ctx.createLinearGradient(0, 0, 0, h);
  gr.addColorStop(0, up ? 'rgba(0,232,122,.12)' : 'rgba(255,77,106,.12)');
  gr.addColorStop(1, 'rgba(0,0,0,0)');
  ctx.beginPath(); ctx.moveTo(pts[0][0], pts[0][1]);
  pts.slice(1).forEach(([x, y]) => ctx.lineTo(x, y));
  ctx.lineTo(w, h); ctx.lineTo(0, h); ctx.closePath();
  ctx.fillStyle = gr; ctx.fill();
  // line
  ctx.beginPath(); ctx.moveTo(pts[0][0], pts[0][1]);
  pts.slice(1).forEach(([x, y]) => ctx.lineTo(x, y));
  ctx.strokeStyle = col; ctx.lineWidth = 1.5; ctx.stroke();
  // label
  ctx.fillStyle = col; ctx.font = '500 10px IBM Plex Mono';
  ctx.fillText('$' + cur.toFixed(2), w - 62, pts.at(-1)[1] - 5);
}
 
// ── Canvas: sparkline ──────────────────────────────────────────────────
function drawSpark(cv, prices) {
  const w = cv.offsetWidth || 196, h = 28; cv.width = w; cv.height = h;
  const ctx = cv.getContext('2d'); ctx.clearRect(0, 0, w, h);
  if (prices.length < 2) return;
  const mn = Math.min(...prices), mx = Math.max(...prices), rng = mx - mn || 1;
  const pts = prices.map((p, i) => [i / (prices.length - 1) * w, h - ((p - mn) / rng * (h - 2) + 1)]);
  const up = pts.at(-1)[1] <= pts[0][1];
  const gr = ctx.createLinearGradient(0, 0, 0, h);
  gr.addColorStop(0, up ? 'rgba(0,232,122,.18)' : 'rgba(255,77,106,.18)');
  gr.addColorStop(1, 'rgba(0,0,0,0)');
  ctx.beginPath(); ctx.moveTo(pts[0][0], pts[0][1]);
  pts.slice(1).forEach(([x, y]) => ctx.lineTo(x, y));
  ctx.lineTo(w, h); ctx.lineTo(0, h); ctx.closePath();
  ctx.fillStyle = gr; ctx.fill();
  ctx.beginPath(); ctx.moveTo(pts[0][0], pts[0][1]);
  pts.slice(1).forEach(([x, y]) => ctx.lineTo(x, y));
  ctx.strokeStyle = up ? '#00e87a' : '#ff4d6a'; ctx.lineWidth = 1.5; ctx.stroke();
}
 
// ── Controls ───────────────────────────────────────────────────────────
async function toggleSim()   { await fetch('/api/sim/toggle', { method: 'POST' }); }
async function resetSim()    { if (!confirm('Reset simulation?')) return; await fetch('/api/sim/reset', { method: 'POST' }); eq = [{ ts: Date.now()/1e3, v: 100 }]; }
async function refreshMkts() { set('cnt-s','…','var(--amber)'); await fetch('/api/refresh', { method: 'POST' }); }
</script>
</body>
</html>"""


if __name__ == "__main__":
    print("=" * 60)
    print("  Polymarket Paper Bot v3.1")
    print(f"  Short-term markets: 3–120 minutes")
    print("  Dashboard → http://localhost:5001")
    print("=" * 60)
    app.run(debug=False, port=5001, threaded=True)