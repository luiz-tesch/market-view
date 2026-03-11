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
SYMBOLS = ["BTC", "ETH", "SOL", "BNB", "MATIC", "DOGE"]
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
<link href="https://fonts.googleapis.com/css2?family=DM+Mono:ital,wght@0,300;0,400;0,500;1,300&family=DM+Sans:wght@300;400;500&display=swap" rel="stylesheet">
<style>
/* ── Reset & tokens ─────────────────────────────────────────────────────── */
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
:root {
  --bg:          #0a0b0d;
  --surface:     #0f1115;
  --surface2:    #13161b;
  --border:      #1c2028;
  --border-soft: #161a20;
  --text:        #6b7a8d;
  --text-mid:    #8fa0b5;
  --text-hi:     #c8d8e8;
  --green:       #34d67a;
  --green-dim:   rgba(52,214,122,.08);
  --red:         #f05252;
  --red-dim:     rgba(240,82,82,.08);
  --blue:        #4b9cf5;
  --blue-dim:    rgba(75,156,245,.07);
  --amber:       #f0a030;
  --purple:      #a374f0;
  --mono:        'DM Mono', monospace;
  --sans:        'DM Sans', sans-serif;
  --r:           4px;
  --scrollbar:   3px;
}
html, body { height: 100%; overflow: hidden; background: var(--bg); color: var(--text); font-family: var(--sans); font-size: 12px; line-height: 1.5; }

/* ── Scrollbar ──────────────────────────────────────────────────────────── */
::-webkit-scrollbar { width: var(--scrollbar); height: var(--scrollbar); }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { background: var(--border); border-radius: 2px; }
::-webkit-scrollbar-thumb:hover { background: #2a3040; }

/* ── Layout ─────────────────────────────────────────────────────────────── */
.root   { display: grid; grid-template-rows: 44px 1fr; height: 100vh; }
.body   { display: grid; grid-template-columns: 200px 1fr 260px; height: 100%; overflow: hidden; min-height: 0; }
.col    { display: flex; flex-direction: column; border-right: 1px solid var(--border); min-height: 0; overflow: hidden; transition: width .25s ease; }
.col:last-child { border-right: none; }
.col.hidden { width: 0 !important; overflow: hidden; border: none; }
.scroll { overflow-y: auto; overflow-x: hidden; flex: 1; min-height: 0; }

/* ── Topbar ─────────────────────────────────────────────────────────────── */
.topbar {
  display: flex; align-items: center; gap: 8px; padding: 0 14px;
  background: var(--surface); border-bottom: 1px solid var(--border);
  flex-shrink: 0;
}
.brand {
  font-family: var(--mono); font-size: 11px; font-weight: 500;
  color: var(--text-hi); letter-spacing: .12em; margin-right: 4px;
}
.dot { width: 5px; height: 5px; border-radius: 50%; background: var(--text); flex-shrink: 0; }
.dot.live { background: var(--green); box-shadow: 0 0 5px var(--green); animation: pulse 1.6s infinite; }
.dot.mock { background: var(--amber); }
@keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.25} }

.pill {
  font-family: var(--mono); font-size: 8px; font-weight: 500; letter-spacing: .08em;
  padding: 2px 7px; border-radius: 2px; border: 1px solid;
}
.pill-green { background: var(--green-dim); color: var(--green); border-color: rgba(52,214,122,.2); }
.pill-amber { background: rgba(240,160,48,.07); color: var(--amber); border-color: rgba(240,160,48,.18); }
.pill-purple{ background: rgba(163,116,240,.07); color: var(--purple); border-color: rgba(163,116,240,.18); }

.topbar-right { display: flex; gap: 16px; align-items: center; margin-left: auto; }
.tm { display: flex; flex-direction: column; align-items: flex-end; }
.tm-l { font-size: 8px; color: var(--text); letter-spacing: .1em; text-transform: uppercase; }
.tm-v { font-family: var(--mono); font-size: 13px; font-weight: 500; letter-spacing: -.01em; color: var(--text-hi); }
.tm-v.up  { color: var(--green); }
.tm-v.dn  { color: var(--red); }
.tm-v.neu { color: var(--blue); }

.topbar-actions { display: flex; gap: 5px; align-items: center; margin-left: 8px; }
.btn {
  font-family: var(--mono); font-size: 9px; letter-spacing: .1em; text-transform: uppercase;
  padding: 4px 11px; border-radius: var(--r); cursor: pointer; border: 1px solid;
  background: transparent; transition: all .12s;
}
.btn-run   { border-color: rgba(52,214,122,.3);  color: var(--green); }
.btn-run:hover   { background: var(--green-dim); }
.btn-stop  { border-color: rgba(240,82,82,.3);   color: var(--red); }
.btn-stop:hover  { background: var(--red-dim); }
.btn-ghost { border-color: var(--border); color: var(--text); }
.btn-ghost:hover { border-color: var(--text-mid); color: var(--text-hi); }

