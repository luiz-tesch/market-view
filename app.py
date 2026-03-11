"""
Polymarket Paper Bot v3 — Mercados Reais + Simulação Fiel
Run: python app.py
Open: http://localhost:5001

Melhorias v3:
- Foco em mercados SHORT-TERM (3–120 min)
- enrich_prices em background (não bloqueia startup)
- Painel mostra separadamente: todos os mercados vs. só short-term
- Badges de feed live/mock atualizados
- CLOB resolve real integrado ao simulador
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
from src.execution.simulator import TradingSimulator

app = Flask(__name__)

# ── Globals ────────────────────────────────────────────────────────────────────
SYMBOLS = ["BTC", "ETH", "SOL", "BNB", "MATIC", "DOGE"]
price_buffers: dict[str, PriceBuffer] = {s: PriceBuffer(maxlen=400) for s in SYMBOLS}
simulator    = TradingSimulator(price_buffers)
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
        "signals":       signals,
        "buffers":       bufinfo,
        "all_markets":   [m.to_dict() for m in all_markets],
        "short_markets": [m.to_dict() for m in short_markets],
        "feed": {
            "binance_live": IS_LIVE_BIN,
            "clob_live":    IS_LIVE_CLOB,
            "clob_lat_ms":  round(lat, 1),
            "clob_msgs":    msgs,
            "total_markets":    len(all_markets),
            "short_markets":    len(short_markets),
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

    # Enriquece preços em BACKGROUND (não bloqueia)
    enrich_prices_background(markets)

    all_markets   = markets
    short_markets = [m for m in markets if m.is_short_term()]
    print(f"[App] {len(all_markets)} mercados totais | {len(short_markets)} short-term")

    # Registra short-term no simulador
    for m in short_markets:
        if m.symbol in price_buffers:
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

    # 1. Mercados (rápido — enrich em background)
    _load_markets()
    time.sleep(0.3)

    # 2. Binance feed
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

    # 3. CLOB WebSocket
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

    # 4. Aguarda buffers aquecerem
    time.sleep(2)
    simulator.start()
    print("[App] ✓ Simulador iniciado (foco short-term)")

    # 5. Pusher periódico
    def pusher():
        while True:
            time.sleep(2)
            try:
                _sse("snapshot", _snap())
            except Exception:
                pass

    threading.Thread(target=pusher, daemon=True).start()

    # 6. Refresh periódico de short-term markets (a cada 5 min)
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
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Polymarket Bot</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600&family=JetBrains+Mono:wght@400;500;700&display=swap" rel="stylesheet">
<style>
:root {
  --bg: #0c0e10;
  --surface: #13161a;
  --border: #1e2328;
  --border-soft: #191d22;
  --text: #8a9bb0;
  --text-dim: #3d4f62;
  --text-bright: #d4e4f4;
  --green: #22c55e;
  --red: #ef4444;
  --blue: #3b82f6;
  --amber: #f59e0b;
  --purple: #a855f7;
  --green-dim: rgba(34,197,94,.1);
  --red-dim: rgba(239,68,68,.1);
  --blue-dim: rgba(59,130,246,.08);
  --mono: 'JetBrains Mono', monospace;
  --sans: 'Inter', sans-serif;
}
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
html, body { height: 100%; overflow: hidden; }
body { background: var(--bg); color: var(--text); font-family: var(--sans); font-size: 12px; line-height: 1.5; }

/* ─── Layout ─────────────────────────────────────────────── */
.root { display: grid; grid-template-rows: 48px 1fr; height: 100vh; }
.topbar {
  display: flex; align-items: center; padding: 0 16px; gap: 10px;
  background: var(--surface); border-bottom: 1px solid var(--border);
}
.brand { font-family: var(--mono); font-size: 12px; font-weight: 700; color: var(--text-bright); letter-spacing: .08em; margin-right: 4px; }
.badge { font-family: var(--mono); font-size: 9px; font-weight: 500; padding: 2px 7px; border-radius: 3px; letter-spacing: .06em; }
.b-live { background: rgba(34,197,94,.12); color: var(--green); border: 1px solid rgba(34,197,94,.2); }
.b-mock { background: rgba(245,158,11,.1); color: var(--amber); border: 1px solid rgba(245,158,11,.18); }
.b-short { background: rgba(168,85,247,.1); color: var(--purple); border: 1px solid rgba(168,85,247,.18); }
.topbar-metrics { display: flex; gap: 20px; margin-left: auto; align-items: center; }
.tm { display: flex; flex-direction: column; align-items: flex-end; gap: 1px; }
.tm-label { font-size: 9px; color: var(--text-dim); letter-spacing: .1em; text-transform: uppercase; }
.tm-val { font-family: var(--mono); font-size: 13px; font-weight: 700; letter-spacing: -.01em; }
.tm-val.up { color: var(--green); } .tm-val.dn { color: var(--red); } .tm-val.neu { color: var(--blue); }
.topbar-btns { display: flex; gap: 6px; margin-left: 8px; }
.btn { font-family: var(--mono); font-size: 9px; font-weight: 600; letter-spacing: .08em; padding: 5px 12px; border-radius: 3px; cursor: pointer; border: 1px solid; background: transparent; transition: all .15s; text-transform: uppercase; }
.btn-start { border-color: rgba(34,197,94,.3); color: var(--green); }
.btn-start:hover { background: var(--green-dim); }
.btn-stop  { border-color: rgba(239,68,68,.3); color: var(--red); }
.btn-stop:hover  { background: var(--red-dim); }
.btn-reset { border-color: var(--border); color: var(--text-dim); }
.btn-reset:hover { border-color: var(--text-dim); color: var(--text); }
.btn-ref   { border-color: rgba(59,130,246,.25); color: rgba(59,130,246,.7); }
.btn-ref:hover { border-color: var(--blue); color: var(--blue); }
.pulsedot { width: 6px; height: 6px; border-radius: 50%; background: var(--green); box-shadow: 0 0 6px var(--green); animation: pulse 1.4s infinite; }
@keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.2} }

/* ─── Main grid ──────────────────────────────────────────── */
.body { display: grid; grid-template-columns: 220px 1fr 280px; height: 100%; overflow: hidden; }
.col { border-right: 1px solid var(--border); overflow: hidden; display: flex; flex-direction: column; }
.col:last-child { border-right: none; }
.col-scroll { overflow-y: auto; overflow-x: hidden; flex: 1; }
.col-scroll::-webkit-scrollbar { width: 3px; }
.col-scroll::-webkit-scrollbar-track { background: transparent; }
.col-scroll::-webkit-scrollbar-thumb { background: var(--border); border-radius: 2px; }
.col-head { font-family: var(--mono); font-size: 9px; font-weight: 600; letter-spacing: .14em; color: var(--text-dim); padding: 9px 12px; border-bottom: 1px solid var(--border); background: var(--surface); flex-shrink: 0; display: flex; align-items: center; justify-content: space-between; text-transform: uppercase; }
.col-sub { font-size: 9px; color: var(--text-dim); font-weight: 400; font-family: var(--sans); letter-spacing: 0; }

/* ─── Spot signals ───────────────────────────────────────── */
.spot-block { padding: 10px 12px; border-bottom: 1px solid var(--border-soft); }
.spot-row { display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 6px; }
.spot-sym { font-family: var(--mono); font-size: 15px; font-weight: 700; color: var(--text-bright); }
.spot-price { font-family: var(--mono); font-size: 13px; font-weight: 500; color: var(--blue); }
.spark-wrap { height: 26px; margin-bottom: 7px; }
canvas.spark { width: 100%; height: 26px; display: block; }
.chips { display: flex; gap: 4px; flex-wrap: wrap; margin-bottom: 6px; }
.chip { font-family: var(--mono); font-size: 8px; font-weight: 600; padding: 2px 6px; border-radius: 2px; letter-spacing: .04em; }
.chip-yes { background: var(--green-dim); color: var(--green); border: 1px solid rgba(34,197,94,.2); }
.chip-no  { background: var(--red-dim);   color: var(--red);   border: 1px solid rgba(239,68,68,.2); }
.chip-skip{ background: rgba(30,35,40,.6); color: var(--text-dim); border: 1px solid var(--border); }
.chip-hi  { background: var(--blue-dim);  color: var(--blue);  border: 1px solid rgba(59,130,246,.18); }
.chip-med { background: rgba(245,158,11,.07); color: var(--amber); border: 1px solid rgba(245,158,11,.18); }
.chip-lo  { background: rgba(30,35,40,.4); color: var(--text-dim); border: 1px solid var(--border); }
.chip-edge{ background: rgba(168,85,247,.07); color: var(--purple); border: 1px solid rgba(168,85,247,.18); }
.chip-type{ background: rgba(59,130,246,.05); color: #60a5fa; border: 1px solid rgba(59,130,246,.12); }
.inds { display: grid; grid-template-columns: 1fr 1fr; gap: 2px; }
.ind { background: var(--surface); padding: 3px 6px; border-radius: 2px; }
.ind-l { font-size: 7px; color: var(--text-dim); letter-spacing: .1em; text-transform: uppercase; margin-bottom: 1px; }
.ind-v { font-family: var(--mono); font-size: 10px; font-weight: 500; color: var(--text-bright); }
.up { color: var(--green)!important; } .dn { color: var(--red)!important; } .dim { color: var(--text-dim)!important; }
.flash-up { animation: flash-g .4s ease; } .flash-dn { animation: flash-r .4s ease; }
@keyframes flash-g { 0%{color:var(--green)} 100%{} } @keyframes flash-r { 0%{color:var(--red)} 100%{} }

/* ─── Market tabs & list ─────────────────────────────────── */
.tabs { display: flex; border-bottom: 1px solid var(--border); flex-shrink: 0; background: var(--surface); }
.tab { font-family: var(--mono); font-size: 8px; font-weight: 600; letter-spacing: .1em; padding: 7px 12px; cursor: pointer; color: var(--text-dim); border-bottom: 2px solid transparent; transition: .12s; text-transform: uppercase; }
.tab.active { color: var(--blue); border-bottom-color: var(--blue); }
.tab-n { font-size: 8px; margin-left: 3px; color: var(--purple); }
.mkt-hdr, .mkt-row { display: grid; grid-template-columns: 1fr 50px 42px 38px; gap: 4px; padding: 6px 10px; border-bottom: 1px solid var(--border-soft); align-items: start; }
.mkt-hdr { background: var(--surface); position: sticky; top: 0; z-index: 5; font-size: 7px; letter-spacing: .1em; color: var(--text-dim); text-transform: uppercase; }
.mkt-row:hover { background: rgba(255,255,255,.01); }
.mkt-q { font-size: 8px; color: var(--text-bright); line-height: 1.35; }
.mkt-meta { font-size: 7px; color: var(--text-dim); margin-top: 2px; display: flex; gap: 5px; }
.mkt-p { font-family: var(--mono); font-size: 11px; font-weight: 600; text-align: right; }
.mkt-t { font-size: 7px; color: var(--text-dim); text-align: right; font-family: var(--mono); }
.mkt-sig { text-align: center; }
.short-dot { width: 4px; height: 4px; border-radius: 50%; background: var(--purple); display: inline-block; margin-right: 3px; vertical-align: middle; }
.vbar { height: 2px; background: var(--border); margin-top: 3px; border-radius: 1px; overflow: hidden; }
.vfill { height: 100%; background: rgba(59,130,246,.25); }

/* ─── Center ─────────────────────────────────────────────── */
.stat-strip { display: grid; grid-template-columns: repeat(6,1fr); border-bottom: 1px solid var(--border); flex-shrink: 0; }
.stat { padding: 10px 12px; border-right: 1px solid var(--border); position: relative; }
.stat:last-child { border-right: none; }
.stat::after { content:''; position: absolute; bottom: 0; left: 0; right: 0; height: 2px; border-radius: 0; }
.s-green::after{background:var(--green)} .s-blue::after{background:var(--blue)} .s-amber::after{background:var(--amber)}
.s-g2::after{background:var(--green)} .s-red::after{background:var(--red)} .s-dim::after{background:var(--text-dim)}
.stat-l { font-size: 7px; letter-spacing: .12em; color: var(--text-dim); text-transform: uppercase; margin-bottom: 3px; }
.stat-v { font-family: var(--mono); font-size: 18px; font-weight: 700; line-height: 1; }
.s-green .stat-v { color: var(--green); } .s-blue .stat-v { color: var(--blue); } .s-amber .stat-v { color: var(--amber); }
.s-g2 .stat-v { color: var(--green); } .s-red .stat-v { color: var(--red); } .s-dim .stat-v { color: var(--text-dim); }
.stat-sub { font-size: 7px; color: var(--text-dim); margin-top: 2px; font-family: var(--mono); }

.eq-wrap { padding: 10px 14px; border-bottom: 1px solid var(--border); flex-shrink: 0; }
.eq-head { font-size: 8px; color: var(--text-dim); letter-spacing: .1em; text-transform: uppercase; margin-bottom: 5px; display: flex; justify-content: space-between; align-items: center; }
.eq-sub { font-size: 8px; color: var(--text-dim); font-family: var(--mono); letter-spacing: 0; }

.pos-head { font-family: var(--mono); font-size: 9px; font-weight: 600; letter-spacing: .12em; color: var(--text-dim); padding: 9px 12px; border-bottom: 1px solid var(--border); background: var(--surface); flex-shrink: 0; display: flex; justify-content: space-between; text-transform: uppercase; }
.pos-cnt { font-size: 9px; color: var(--blue); font-weight: 500; }
.trade-card { padding: 8px 10px; border-bottom: 1px solid var(--border-soft); border-left: 2px solid transparent; animation: fadein .2s ease; }
@keyframes fadein { from{opacity:0;transform:translateY(-3px)} to{opacity:1;transform:translateY(0)} }
.trade-card.yes   { border-left-color: var(--green); }
.trade-card.no    { border-left-color: var(--red); }
.trade-card.win   { border-left-color: var(--green); opacity: .55; }
.trade-card.loss  { border-left-color: var(--red); opacity: .45; }
.t-top { display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 2px; }
.t-sym { font-family: var(--mono); font-size: 11px; font-weight: 700; color: var(--text-bright); }
.t-pnl { font-family: var(--mono); font-size: 11px; font-weight: 600; }
.t-q   { font-size: 8px; color: var(--text); line-height: 1.35; margin-bottom: 4px; }
.t-tags { display: flex; gap: 3px; flex-wrap: wrap; }
.tag { font-family: var(--mono); font-size: 7px; padding: 1px 5px; border-radius: 2px; border: 1px solid; }
.tag-yes  { background: var(--green-dim); color: var(--green); border-color: rgba(34,197,94,.2); }
.tag-no   { background: var(--red-dim);   color: var(--red);   border-color: rgba(239,68,68,.2); }
.tag-open { background: var(--blue-dim);  color: var(--blue);  border-color: rgba(59,130,246,.18); }
.tag-cls  { background: rgba(30,35,40,.5); color: var(--text-dim); border-color: var(--border); }
.tag-n    { background: var(--surface); color: var(--text-dim); border-color: var(--border); }
.tag-fee  { background: rgba(245,158,11,.06); color: var(--amber); border-color: rgba(245,158,11,.15); }
.prog     { height: 2px; background: var(--border); margin-top: 5px; overflow: hidden; border-radius: 1px; }
.prog-f   { height: 100%; background: rgba(59,130,246,.5); transition: width 1s linear; }

/* ─── Log ────────────────────────────────────────────────── */
.log-wrap { padding: 4px 8px; display: flex; flex-direction: column; gap: 2px; }
.log-e { font-size: 8px; line-height: 1.55; padding: 3px 7px; border-left: 2px solid var(--border); font-family: var(--mono); animation: fadein .2s ease; }
.le-open  { border-left-color: var(--blue); }
.le-win   { border-left-color: var(--green); background: rgba(34,197,94,.02); }
.le-loss  { border-left-color: var(--red);   background: rgba(239,68,68,.02); }
.le-start { border-left-color: var(--amber); }
.le-skip  { border-left-color: var(--border); opacity: .5; }
.le-real  { border-left-color: var(--purple); }
.le-ts    { color: var(--text-dim); margin-right: 5px; }
.le-ev    { font-weight: 600; margin-right: 5px; }
.le-msg   { color: var(--text); }

.empty { padding: 20px 12px; text-align: center; color: var(--text-dim); font-size: 9px; letter-spacing: .06em; line-height: 2.4; }
</style>
</head>
<body>
<div class="root">

<!-- TOPBAR -->
<div class="topbar">
  <div class="pulsedot" id="pulse"></div>
  <span class="brand">POLYMARKET BOT</span>
  <span class="badge b-mock" id="b-bin">BIN MOCK</span>
  <span class="badge b-mock" id="b-clob">CLOB MOCK</span>
  <span class="badge b-short" id="b-short" style="display:none">0 SHORT</span>
  <div class="topbar-metrics">
    <div class="tm"><span class="tm-label">Balance</span><span class="tm-val up" id="n-bal">$100</span></div>
    <div class="tm"><span class="tm-label">P&L</span><span class="tm-val" id="n-pnl">$0</span></div>
    <div class="tm"><span class="tm-label">Win Rate</span><span class="tm-val neu" id="n-wr">—</span></div>
    <div class="tm"><span class="tm-label">Fees</span><span class="tm-val" style="color:var(--amber)" id="n-fees">$0</span></div>
    <div class="tm"><span class="tm-label">Latency</span><span class="tm-val" id="n-lat">—</span></div>
  </div>
  <div class="topbar-btns">
    <button class="btn btn-start" id="btn-tog" onclick="toggleSim()">▶ Start</button>
    <button class="btn btn-reset" onclick="resetSim()">↺</button>
    <button class="btn btn-ref" onclick="refreshMkts()">⟳</button>
  </div>
</div>

<!-- BODY -->
<div class="body">

  <!-- LEFT: Signals + Markets -->
  <div class="col">
    <div class="col-head">Signals <span class="col-sub" id="h-bin">mock</span></div>
    <div class="col-scroll" id="spot"></div>
    <div class="tabs">
      <div class="tab active" id="tab-s" onclick="setTab('short')">Short-Term<span class="tab-n" id="cnt-s">0</span></div>
      <div class="tab" id="tab-a" onclick="setTab('all')">All<span class="tab-n" id="cnt-a">0</span></div>
    </div>
    <div class="col-scroll" id="mkts"></div>
  </div>

  <!-- CENTER: Stats + Equity + Positions -->
  <div class="col">
    <div class="stat-strip">
      <div class="stat s-green"><div class="stat-l">Balance</div><div class="stat-v" id="s-bal">$100</div></div>
      <div class="stat s-blue"><div class="stat-l">P&L</div><div class="stat-v" id="s-pnl">$0</div><div class="stat-sub" id="s-roi">0%</div></div>
      <div class="stat s-amber"><div class="stat-l">Open</div><div class="stat-v" id="s-open">0</div><div class="stat-sub" id="s-unr"></div></div>
      <div class="stat s-g2"><div class="stat-l">Wins</div><div class="stat-v" id="s-wins">0</div><div class="stat-sub" id="s-avgw"></div></div>
      <div class="stat s-red"><div class="stat-l">Losses</div><div class="stat-v" id="s-loss">0</div><div class="stat-sub" id="s-avgl"></div></div>
      <div class="stat s-dim"><div class="stat-l">Max DD</div><div class="stat-v" id="s-dd">0%</div><div class="stat-sub" id="s-streak"></div></div>
    </div>
    <div class="eq-wrap">
      <div class="eq-head">Equity Curve<span class="eq-sub" id="eq-sub"></span></div>
      <canvas id="eq" height="60"></canvas>
    </div>
    <div class="pos-head">Positions<span class="pos-cnt" id="pos-cnt"></span></div>
    <div class="col-scroll" id="trades"></div>
  </div>

  <!-- RIGHT: Log -->
  <div class="col">
    <div class="col-head">Activity Log<span class="col-sub" id="log-cnt"></span></div>
    <div class="col-scroll">
      <div class="log-wrap" id="log"></div>
    </div>
  </div>

</div>
</div>

<script>
let snap={}, eq=[{ts:Date.now()/1e3,v:100}], ph={}, prevPx={}, maxVol=1;
let activeTab='short';
const SYMS=['BTC','ETH','SOL','BNB','MATIC','DOGE'];
SYMS.forEach(s=>ph[s]=[]);

// SSE connection
(function conn(){
  const es=new EventSource('/stream');
  es.addEventListener('snapshot',e=>apply(JSON.parse(e.data)));
  es.addEventListener('tick',e=>{
    const d=JSON.parse(e.data);
    if(ph[d.sym]){ph[d.sym].push(d.price);if(ph[d.sym].length>80)ph[d.sym].shift();}
    flashPx(d.sym,d.price);
  });
  es.onerror=()=>{setTimeout(conn,3000);es.close();};
})();
setInterval(async()=>{try{apply(await(await fetch('/api/snapshot')).json())}catch{}},5000);

function setTab(t){
  activeTab=t;
  document.getElementById('tab-s').classList.toggle('active',t==='short');
  document.getElementById('tab-a').classList.toggle('active',t==='all');
  renderMarkets(snap.short_markets||[],snap.all_markets||[],snap.signals||{});
}

function apply(d){
  snap=d;
  const st=d.stats||{},bal=st.balance??100,pnl=st.total_pnl??0;
  const w=st.wins??0,l=st.losses??0;
  const wr=w+l>0?(w/(w+l)*100).toFixed(0)+'%':'—';

  // Topbar
  _set('n-bal','$'+bal.toFixed(2),bal>=100?'var(--green)':'var(--red)');
  _setPnl('n-pnl',pnl);
  _set('n-wr',wr,'var(--blue)');
  _set('n-fees','$'+(st.total_fees||0).toFixed(3),'var(--amber)');
  const lat=d.feed?.clob_lat_ms??0;
  _set('n-lat',lat>0?lat.toFixed(1)+'ms':'—',lat<30?'var(--green)':lat<100?'var(--amber)':'var(--red)');

  ['bin','clob'].forEach(k=>{
    const live=d.feed?.[k==='bin'?'binance_live':'clob_live'];
    const el=document.getElementById('b-'+k);
    el.textContent=live?`${k.toUpperCase()} LIVE`:`${k.toUpperCase()} MOCK`;
    el.className='badge '+(live?'b-live':'b-mock');
  });
  _set('h-bin',d.feed?.binance_live?'● live':'◌ mock', d.feed?.binance_live?'var(--green)':'var(--text-dim)');

  const sc=d.feed?.short_markets??0;
  const bs=document.getElementById('b-short');
  bs.textContent=`${sc} SHORT`;
  bs.style.display=sc>0?'inline':'none';

  const btn=document.getElementById('btn-tog');
  btn.textContent=d.running?'■ Stop':'▶ Start';
  btn.className='btn '+(d.running?'btn-stop':'btn-start');

  // Stats strip
  _set('s-bal','$'+bal.toFixed(0));
  const pe=document.getElementById('s-pnl');
  pe.textContent=(pnl>=0?'+$':'-$')+Math.abs(pnl).toFixed(2);
  pe.style.color=pnl>=0?'var(--green)':'var(--red)';
  _set('s-roi',(((bal-100)/100)*100).toFixed(1)+'% ROI');
  const ot=d.open_trades||[];
  _set('s-open',ot.length);
  const unr=ot.reduce((s,t)=>s+(t.unrealized_pnl||0),0);
  _set('s-unr',(unr>=0?'+':'')+'$'+Math.abs(unr).toFixed(2)+' unr');
  _set('s-wins',w); _set('s-loss',l);
  _set('s-avgw','avg $'+(st.avg_win_usdc||0).toFixed(2));
  _set('s-avgl','avg $'+(st.avg_loss_usdc||0).toFixed(2));
  _set('s-dd',((st.max_drawdown_pct||0)*100).toFixed(1)+'%');
  _set('s-streak',st.win_streak>1?`🔥 ${st.win_streak}x`:st.loss_streak>1?`❌ ${st.loss_streak}x`:'');
  _set('pos-cnt',ot.length?ot.length+' open':'');

  if(d.equity_curve?.length)eq=d.equity_curve.map(([ts,v])=>({ts,v}));
  drawEq();
  _set('eq-sub',`$${(st.total_fees||0).toFixed(3)} fees · ${st.total_trades||0} trades`);

  _set('cnt-s',(d.short_markets||[]).length);
  _set('cnt-a',(d.all_markets||[]).length);

  renderSpot(d.signals||{},d.buffers||{});
  renderMarkets(d.short_markets||[],d.all_markets||[],d.signals||{});
  renderTrades(ot,d.closed_trades||[]);
  renderLog(d.event_log||[]);
  _set('log-cnt',(d.event_log||[]).length+' entries');
}

function _set(id,v,c){const e=document.getElementById(id);if(!e)return;e.textContent=v;if(c)e.style.color=c;}
function _setPnl(id,v){const e=document.getElementById(id);if(!e)return;e.textContent=(v>=0?'+$':'-$')+Math.abs(v).toFixed(2);e.style.color=v>=0?'var(--green)':'var(--red)';}
function fPx(s,p){if(!p)return'—';if(s==='BTC')return'$'+Math.round(p).toLocaleString();if(s==='ETH')return'$'+p.toFixed(1);return'$'+p.toFixed(3);}
function fNum(n){if(!n)return'0';if(n>=1e6)return(n/1e6).toFixed(1)+'M';if(n>=1e3)return(n/1e3).toFixed(0)+'k';return n.toFixed(0);}
function fTime(m){if(m<60)return Math.round(m)+'m';if(m<1440)return(m/60).toFixed(1)+'h';return(m/1440).toFixed(1)+'d';}

function flashPx(sym,p){
  const el=document.getElementById('px-'+sym);if(!el)return;
  const pr=prevPx[sym];
  if(pr&&p!==pr){el.classList.remove('flash-up','flash-dn');void el.offsetWidth;el.classList.add(p>pr?'flash-up':'flash-dn');}
  el.textContent=fPx(sym,p);prevPx[sym]=p;
}

function renderSpot(sigs,bufs){
  const c=document.getElementById('spot');
  let h='';
  for(const sym of SYMS){
    const buf=bufs[sym]||{},sig=sigs[sym]||{};
    const p=buf.price||ph[sym]?.at(-1);
    const dir=sig.direction||'SKIP';
    const dc=dir==='YES'?'chip-yes':dir==='NO'?'chip-no':'chip-skip';
    const cc=sig.confidence==='high'?'chip-hi':sig.confidence==='medium'?'chip-med':'chip-lo';
    const m1=sig.momentum_1m||0,m5=sig.momentum_5m||0,r=sig.rsi||50,vd=sig.vwap_dev||0,bb=sig.bb_position||.5;
    h+=`<div class="spot-block">
      <div class="spot-row"><span class="spot-sym">${sym}</span><span class="spot-price" id="px-${sym}">${fPx(sym,p)}</span></div>
      <div class="spark-wrap"><canvas class="spark" id="sp-${sym}" height="26"></canvas></div>
      <div class="chips">
        <span class="chip ${dc}">${dir==='YES'?'↑ YES':dir==='NO'?'↓ NO':'— SKIP'}</span>
        <span class="chip ${cc}">${sig.confidence||'low'}</span>
        ${sig.edge!=null?`<span class="chip chip-edge">e${(sig.edge*100).toFixed(1)}%</span>`:''}
      </div>
      <div class="inds">
        <div class="ind"><div class="ind-l">1m</div><div class="ind-v ${m1>0?'up':m1<0?'dn':'dim'}">${m1>=0?'+':''}${m1.toFixed(2)}%</div></div>
        <div class="ind"><div class="ind-l">5m</div><div class="ind-v ${m5>0?'up':m5<0?'dn':'dim'}">${m5>=0?'+':''}${m5.toFixed(2)}%</div></div>
        <div class="ind"><div class="ind-l">RSI</div><div class="ind-v ${r>65?'dn':r<35?'up':'dim'}">${r.toFixed(0)}</div></div>
        <div class="ind"><div class="ind-l">VWAP</div><div class="ind-v ${vd>0?'dn':vd<0?'up':'dim'}">${vd>=0?'+':''}${vd.toFixed(2)}%</div></div>
      </div>
    </div>`;
  }
  c.innerHTML=h;
  SYMS.forEach(s=>{
    const cv=document.getElementById('sp-'+s);
    if(cv&&ph[s]?.length>2)drawSpark(cv,ph[s]);
  });
}

function renderMarkets(short,all,sigs){
  const c=document.getElementById('mkts');
  const markets=activeTab==='short'?short:all;
  if(!markets.length){
    c.innerHTML=`<div class="empty">${activeTab==='short'?'No short-term markets found<br>(3–120 min)':'Loading markets...'}</div>`;
    return;
  }
  const sorted=[...markets].sort((a,b)=>a.mins_left-b.mins_left);
  maxVol=Math.max(...markets.map(m=>m.volume||1),1);
  let h=`<div class="mkt-hdr"><div>Market</div><div style="text-align:right">Prob</div><div style="text-align:center">Sig</div><div style="text-align:right">TTL</div></div>`;
  for(const m of sorted){
    const p=m.yes_price||0.5;
    const pc=p>0.65?'var(--green)':p<0.35?'var(--red)':'var(--blue)';
    const sym=m.symbol==='CRYPTO'?'BTC':m.symbol;
    const sig=sigs[sym]||{};
    const dir=sig.direction||'SKIP';
    const dc=dir==='YES'?'chip chip-yes':dir==='NO'?'chip chip-no':'chip chip-skip';
    const ml=m.mins_left??9999;
    const tc=ml<30?'var(--red)':ml<120?'var(--amber)':'var(--text-dim)';
    const vpct=Math.min(100,(m.volume||0)/maxVol*100);
    h+=`<div class="mkt-row">
      <div>
        <div class="mkt-q">${m.is_short?'<span class="short-dot"></span>':''}${m.question}</div>
        <div class="mkt-meta"><span style="color:var(--blue)">${m.symbol}</span><span>$${fNum(m.volume)}</span></div>
        <div class="vbar"><div class="vfill" style="width:${vpct.toFixed(0)}%"></div></div>
      </div>
      <div class="mkt-p" style="color:${pc}">${(p*100).toFixed(1)}%</div>
      <div class="mkt-sig"><span class="${dc}" style="font-size:7px">${dir}</span></div>
      <div class="mkt-t" style="color:${tc}">${fTime(ml)}</div>
    </div>`;
  }
  c.innerHTML=h;
}

function renderTrades(open,closed){
  const c=document.getElementById('trades');
  if(!open.length&&!closed.length){
    c.innerHTML='<div class="empty">Waiting for warm-up (30s).<br>Only trades 3–120 min markets.</div>';
    return;
  }
  const now=Date.now()/1e3;
  function row(t,isOpen){
    const pnl=isOpen?t.unrealized_pnl:t.pnl_usdc;
    const pc=pnl==null?'var(--text)':pnl>=0?'var(--green)':'var(--red)';
    const ps=pnl==null?'—':(pnl>=0?'+$':'-$')+Math.abs(pnl).toFixed(2);
    const cls=isOpen?(t.action==='BUY_YES'?'yes':'no'):(t.won?'win':'loss');
    const prog=isOpen?Math.min(100,(now-t.entry_ts)/(t.horizon_min*60)*100):100;
    return `<div class="trade-card ${cls}">
      <div class="t-top"><span class="t-sym">${t.symbol}</span><span class="t-pnl" style="color:${pc}">${isOpen?'~':''} ${ps}</span></div>
      <div class="t-q">${t.question}</div>
      <div class="t-tags">
        <span class="tag ${t.action==='BUY_YES'?'tag-yes':'tag-no'}">${t.action==='BUY_YES'?'↑ YES':'↓ NO'}</span>
        <span class="tag ${isOpen?'tag-open':'tag-cls'}">${isOpen?'OPEN':t.exit_reason||'CLOSED'}</span>
        <span class="tag tag-n">$${(t.gross_usdc||t.amount_usdc).toFixed(2)}</span>
        <span class="tag tag-fee">fee $${(t.fee_usdc||0).toFixed(4)}</span>
        <span class="tag tag-n">${(t.entry_price*100).toFixed(1)}%</span>
        <span class="tag tag-n">e${(t.signal_edge*100).toFixed(1)}%</span>
      </div>
      ${isOpen?`<div class="prog"><div class="prog-f" style="width:${prog.toFixed(0)}%"></div></div>`:''}
    </div>`;
  }
  c.innerHTML=open.map(t=>row(t,true)).join('')+[...closed].reverse().slice(0,30).map(t=>row(t,false)).join('');
}

function renderLog(logs){
  const c=document.getElementById('log');
  c.innerHTML=[...logs].reverse().slice(0,100).map(e=>{
    const cls=e.event==='TRADE_OPEN'?'le-open':
              e.level==='win'?'le-win':e.level==='loss'?'le-loss':
              e.event?.includes('START')||e.event?.includes('RESET')?'le-start':
              e.event==='SKIP'?'le-skip':
              e.event==='RESOLVE_REAL'?'le-real':'';
    return `<div class="log-e ${cls}"><span class="le-ts">${e.ts_str}</span><span class="le-ev">${e.event}</span><span class="le-msg">${e.message}</span></div>`;
  }).join('');
}

function drawEq(){
  const cv=document.getElementById('eq');if(!cv||eq.length<2)return;
  const w=cv.offsetWidth||600,h=60;cv.width=w;cv.height=h;
  const ctx=cv.getContext('2d');ctx.clearRect(0,0,w,h);
  const vals=eq.map(p=>p.v),mn=Math.min(...vals)*.996,mx=Math.max(...vals)*1.004,rng=mx-mn||1;
  const pts=vals.map((v,i)=>[i/(vals.length-1)*w,h-((v-mn)/rng*(h-6)+3)]);
  const cur=vals.at(-1),up=cur>=100;
  const col=up?'#22c55e':'#ef4444';
  const bl=h-((100-mn)/rng*(h-6)+3);
  ctx.beginPath();ctx.moveTo(0,bl);ctx.lineTo(w,bl);
  ctx.strokeStyle='rgba(255,255,255,.05)';ctx.lineWidth=1;ctx.setLineDash([4,4]);ctx.stroke();ctx.setLineDash([]);
  const gr=ctx.createLinearGradient(0,0,0,h);
  gr.addColorStop(0,up?'rgba(34,197,94,.12)':'rgba(239,68,68,.12)');gr.addColorStop(1,'rgba(0,0,0,0)');
  ctx.beginPath();ctx.moveTo(pts[0][0],pts[0][1]);pts.slice(1).forEach(([x,y])=>ctx.lineTo(x,y));
  ctx.lineTo(w,h);ctx.lineTo(0,h);ctx.closePath();ctx.fillStyle=gr;ctx.fill();
  ctx.beginPath();ctx.moveTo(pts[0][0],pts[0][1]);pts.slice(1).forEach(([x,y])=>ctx.lineTo(x,y));
  ctx.strokeStyle=col;ctx.lineWidth=1.5;ctx.stroke();
  ctx.fillStyle=col;ctx.font='500 9px JetBrains Mono';
  ctx.fillText('$'+cur.toFixed(2),w-58,pts.at(-1)[1]-4);
}

function drawSpark(cv,prices){
  const w=cv.offsetWidth||196,h=26;cv.width=w;cv.height=h;
  const ctx=cv.getContext('2d');ctx.clearRect(0,0,w,h);
  if(prices.length<2)return;
  const mn=Math.min(...prices),mx=Math.max(...prices),rng=mx-mn||1;
  const pts=prices.map((p,i)=>[i/(prices.length-1)*w,h-((p-mn)/rng*(h-2)+1)]);
  const up=pts.at(-1)[1]<=pts[0][1];
  const col=up?'#22c55e':'#ef4444';
  const gr=ctx.createLinearGradient(0,0,0,h);
  gr.addColorStop(0,up?'rgba(34,197,94,.18)':'rgba(239,68,68,.18)');gr.addColorStop(1,'rgba(0,0,0,0)');
  ctx.beginPath();ctx.moveTo(pts[0][0],pts[0][1]);pts.slice(1).forEach(([x,y])=>ctx.lineTo(x,y));
  ctx.lineTo(w,h);ctx.lineTo(0,h);ctx.closePath();ctx.fillStyle=gr;ctx.fill();
  ctx.beginPath();ctx.moveTo(pts[0][0],pts[0][1]);pts.slice(1).forEach(([x,y])=>ctx.lineTo(x,y));
  ctx.strokeStyle=col;ctx.lineWidth=1.5;ctx.stroke();
}

async function toggleSim(){await fetch('/api/sim/toggle',{method:'POST'});}
async function resetSim(){if(!confirm('Reset simulation?'))return;await fetch('/api/sim/reset',{method:'POST'});eq=[{ts:Date.now()/1e3,v:100}];}
async function refreshMkts(){_set('cnt-s','…','var(--amber)');await fetch('/api/refresh',{method:'POST'});}
</script>
</body>
</html>"""


if __name__ == "__main__":
    print("=" * 60)
    print("  Polymarket Paper Bot v3 — Short-Term Focus")
    print(f"  Foco: mercados 3–120 minutos")
    print("  Dashboard → http://localhost:5001")
    print("=" * 60)
    app.run(debug=False, port=5001, threaded=True)