/* Panel toggle buttons */
.panel-toggles { display: flex; gap: 3px; align-items: center; }
.ptog {
  font-family: var(--mono); font-size: 8px; letter-spacing: .08em;
  padding: 3px 8px; border-radius: 2px; cursor: pointer;
  border: 1px solid var(--border); color: var(--text); background: transparent;
  transition: all .12s; text-transform: uppercase;
}
.ptog.active { border-color: rgba(75,156,245,.3); color: var(--blue); background: var(--blue-dim); }
.ptog:hover:not(.active) { border-color: var(--text); color: var(--text-mid); }

/* ── Column headers ─────────────────────────────────────────────────────── */
.col-head {
  font-family: var(--mono); font-size: 8px; font-weight: 500; letter-spacing: .14em;
  color: var(--text); padding: 8px 12px; border-bottom: 1px solid var(--border);
  background: var(--surface); flex-shrink: 0; display: flex; align-items: center;
  justify-content: space-between; text-transform: uppercase;
}
.col-head-sub { font-size: 8px; color: var(--text); font-weight: 400; letter-spacing: 0; font-family: var(--sans); }

/* ── Signal blocks ──────────────────────────────────────────────────────── */
.sig-block {
  padding: 10px 12px; border-bottom: 1px solid var(--border-soft);
  transition: background .1s;
}
.sig-block:hover { background: rgba(255,255,255,.01); }
.sig-row { display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 5px; }
.sig-sym  { font-family: var(--mono); font-size: 14px; font-weight: 500; color: var(--text-hi); }
.sig-price{ font-family: var(--mono); font-size: 12px; color: var(--blue); }
.spark-wrap { height: 24px; margin-bottom: 6px; }
canvas.spark { width: 100%; height: 24px; display: block; }
.sig-chips { display: flex; gap: 3px; flex-wrap: wrap; margin-bottom: 5px; }
.chip {
  font-family: var(--mono); font-size: 7px; font-weight: 500; letter-spacing: .06em;
  padding: 2px 5px; border-radius: 2px; border: 1px solid;
}
.chip-yes  { background: var(--green-dim); color: var(--green); border-color: rgba(52,214,122,.2); }
.chip-no   { background: var(--red-dim);   color: var(--red);   border-color: rgba(240,82,82,.2); }
.chip-skip { background: transparent; color: var(--text); border-color: var(--border); }
.chip-hi   { background: var(--blue-dim);  color: var(--blue);  border-color: rgba(75,156,245,.2); }
.chip-med  { background: rgba(240,160,48,.06); color: var(--amber); border-color: rgba(240,160,48,.18); }
.chip-lo   { background: transparent; color: var(--text); border-color: var(--border); }
.chip-edge { background: rgba(163,116,240,.06); color: var(--purple); border-color: rgba(163,116,240,.18); }
.sig-inds { display: grid; grid-template-columns: 1fr 1fr; gap: 2px; }
.ind { padding: 3px 5px; background: var(--surface2); border-radius: 2px; }
.ind-l { font-size: 6px; color: var(--text); letter-spacing: .1em; text-transform: uppercase; margin-bottom: 1px; }
.ind-v { font-family: var(--mono); font-size: 9px; font-weight: 400; color: var(--text-hi); }
.up  { color: var(--green) !important; }
.dn  { color: var(--red) !important; }
.dim { color: var(--text) !important; }
.flash-up { animation: fg .35s; } .flash-dn { animation: fr .35s; }
@keyframes fg { 0%{color:var(--green)} 100%{} }
@keyframes fr { 0%{color:var(--red)} 100%{} }

/* ── Market list ────────────────────────────────────────────────────────── */
.mkt-tabs { display: flex; border-bottom: 1px solid var(--border); flex-shrink: 0; }
.tab {
  font-family: var(--mono); font-size: 7px; letter-spacing: .12em; text-transform: uppercase;
  padding: 6px 10px; cursor: pointer; color: var(--text);
  border-bottom: 2px solid transparent; transition: all .12s;
}
.tab.on { color: var(--blue); border-bottom-color: var(--blue); }
.tab-c { font-size: 7px; margin-left: 2px; color: var(--purple); }
.mkt-hdr {
  display: grid; grid-template-columns: 1fr 44px 36px 32px; gap: 3px;
  padding: 5px 10px; font-size: 7px; letter-spacing: .1em; color: var(--text);
  text-transform: uppercase; background: var(--surface); border-bottom: 1px solid var(--border);
  position: sticky; top: 0; z-index: 5;
}
.mkt-row {
  display: grid; grid-template-columns: 1fr 44px 36px 32px; gap: 3px;
  padding: 7px 10px; border-bottom: 1px solid var(--border-soft); align-items: start;
  transition: background .1s;
}
.mkt-row:hover { background: rgba(255,255,255,.01); }
.mkt-q { font-size: 8px; color: var(--text-hi); line-height: 1.35; }
.mkt-meta { font-size: 7px; color: var(--text); margin-top: 2px; display: flex; gap: 4px; }
.mkt-meta .sym { color: var(--blue); }
.mkt-p { font-family: var(--mono); font-size: 10px; text-align: right; }
.mkt-t { font-family: var(--mono); font-size: 7px; color: var(--text); text-align: right; }
.mkt-sig { display: flex; justify-content: center; align-items: flex-start; padding-top: 1px; }
.vbar  { height: 2px; background: var(--border); margin-top: 3px; border-radius: 1px; }
.vfill { height: 100%; background: rgba(75,156,245,.2); }
.sdot { width: 4px; height: 4px; border-radius: 50%; background: var(--purple); display: inline-block; margin-right: 3px; vertical-align: middle; }

/* ── Center: stats + equity + trades ───────────────────────────────────── */
.stat-row {
  display: grid; grid-template-columns: repeat(6,1fr);
  border-bottom: 1px solid var(--border); flex-shrink: 0;
}
.stat { padding: 10px 12px; border-right: 1px solid var(--border); position: relative; }
.stat:last-child { border-right: none; }
.stat::after {
  content: ''; position: absolute; bottom: 0; left: 0; right: 0; height: 2px; border-radius: 0;
}
.s-g::after { background: var(--green); }
.s-b::after { background: var(--blue); }
.s-a::after { background: var(--amber); }
.s-r::after { background: var(--red); }
.s-d::after { background: var(--border); }
.stat-l  { font-size: 7px; letter-spacing: .12em; color: var(--text); text-transform: uppercase; margin-bottom: 3px; }
.stat-v  { font-family: var(--mono); font-size: 17px; font-weight: 500; line-height: 1; color: var(--text-hi); }
.s-g .stat-v { color: var(--green); }
.s-b .stat-v { color: var(--blue); }
.s-a .stat-v { color: var(--amber); }
.s-r .stat-v { color: var(--red); }
.s-d .stat-v { color: var(--text); }
.stat-sub { font-size: 7px; color: var(--text); margin-top: 2px; font-family: var(--mono); }

/* warmup banner */
.warmup-bar {
  display: none; padding: 6px 14px; background: rgba(240,160,48,.06);
  border-bottom: 1px solid rgba(240,160,48,.15); font-family: var(--mono);
  font-size: 9px; color: var(--amber); letter-spacing: .06em; flex-shrink: 0;
  align-items: center; gap: 8px;
}
.warmup-bar.show { display: flex; }
.wu-prog { flex: 1; height: 2px; background: rgba(240,160,48,.15); border-radius: 1px; overflow: hidden; }
.wu-fill { height: 100%; background: var(--amber); transition: width .5s linear; }

/* equity */
.eq-wrap { padding: 10px 14px; border-bottom: 1px solid var(--border); flex-shrink: 0; }
.eq-head { display: flex; justify-content: space-between; align-items: center;
  font-size: 8px; color: var(--text); letter-spacing: .1em; text-transform: uppercase; margin-bottom: 6px; }
.eq-sub  { font-family: var(--mono); font-size: 8px; color: var(--text); letter-spacing: 0; }

/* positions */
.pos-head {
  font-family: var(--mono); font-size: 8px; letter-spacing: .12em; color: var(--text);
  text-transform: uppercase; padding: 8px 12px; border-bottom: 1px solid var(--border);
  background: var(--surface); flex-shrink: 0; display: flex; justify-content: space-between;
}
.pos-cnt { color: var(--blue); font-size: 8px; }
.trade-card {
  padding: 8px 10px; border-bottom: 1px solid var(--border-soft);
  border-left: 2px solid transparent; animation: fadein .2s ease;
}
@keyframes fadein { from{opacity:0;transform:translateY(-2px)} to{opacity:1;transform:translateY(0)} }
.trade-card.yes  { border-left-color: var(--green); }
.trade-card.no   { border-left-color: var(--red); }
.trade-card.win  { border-left-color: var(--green); opacity: .5; }
.trade-card.loss { border-left-color: var(--red);   opacity: .45; }
.t-top { display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 2px; }
.t-sym { font-family: var(--mono); font-size: 11px; font-weight: 500; color: var(--text-hi); }
.t-pnl { font-family: var(--mono); font-size: 10px; }
.t-q   { font-size: 8px; color: var(--text-mid); line-height: 1.35; margin-bottom: 4px; }
.t-tags { display: flex; gap: 3px; flex-wrap: wrap; }
.tag {
  font-family: var(--mono); font-size: 7px; padding: 1px 5px;
  border-radius: 2px; border: 1px solid;
}
.tag-yes  { background: var(--green-dim); color: var(--green); border-color: rgba(52,214,122,.2); }
.tag-no   { background: var(--red-dim);   color: var(--red);   border-color: rgba(240,82,82,.2); }
.tag-open { background: var(--blue-dim);  color: var(--blue);  border-color: rgba(75,156,245,.18); }
.tag-cls  { background: transparent; color: var(--text); border-color: var(--border); }
.tag-n    { background: transparent; color: var(--text); border-color: var(--border); }
.tag-fee  { background: rgba(240,160,48,.05); color: var(--amber); border-color: rgba(240,160,48,.15); }
.prog     { height: 2px; background: var(--border); margin-top: 5px; overflow: hidden; border-radius: 1px; }
.prog-f   { height: 100%; background: rgba(75,156,245,.4); transition: width 1s linear; }

/* ── Log ────────────────────────────────────────────────────────────────── */
.log-wrap { padding: 4px 8px; display: flex; flex-direction: column; gap: 1px; }
.log-e {
  font-family: var(--mono); font-size: 8px; line-height: 1.5;
  padding: 3px 7px; border-left: 2px solid var(--border);
  animation: fadein .2s ease;
}
.le-open  { border-left-color: var(--blue); }
.le-win   { border-left-color: var(--green); background: rgba(52,214,122,.02); }
.le-loss  { border-left-color: var(--red);   background: rgba(240,82,82,.02); }
.le-start { border-left-color: var(--amber); }
.le-skip  { border-left-color: transparent; opacity: .4; }
.le-real  { border-left-color: var(--purple); }
.le-ts    { color: var(--text); margin-right: 5px; }
.le-ev    { font-weight: 500; margin-right: 5px; color: var(--text-mid); }
.le-msg   { color: var(--text); }

/* ── Empty ──────────────────────────────────────────────────────────────── */
.empty {
  padding: 20px 12px; text-align: center; color: var(--text);
  font-size: 9px; letter-spacing: .06em; line-height: 2.4;
}
</style>
</head>
<body>
<div class="root">

<!-- ── TOPBAR ─────────────────────────────────────────────────────────── -->
<div class="topbar">
  <span class="dot live" id="status-dot"></span>
  <span class="brand">POLYMARKET</span>
  <span class="pill pill-amber" id="pill-bin">BIN MOCK</span>
  <span class="pill pill-amber" id="pill-clob">CLOB MOCK</span>
  <span class="pill pill-purple" id="pill-short" style="display:none">0 SHORT</span>

  <div class="topbar-right">
    <div class="tm"><span class="tm-l">Balance</span><span class="tm-v up" id="n-bal">$100</span></div>
    <div class="tm"><span class="tm-l">P&L</span><span class="tm-v" id="n-pnl">$0</span></div>
    <div class="tm"><span class="tm-l">Win Rate</span><span class="tm-v neu" id="n-wr">—</span></div>
    <div class="tm"><span class="tm-l">Fees</span><span class="tm-v" style="color:var(--amber)" id="n-fees">$0</span></div>
    <div class="tm"><span class="tm-l">Latency</span><span class="tm-v" id="n-lat">—</span></div>

    <div class="panel-toggles">
      <button class="ptog active" id="ptog-signals"  onclick="togglePanel('signals')">Signals</button>
      <button class="ptog active" id="ptog-markets"  onclick="togglePanel('markets')">Markets</button>
      <button class="ptog active" id="ptog-log"      onclick="togglePanel('log')">Log</button>
    </div>

    <div class="topbar-actions">
      <button class="btn btn-run"  id="btn-tog" onclick="toggleSim()">▶ Start</button>
      <button class="btn btn-ghost" onclick="resetSim()" title="Reset simulation">↺</button>
      <button class="btn btn-ghost" onclick="refreshMkts()" title="Refresh markets">⟳</button>
    </div>
  </div>
</div>

<!-- ── BODY ───────────────────────────────────────────────────────────── -->
<div class="body">

  <!-- LEFT: Signals + Market list -->
  <div class="col" id="col-signals">
    <div class="col-head">
      Signals
      <span class="col-head-sub" id="h-bin-status">mock</span>
    </div>
    <div class="scroll" id="spot"></div>

    <div id="market-panel">
      <div class="mkt-tabs">
        <div class="tab on" id="tab-s" onclick="setTab('short')">Short<span class="tab-c" id="cnt-s">0</span></div>
        <div class="tab"    id="tab-a" onclick="setTab('all')">All<span class="tab-c" id="cnt-a">0</span></div>
      </div>
      <div class="scroll" id="mkts" style="max-height:220px"></div>
    </div>
  </div>

  <!-- CENTER: Stats + Equity + Positions -->
  <div class="col" id="col-main" style="border-right:none">

    <!-- Warmup banner (FIX v3.1: surfacado do snapshot) -->
    <div class="warmup-bar" id="warmup-bar">
      <span id="wu-label">Warming up…</span>
      <div class="wu-prog"><div class="wu-fill" id="wu-fill" style="width:0%"></div></div>
    </div>

    <div class="stat-row">
      <div class="stat s-g"><div class="stat-l">Balance</div><div class="stat-v" id="s-bal">$100</div></div>
      <div class="stat s-b"><div class="stat-l">P&L</div><div class="stat-v" id="s-pnl">$0</div><div class="stat-sub" id="s-roi">0%</div></div>
      <div class="stat s-a"><div class="stat-l">Open</div><div class="stat-v" id="s-open">0</div><div class="stat-sub" id="s-unr"></div></div>
      <div class="stat s-g"><div class="stat-l">Wins</div><div class="stat-v" id="s-wins">0</div><div class="stat-sub" id="s-avgw"></div></div>
      <div class="stat s-r"><div class="stat-l">Losses</div><div class="stat-v" id="s-loss">0</div><div class="stat-sub" id="s-avgl"></div></div>
      <div class="stat s-d"><div class="stat-l">Max DD</div><div class="stat-v" id="s-dd">0%</div><div class="stat-sub" id="s-streak"></div></div>
    </div>

    <div class="eq-wrap">
      <div class="eq-head">
        Equity Curve
        <span class="eq-sub" id="eq-sub"></span>
      </div>
      <canvas id="eq" height="56"></canvas>
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
      Log
      <span class="col-head-sub" id="log-cnt">0 entries</span>
    </div>
    <div class="scroll">
      <div class="log-wrap" id="log"></div>
    </div>
  </div>

</div>
</div><!-- .root -->

<script>
// ── State ──────────────────────────────────────────────────────────────────────
let snap = {}, eq = [{ts: Date.now()/1e3, v: 100}];
let ph = {}, prevPx = {}, maxVol = 1, activeTab = 'short';
const SYMS = ['BTC','ETH','SOL','BNB','MATIC','DOGE'];
SYMS.forEach(s => ph[s] = []);

// Panel visibility state (persisted in sessionStorage)
const panelState = {
  signals: sessionStorage.getItem('p_signals') !== 'false',
  markets: sessionStorage.getItem('p_markets') !== 'false',
  log:     sessionStorage.getItem('p_log')     !== 'false',
};
// Map panel key → column id
const panelCols = { signals: 'col-signals', markets: 'col-signals', log: 'col-log' };

function togglePanel(key) {
  // 'signals' and 'markets' share the same column; treat them together
  if (key === 'markets') key = 'signals';
  panelState[key] = !panelState[key];
  sessionStorage.setItem('p_' + key, panelState[key]);
  const colId = panelCols[key];
  const col = document.getElementById(colId);
  const btn = document.getElementById('ptog-' + key);
  if (panelState[key]) {
    col.classList.remove('hidden');
    btn.classList.add('active');
  } else {
    col.classList.add('hidden');
    btn.classList.remove('active');
  }
  // Sync markets button since it shares the column with signals
  if (key === 'signals') {
    document.getElementById('ptog-markets').classList.toggle('active', panelState[key]);
  }
}

// Apply initial state
['signals','log'].forEach(k => {
  if (!panelState[k]) {
    document.getElementById(panelCols[k]).classList.add('hidden');
    document.getElementById('ptog-' + k).classList.remove('active');
    if (k === 'signals') document.getElementById('ptog-markets').classList.remove('active');
  }
});

// ── SSE ────────────────────────────────────────────────────────────────────────
(function conn() {
  const es = new EventSource('/stream');
  es.addEventListener('snapshot', e => apply(JSON.parse(e.data)));
  es.addEventListener('tick', e => {
    const d = JSON.parse(e.data);
    if (ph[d.sym]) { ph[d.sym].push(d.price); if (ph[d.sym].length > 80) ph[d.sym].shift(); }
    flashPx(d.sym, d.price);
  });
  es.onerror = () => { setTimeout(conn, 3000); es.close(); };
})();
setInterval(async () => { try { apply(await (await fetch('/api/snapshot')).json()); } catch {} }, 5000);

// ── Apply snapshot ────────────────────────────────────────────────────────────
function apply(d) {
  snap = d;
  const st = d.stats || {}, bal = st.balance ?? 100, pnl = st.total_pnl ?? 0;
  const w = st.wins ?? 0, l = st.losses ?? 0;
  const wr = w + l > 0 ? (w / (w + l) * 100).toFixed(0) + '%' : '—';

  // Topbar
  set('n-bal', '$' + bal.toFixed(2), bal >= 100 ? 'var(--green)' : 'var(--red)');
  setPnl('n-pnl', pnl);
  set('n-wr', wr, 'var(--blue)');
  set('n-fees', '$' + (st.total_fees || 0).toFixed(3), 'var(--amber)');
  const lat = d.feed?.clob_lat_ms ?? 0;
  set('n-lat', lat > 0 ? lat.toFixed(1) + 'ms' : '—',
      lat < 30 ? 'var(--green)' : lat < 100 ? 'var(--amber)' : 'var(--red)');

  // Feed pills
  const binLive = d.feed?.binance_live;
  const clobLive = d.feed?.clob_live;
  const pb = document.getElementById('pill-bin');
  pb.textContent = binLive ? 'BIN LIVE' : 'BIN MOCK';
  pb.className   = 'pill ' + (binLive ? 'pill-green' : 'pill-amber');
  const pc = document.getElementById('pill-clob');
  pc.textContent = clobLive ? 'CLOB LIVE' : 'CLOB MOCK';
  pc.className   = 'pill ' + (clobLive ? 'pill-green' : 'pill-amber');

  set('h-bin-status', binLive ? '● live' : '◌ mock', binLive ? 'var(--green)' : 'var(--text)');

  const sc = d.feed?.short_markets ?? 0;
  const ps = document.getElementById('pill-short');
  ps.textContent = sc + ' short';
  ps.style.display = sc > 0 ? 'inline' : 'none';

  // Top button
  const btn = document.getElementById('btn-tog');
  btn.textContent = d.running ? '■ Stop' : '▶ Start';
  btn.className   = 'btn ' + (d.running ? 'btn-stop' : 'btn-run');

  // FIX v3.1: warmup banner from top-level warmup_remaining field
  const wu = d.warmup_remaining ?? (d.stats?.warmup_remaining ?? 0);
  const wuBar = document.getElementById('warmup-bar');
  if (wu > 0) {
    wuBar.classList.add('show');
    const pct = Math.max(0, Math.min(100, (1 - wu / 30) * 100));
    document.getElementById('wu-label').textContent = `Warming up — ${wu}s remaining`;
    document.getElementById('wu-fill').style.width = pct + '%';
  } else {
    wuBar.classList.remove('show');
  }

  // Stats
  set('s-bal', '$' + bal.toFixed(0));
  const pe = document.getElementById('s-pnl');
  pe.textContent = (pnl >= 0 ? '+$' : '-$') + Math.abs(pnl).toFixed(2);
  pe.style.color = pnl >= 0 ? 'var(--green)' : 'var(--red)';
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

  renderSpot(d.signals || {}, d.buffers || {});
  renderMarkets(d.short_markets || [], d.all_markets || [], d.signals || {});
  renderTrades(ot, d.closed_trades || []);
  renderLog(d.event_log || []);
  set('log-cnt', (d.event_log || []).length + ' entries');
}

// ── Helpers ───────────────────────────────────────────────────────────────────
function set(id, v, c) { const e = document.getElementById(id); if (!e) return; e.textContent = v; if (c) e.style.color = c; }
function setPnl(id, v) { const e = document.getElementById(id); if (!e) return; e.textContent = (v >= 0 ? '+$' : '-$') + Math.abs(v).toFixed(2); e.style.color = v >= 0 ? 'var(--green)' : 'var(--red)'; }
function fPx(s, p) { if (!p) return '—'; if (s === 'BTC') return '$' + Math.round(p).toLocaleString(); if (s === 'ETH') return '$' + p.toFixed(1); return '$' + p.toFixed(3); }
function fNum(n) { if (!n) return '0'; if (n >= 1e6) return (n / 1e6).toFixed(1) + 'M'; if (n >= 1e3) return (n / 1e3).toFixed(0) + 'k'; return n.toFixed(0); }
function fTime(m) { if (m < 60) return Math.round(m) + 'm'; if (m < 1440) return (m / 60).toFixed(1) + 'h'; return (m / 1440).toFixed(1) + 'd'; }

function flashPx(sym, p) {
  const el = document.getElementById('px-' + sym); if (!el) return;
  const pr = prevPx[sym];
  if (pr && p !== pr) { el.classList.remove('flash-up', 'flash-dn'); void el.offsetWidth; el.classList.add(p > pr ? 'flash-up' : 'flash-dn'); }
  el.textContent = fPx(sym, p); prevPx[sym] = p;
}

function setTab(t) {
  activeTab = t;
  document.getElementById('tab-s').classList.toggle('on', t === 'short');
  document.getElementById('tab-a').classList.toggle('on', t === 'all');
  renderMarkets(snap.short_markets || [], snap.all_markets || [], snap.signals || {});
}

// ── Render: signals ───────────────────────────────────────────────────────────
function renderSpot(sigs, bufs) {
  const c = document.getElementById('spot');
  let h = '';
  for (const sym of SYMS) {
    const buf = bufs[sym] || {}, sig = sigs[sym] || {};
    const p = buf.price || ph[sym]?.at(-1);
    const dir = sig.direction || 'SKIP';
    const dc = dir === 'YES' ? 'chip-yes' : dir === 'NO' ? 'chip-no' : 'chip-skip';
    const cc = sig.confidence === 'high' ? 'chip-hi' : sig.confidence === 'medium' ? 'chip-med' : 'chip-lo';
    const m1 = sig.momentum_1m || 0, m5 = sig.momentum_5m || 0, r = sig.rsi || 50, vd = sig.vwap_dev || 0;
    h += `<div class="sig-block">
      <div class="sig-row"><span class="sig-sym">${sym}</span><span class="sig-price" id="px-${sym}">${fPx(sym, p)}</span></div>
      <div class="spark-wrap"><canvas class="spark" id="sp-${sym}" height="24"></canvas></div>
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

// ── Render: markets ───────────────────────────────────────────────────────────
function renderMarkets(short, all, sigs) {
  const c = document.getElementById('mkts');
  const markets = activeTab === 'short' ? short : all;
  if (!markets.length) {
    c.innerHTML = `<div class="empty">${activeTab === 'short' ? 'No short-term markets<br>(3–120 min)' : 'Loading…'}</div>`;
    return;
  }
  const sorted = [...markets].sort((a, b) => a.mins_left - b.mins_left);
  maxVol = Math.max(...markets.map(m => m.volume || 1), 1);
  let h = `<div class="mkt-hdr"><div>Market</div><div style="text-align:right">Prob</div><div style="text-align:center">Sig</div><div style="text-align:right">TTL</div></div>`;
  for (const m of sorted) {
    const p = m.yes_price || 0.5;
    const pc = p > 0.65 ? 'var(--green)' : p < 0.35 ? 'var(--red)' : 'var(--blue)';
    const sym = m.symbol === 'CRYPTO' ? 'BTC' : m.symbol;
    const sig = sigs[sym] || {};
    const dir = sig.direction || 'SKIP';
    const dc = dir === 'YES' ? 'chip chip-yes' : dir === 'NO' ? 'chip chip-no' : 'chip chip-skip';
    const ml = m.mins_left ?? 9999;
    const tc = ml < 30 ? 'var(--red)' : ml < 120 ? 'var(--amber)' : 'var(--text)';
    const vpct = Math.min(100, (m.volume || 0) / maxVol * 100);
    h += `<div class="mkt-row">
      <div>
        <div class="mkt-q">${m.is_short ? '<span class="sdot"></span>' : ''}${m.question}</div>
        <div class="mkt-meta"><span class="sym">${m.symbol}</span><span>$${fNum(m.volume)}</span></div>
        <div class="vbar"><div class="vfill" style="width:${vpct.toFixed(0)}%"></div></div>
      </div>
      <div class="mkt-p" style="color:${pc}">${(p * 100).toFixed(1)}%</div>
      <div class="mkt-sig"><span class="${dc}" style="font-size:6px">${dir}</span></div>
      <div class="mkt-t" style="color:${tc}">${fTime(ml)}</div>
    </div>`;
  }
  c.innerHTML = h;
}

// ── Render: trades ────────────────────────────────────────────────────────────
function renderTrades(open, closed) {
  const c = document.getElementById('trades');
  if (!open.length && !closed.length) {
    c.innerHTML = '<div class="empty">No positions yet.<br>Waiting for warm-up (30s).</div>';
    return;
  }
  const now = Date.now() / 1e3;
  function row(t, isOpen) {
    const pnl = isOpen ? t.unrealized_pnl : t.pnl_usdc;
    const pc = pnl == null ? 'var(--text-mid)' : pnl >= 0 ? 'var(--green)' : 'var(--red)';
    const ps = pnl == null ? '—' : (pnl >= 0 ? '+$' : '-$') + Math.abs(pnl).toFixed(2);
    const cls = isOpen ? (t.action === 'BUY_YES' ? 'yes' : 'no') : (t.won ? 'win' : 'loss');
    const prog = isOpen ? Math.min(100, (now - t.entry_ts) / (t.horizon_min * 60) * 100) : 100;
    return `<div class="trade-card ${cls}">
      <div class="t-top"><span class="t-sym">${t.symbol}</span><span class="t-pnl" style="color:${pc}">${isOpen ? '~' : ''} ${ps}</span></div>
      <div class="t-q">${t.question}</div>
      <div class="t-tags">
        <span class="tag ${t.action==='BUY_YES'?'tag-yes':'tag-no'}">${t.action==='BUY_YES'?'↑ YES':'↓ NO'}</span>
        <span class="tag ${isOpen?'tag-open':'tag-cls'}">${isOpen ? 'OPEN' : t.exit_reason || 'CLOSED'}</span>
        <span class="tag tag-n">$${(t.gross_usdc || t.amount_usdc).toFixed(2)}</span>
        <span class="tag tag-fee">fee $${(t.fee_usdc || 0).toFixed(4)}</span>
        <span class="tag tag-n">${(t.entry_price * 100).toFixed(1)}%</span>
        <span class="tag tag-n">e${(t.signal_edge * 100).toFixed(1)}%</span>
      </div>
      ${isOpen ? `<div class="prog"><div class="prog-f" style="width:${prog.toFixed(0)}%"></div></div>` : ''}
    </div>`;
  }
  c.innerHTML = open.map(t => row(t, true)).join('') +
    [...closed].reverse().slice(0, 30).map(t => row(t, false)).join('');
}

// ── Render: log ───────────────────────────────────────────────────────────────
function renderLog(logs) {
  const c = document.getElementById('log');
  c.innerHTML = [...logs].reverse().slice(0, 100).map(e => {
    const cls = e.event === 'TRADE_OPEN' ? 'le-open' :
      e.level === 'win' ? 'le-win' : e.level === 'loss' ? 'le-loss' :
      e.event?.includes('START') || e.event?.includes('RESET') ? 'le-start' :
      e.event === 'SKIP' ? 'le-skip' :
      e.event === 'RESOLVE_REAL' ? 'le-real' : '';
    return `<div class="log-e ${cls}"><span class="le-ts">${e.ts_str}</span><span class="le-ev">${e.event}</span><span class="le-msg">${e.message}</span></div>`;
  }).join('');
}

// ── Canvas: equity ────────────────────────────────────────────────────────────
function drawEq() {
  const cv = document.getElementById('eq'); if (!cv || eq.length < 2) return;
  const w = cv.offsetWidth || 600, h = 56; cv.width = w; cv.height = h;
  const ctx = cv.getContext('2d'); ctx.clearRect(0, 0, w, h);
  const vals = eq.map(p => p.v), mn = Math.min(...vals) * .997, mx = Math.max(...vals) * 1.003, rng = mx - mn || 1;
  const pts = vals.map((v, i) => [i / (vals.length - 1) * w, h - ((v - mn) / rng * (h - 5) + 2)]);
  const cur = vals.at(-1), up = cur >= 100;
  const col = up ? 'var(--green)' : 'var(--red)';
  // baseline
  const bl = h - ((100 - mn) / rng * (h - 5) + 2);
  ctx.beginPath(); ctx.moveTo(0, bl); ctx.lineTo(w, bl);
  ctx.strokeStyle = 'rgba(255,255,255,.04)'; ctx.lineWidth = 1;
  ctx.setLineDash([3, 4]); ctx.stroke(); ctx.setLineDash([]);
  // fill
  const gr = ctx.createLinearGradient(0, 0, 0, h);
  gr.addColorStop(0, up ? 'rgba(52,214,122,.10)' : 'rgba(240,82,82,.10)');
  gr.addColorStop(1, 'rgba(0,0,0,0)');
  ctx.beginPath(); ctx.moveTo(pts[0][0], pts[0][1]);
  pts.slice(1).forEach(([x, y]) => ctx.lineTo(x, y));
  ctx.lineTo(w, h); ctx.lineTo(0, h); ctx.closePath(); ctx.fillStyle = gr; ctx.fill();
  // line
  ctx.beginPath(); ctx.moveTo(pts[0][0], pts[0][1]);
  pts.slice(1).forEach(([x, y]) => ctx.lineTo(x, y));
  ctx.strokeStyle = up ? '#34d67a' : '#f05252'; ctx.lineWidth = 1.5; ctx.stroke();
  // label
  ctx.fillStyle = up ? '#34d67a' : '#f05252';
  ctx.font = '400 9px DM Mono'; ctx.fillText('$' + cur.toFixed(2), w - 56, pts.at(-1)[1] - 4);
}

// ── Canvas: sparkline ─────────────────────────────────────────────────────────
function drawSpark(cv, prices) {
  const w = cv.offsetWidth || 188, h = 24; cv.width = w; cv.height = h;
  const ctx = cv.getContext('2d'); ctx.clearRect(0, 0, w, h);
  if (prices.length < 2) return;
  const mn = Math.min(...prices), mx = Math.max(...prices), rng = mx - mn || 1;
  const pts = prices.map((p, i) => [i / (prices.length - 1) * w, h - ((p - mn) / rng * (h - 2) + 1)]);
  const up = pts.at(-1)[1] <= pts[0][1];
  const gr = ctx.createLinearGradient(0, 0, 0, h);
  gr.addColorStop(0, up ? 'rgba(52,214,122,.15)' : 'rgba(240,82,82,.15)');
  gr.addColorStop(1, 'rgba(0,0,0,0)');
  ctx.beginPath(); ctx.moveTo(pts[0][0], pts[0][1]);
  pts.slice(1).forEach(([x, y]) => ctx.lineTo(x, y));
  ctx.lineTo(w, h); ctx.lineTo(0, h); ctx.closePath(); ctx.fillStyle = gr; ctx.fill();
  ctx.beginPath(); ctx.moveTo(pts[0][0], pts[0][1]);
  pts.slice(1).forEach(([x, y]) => ctx.lineTo(x, y));
  ctx.strokeStyle = up ? '#34d67a' : '#f05252'; ctx.lineWidth = 1.5; ctx.stroke();
}

// ── Controls ──────────────────────────────────────────────────────────────────
async function toggleSim()  { await fetch('/api/sim/toggle', { method: 'POST' }); }
async function resetSim()   { if (!confirm('Reset simulation?')) return; await fetch('/api/sim/reset', { method: 'POST' }); eq = [{ ts: Date.now() / 1e3, v: 100 }]; }
async function refreshMkts(){ set('cnt-s', '…', 'var(--amber)'); await fetch('/api/refresh', { method: 'POST' }); }
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