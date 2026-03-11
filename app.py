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
<title>Polymarket Paper Bot v3</title>
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@300;400;500;700&family=Orbitron:wght@500;700;900&display=swap" rel="stylesheet">
<style>
:root{
  --bg:#010408;--s1:#04080f;--s2:#060d18;--b:#0b1928;--b2:#0f2035;
  --acc:#00c2ff;--grn:#00e676;--red:#ff1744;--yel:#ffd600;--pur:#aa00ff;--ora:#ff6d00;
  --txt:#3a6a8a;--wht:#b8d8f0;--dim:#122840;
  --fm:'JetBrains Mono',monospace;--fh:'Orbitron',monospace;
}
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%;overflow:hidden;background:var(--bg)}
body{color:var(--txt);font-family:var(--fm);font-size:11px}
body::before{content:'';position:fixed;inset:0;pointer-events:none;z-index:0;
  background:radial-gradient(ellipse 60% 40% at 5% 5%,rgba(0,194,255,.04),transparent 55%),
             radial-gradient(ellipse 40% 60% at 95% 95%,rgba(0,230,118,.03),transparent 55%)}
body::after{content:'';position:fixed;inset:0;pointer-events:none;z-index:9999;
  background:repeating-linear-gradient(0deg,transparent,transparent 3px,rgba(0,194,255,.005) 4px)}
.app{display:grid;grid-template-rows:44px 1fr;height:100vh;position:relative;z-index:1}

/* NAV */
nav{display:flex;align-items:center;padding:0 14px;gap:12px;
    background:rgba(1,4,8,.96);border-bottom:1px solid var(--b);backdrop-filter:blur(10px)}
.brand{font-family:var(--fh);font-size:13px;font-weight:900;letter-spacing:.12em;
       color:var(--wht);display:flex;align-items:center;gap:8px}
.dot{width:7px;height:7px;border-radius:50%;background:var(--grn);
     box-shadow:0 0 8px var(--grn);animation:pulse 1.4s ease-in-out infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.25}}
.tag{font-size:8px;letter-spacing:.14em;padding:2px 6px;text-transform:uppercase;font-weight:700;border-radius:2px}
.tag-live{background:rgba(0,230,118,.1);color:var(--grn);border:1px solid rgba(0,230,118,.3)}
.tag-mock{background:rgba(255,214,0,.1);color:var(--yel);border:1px solid rgba(255,214,0,.3)}
.tag-warm{background:rgba(255,109,0,.1);color:var(--ora);border:1px solid rgba(255,109,0,.3);animation:pulse .9s infinite}
.tag-short{background:rgba(170,0,255,.1);color:var(--pur);border:1px solid rgba(170,0,255,.3)}
.nv{display:flex;gap:16px;margin-left:auto;align-items:center}
.ns{display:flex;flex-direction:column;align-items:flex-end}
.nl{font-size:7px;letter-spacing:.18em;color:var(--dim);text-transform:uppercase}
.nv_{font-size:13px;font-weight:700;font-family:var(--fh)}
.btn{font-family:var(--fm);font-size:8px;letter-spacing:.12em;text-transform:uppercase;
     padding:5px 12px;border:1px solid;background:transparent;cursor:pointer;transition:.12s;border-radius:2px}
.btn-run{border-color:rgba(0,230,118,.5);color:var(--grn)}
.btn-run:hover{background:rgba(0,230,118,.08)}
.btn-stop{border-color:rgba(255,23,68,.4);color:var(--red)}
.btn-reset{border-color:var(--b2);color:var(--txt)}
.btn-reset:hover{border-color:var(--yel);color:var(--yel)}
.btn-ref{border-color:rgba(0,194,255,.3);color:rgba(0,194,255,.6)}
.btn-ref:hover{border-color:var(--acc);color:var(--acc)}

/* LAYOUT */
.grid{display:grid;grid-template-columns:252px 1fr 300px;height:100%;overflow:hidden}
.col{border-right:1px solid var(--b);overflow:hidden;display:flex;flex-direction:column}
.col:last-child{border-right:none;border-left:1px solid var(--b)}
.cscroll{overflow-y:auto;overflow-x:hidden;flex:1}
.chd{font-family:var(--fh);font-size:7.5px;font-weight:700;letter-spacing:.2em;
     color:var(--dim);padding:8px 12px;border-bottom:1px solid var(--b);
     background:var(--s1);display:flex;justify-content:space-between;align-items:center;
     flex-shrink:0;text-transform:uppercase}

/* SPOT */
.spot{padding:9px 12px;border-bottom:1px solid var(--b)}
.st{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:4px}
.sn{font-family:var(--fh);font-size:14px;font-weight:900;color:var(--wht);letter-spacing:.1em}
.sp{font-size:12px;font-weight:700;color:var(--acc)}
canvas.spark{display:block;width:100%;height:28px;margin-bottom:5px}
.chips{display:flex;gap:3px;flex-wrap:wrap;margin-bottom:5px}
.chip{font-size:7.5px;font-weight:700;letter-spacing:.07em;padding:2px 5px;border-radius:2px;border:1px solid;text-transform:uppercase}
.c-yes{background:rgba(0,230,118,.08);color:var(--grn);border-color:rgba(0,230,118,.3)}
.c-no {background:rgba(255,23,68,.08);color:var(--red);border-color:rgba(255,23,68,.3)}
.c-skip{background:rgba(18,40,64,.3);color:var(--dim);border-color:var(--b)}
.c-hi {background:rgba(0,194,255,.07);color:var(--acc);border-color:rgba(0,194,255,.25)}
.c-med{background:rgba(255,214,0,.07);color:var(--yel);border-color:rgba(255,214,0,.25)}
.c-lo {background:rgba(18,40,64,.2);color:var(--dim);border-color:var(--b)}
.c-pur{background:rgba(170,0,255,.06);color:var(--pur);border-color:rgba(170,0,255,.2)}
.c-ev {background:rgba(255,109,0,.06);color:var(--ora);border-color:rgba(255,109,0,.2)}
.inds{display:grid;grid-template-columns:1fr 1fr;gap:2px}
.ind{background:var(--s2);padding:2px 6px}
.il{font-size:6px;letter-spacing:.15em;color:var(--dim);text-transform:uppercase}
.iv{font-size:9.5px;font-weight:500;color:var(--wht)}
.pos{color:var(--grn)!important}.neg{color:var(--red)!important}.neu{color:var(--dim)!important}
.reasons{font-size:7px;color:var(--dim);margin-top:3px;line-height:1.7}

/* TABS */
.tabs{display:flex;border-bottom:1px solid var(--b);flex-shrink:0;background:var(--s1)}
.tab{font-family:var(--fh);font-size:7px;font-weight:700;letter-spacing:.15em;
     text-transform:uppercase;padding:7px 12px;cursor:pointer;color:var(--dim);
     border-bottom:2px solid transparent;transition:.12s}
.tab.active{color:var(--acc);border-bottom-color:var(--acc)}
.tab-cnt{font-size:8px;margin-left:4px;color:var(--pur)}

/* MARKETS */
.mhdr,.mrow{display:grid;grid-template-columns:1fr 56px 50px 46px;gap:4px;
            padding:6px 10px;border-bottom:1px solid rgba(11,25,40,.8);align-items:start}
.mhdr{background:rgba(4,8,15,.95);position:sticky;top:0;z-index:5;
      font-size:7px;letter-spacing:.12em;color:var(--dim);text-transform:uppercase}
.mrow:hover{background:rgba(255,255,255,.01)}
.mq{font-size:8px;color:var(--wht);line-height:1.35}
.msub{font-size:7px;color:var(--dim);margin-top:2px;display:flex;gap:6px;flex-wrap:wrap}
.mp{font-family:var(--fh);font-size:10px;font-weight:700;text-align:right}
.mc{text-align:center}.mt{font-size:7px;text-align:right;color:var(--dim)}
.vbar{height:2px;background:var(--b);margin-top:2px;border-radius:1px;overflow:hidden}
.vfill{height:100%;background:rgba(0,194,255,.25);border-radius:1px}
.short-badge{font-size:6px;padding:1px 3px;background:rgba(170,0,255,.1);
             color:var(--pur);border:1px solid rgba(170,0,255,.2);border-radius:1px}

/* STATS */
.strip{display:grid;grid-template-columns:repeat(6,1fr);border-bottom:1px solid var(--b);flex-shrink:0}
.sc{padding:8px 10px;border-right:1px solid var(--b);position:relative;overflow:hidden}
.sc:last-child{border-right:none}
.sc::after{content:'';position:absolute;bottom:0;left:0;right:0;height:2px}
.sc.g::after{background:var(--grn)}.sc.a::after{background:var(--acc)}
.sc.y::after{background:var(--yel)}.sc.r::after{background:var(--red)}
.sc.p::after{background:var(--pur)}.sc.d::after{background:var(--dim)}
.scl{font-size:6.5px;letter-spacing:.18em;color:var(--dim);text-transform:uppercase;margin-bottom:2px}
.scv{font-family:var(--fh);font-size:17px;font-weight:900;letter-spacing:-.02em;line-height:1}
.sc.g .scv{color:var(--grn)}.sc.a .scv{color:var(--acc)}.sc.y .scv{color:var(--yel)}
.sc.r .scv{color:var(--red)}.sc.p .scv{color:var(--pur)}.sc.d .scv{color:var(--dim)}
.scs{font-size:7px;color:var(--dim);margin-top:1px}

/* EQUITY */
.eqw{padding:8px 12px;border-bottom:1px solid var(--b);flex-shrink:0}
.eqtitle{font-size:7px;letter-spacing:.16em;color:var(--dim);text-transform:uppercase;
         margin-bottom:4px;display:flex;justify-content:space-between}

/* TRADES */
.tcard{padding:7px 10px;border-bottom:1px solid rgba(11,25,40,.5);
       border-left:2px solid transparent;animation:fadein .2s ease}
@keyframes fadein{from{opacity:0;transform:translateY(-3px)}to{opacity:1;transform:translateY(0)}}
.tcard.yes{border-left-color:var(--grn)}.tcard.no{border-left-color:var(--red)}
.tcard.win{border-left-color:var(--grn);opacity:.6}.tcard.loss{border-left-color:var(--red);opacity:.5}
.tt{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:2px}
.tsym{font-family:var(--fh);font-size:10px;font-weight:900;color:var(--wht);letter-spacing:.1em}
.tpnl{font-size:11px;font-weight:700}
.tq{font-size:7.5px;color:var(--txt);line-height:1.4;margin-bottom:3px}
.tmeta{display:flex;gap:3px;flex-wrap:wrap}
.tg{font-size:6.5px;letter-spacing:.06em;padding:1px 5px;border-radius:2px;border:1px solid}
.tg-y{background:rgba(0,230,118,.06);color:var(--grn);border-color:rgba(0,230,118,.18)}
.tg-n{background:rgba(255,23,68,.06);color:var(--red);border-color:rgba(255,23,68,.18)}
.tg-o{background:rgba(0,194,255,.04);color:var(--acc);border-color:rgba(0,194,255,.12)}
.tg-c{background:rgba(18,40,64,.2);color:var(--dim);border-color:var(--b)}
.tg-k{background:var(--s1);color:var(--dim);border-color:var(--b)}
.tg-fee{background:rgba(255,109,0,.05);color:var(--ora);border-color:rgba(255,109,0,.15)}
.tg-real{background:rgba(0,230,118,.1);color:var(--grn);border-color:rgba(0,230,118,.3)}
.tg-sim{background:rgba(255,214,0,.07);color:var(--yel);border-color:rgba(255,214,0,.2)}
.prog{height:2px;background:var(--b);margin-top:3px;overflow:hidden}
.progf{height:100%;background:rgba(0,194,255,.5);transition:width 1s linear}

/* LOG */
.loge{padding:4px 9px;border-left:2px solid;font-size:8px;line-height:1.5;animation:fadein .18s ease}
.lo{border-left-color:var(--acc)}.lw{border-left-color:var(--grn);background:rgba(0,230,118,.015)}
.ll{border-left-color:var(--red);background:rgba(255,23,68,.015)}
.ls{border-left-color:var(--yel)}.lk{border-left-color:var(--b);opacity:.55}
.lo2{border-left-color:var(--dim)}.lr{border-left-color:var(--pur)}
.lts{color:var(--dim);margin-right:4px}.lev{font-weight:700;letter-spacing:.06em;margin-right:4px}

.fu{animation:fu .35s ease}.fd{animation:fd .35s ease}
@keyframes fu{0%{color:var(--grn)}100%{}}@keyframes fd{0%{color:var(--red)}100%{}}

::-webkit-scrollbar{width:2px}::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:var(--b2)}
.empty{padding:16px;text-align:center;color:var(--dim);font-size:8.5px;letter-spacing:.1em;line-height:2.2}
</style>
</head>
<body>
<div class="app">

<nav>
  <div class="brand"><div class="dot"></div>POLYMARKET PAPER BOT <span style="font-size:9px;opacity:.5">v3</span></div>
  <span id="b-bin" class="tag tag-mock">◯ BIN MOCK</span>
  <span id="b-clob" class="tag tag-mock">◯ CLOB MOCK</span>
  <span id="b-warm" class="tag tag-warm" style="display:none">⏳ WARM-UP</span>
  <span id="b-short" class="tag tag-short">0 SHORT</span>
  <div class="nv">
    <div class="ns"><span class="nl">Balance</span><span class="nv_" id="n-bal" style="color:var(--grn)">$100</span></div>
    <div class="ns"><span class="nl">P&L</span><span class="nv_" id="n-pnl">$0</span></div>
    <div class="ns"><span class="nl">Win Rate</span><span class="nv_" id="n-wr" style="color:var(--acc)">—</span></div>
    <div class="ns"><span class="nl">Fees Pagas</span><span class="nv_" id="n-fees" style="color:var(--ora)">$0</span></div>
    <div class="ns"><span class="nl">CLOB lat</span><span class="nv_" id="n-lat">—</span></div>
    <button class="btn btn-run" id="btn-tog" onclick="toggleSim()">▶ START</button>
    <button class="btn btn-reset" onclick="resetSim()">↺ RESET</button>
    <button class="btn btn-ref" onclick="refreshMarkets()">⟳ REFRESH</button>
  </div>
</nav>

<div class="grid">
  <!-- LEFT -->
  <div class="col">
    <div class="chd">Spot Prices <span id="hbin" style="font-size:7px"></span></div>
    <div class="cscroll" id="spot"></div>
    <div class="tabs">
      <div class="tab active" id="tab-short" onclick="setTab('short')">Short-Term <span class="tab-cnt" id="cnt-short">0</span></div>
      <div class="tab" id="tab-all" onclick="setTab('all')">Todos <span class="tab-cnt" id="cnt-all">0</span></div>
    </div>
    <div class="cscroll" id="mkts"></div>
  </div>

  <!-- CENTER -->
  <div class="col">
    <div class="strip">
      <div class="sc g"><div class="scl">Balance</div><div class="scv" id="s-bal">$100</div></div>
      <div class="sc a"><div class="scl">P&L</div><div class="scv" id="s-pnl">$0</div><div class="scs" id="s-roi">0%</div></div>
      <div class="sc y"><div class="scl">Open</div><div class="scv" id="s-open">0</div><div class="scs" id="s-unr"></div></div>
      <div class="sc g"><div class="scl">Wins</div><div class="scv" id="s-wins">0</div><div class="scs" id="s-avgw"></div></div>
      <div class="sc r"><div class="scl">Losses</div><div class="scv" id="s-loss">0</div><div class="scs" id="s-avgl"></div></div>
      <div class="sc d"><div class="scl">Max DD</div><div class="scv" id="s-dd">0%</div><div class="scs" id="s-streak"></div></div>
    </div>
    <div class="eqw">
      <div class="eqtitle">Equity Curve <span id="eq-sub"></span></div>
      <canvas id="eq" height="65"></canvas>
    </div>
    <div class="chd" style="flex-shrink:0">Posições <span id="pos-cnt" style="color:var(--acc);font-size:8px"></span></div>
    <div class="cscroll" id="trades"></div>
  </div>

  <!-- RIGHT -->
  <div class="col">
    <div class="chd">Activity Log <span id="log-cnt" style="font-size:7.5px;color:var(--dim)"></span></div>
    <div class="cscroll" id="log" style="padding:3px 6px;display:flex;flex-direction:column;gap:2px"></div>
  </div>
</div>
</div>

<script>
let snap={}, eq=[{ts:Date.now()/1e3,v:100}], ph={}, prevPx={}, maxVol=1;
let activeTab='short';
const SYMS=['BTC','ETH','SOL','BNB','MATIC','DOGE'];
SYMS.forEach(s=>ph[s]=[]);

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
  document.getElementById('tab-short').classList.toggle('active',t==='short');
  document.getElementById('tab-all').classList.toggle('active',t==='all');
  renderMarkets(snap.short_markets||[],snap.all_markets||[],snap.signals||{});
}

function apply(d){
  snap=d;
  const st=d.stats||{},bal=st.balance??100,pnl=st.total_pnl??0;
  const w=st.wins??0,l=st.losses??0;
  const wr=w+l>0?(w/(w+l)*100).toFixed(0)+'%':'—';

  set('n-bal','$'+bal.toFixed(2),bal>=100?'var(--grn)':'var(--red)');
  setPnl('n-pnl',pnl);
  set('n-wr',wr,'var(--acc)');
  set('n-fees','$'+(st.total_fees||0).toFixed(3),'var(--ora)');
  const lat=d.feed?.clob_lat_ms??0;
  set('n-lat',lat>0?lat.toFixed(1)+'ms':'—',lat<30?'var(--grn)':lat<100?'var(--yel)':'var(--red)');

  ['bin','clob'].forEach(k=>{
    const live=d.feed?.[k==='bin'?'binance_live':'clob_live'];
    const el=document.getElementById('b-'+k);
    el.textContent=live?`⬤ ${k.toUpperCase()} LIVE`:`◯ ${k.toUpperCase()} MOCK`;
    el.className='tag '+(live?'tag-live':'tag-mock');
  });
  set('hbin',d.feed?.binance_live?'● LIVE':'◯ MOCK',d.feed?.binance_live?'var(--grn)':'var(--yel)');

  const sc=d.feed?.short_markets??0;
  const bshort=document.getElementById('b-short');
  bshort.textContent=`${sc} SHORT`;
  bshort.style.display=sc>0?'inline-block':'none';

  const wu=st.warmup_remaining??0;
  const bwu=document.getElementById('b-warm');
  bwu.style.display=wu>0?'inline-block':'none';
  if(wu>0)bwu.textContent=`⏳ WARM-UP ${wu}s`;

  const btn=document.getElementById('btn-tog');
  btn.textContent=d.running?'■ RUNNING':'▶ START';
  btn.className='btn '+(d.running?'btn-stop':'btn-run');

  set('s-bal','$'+bal.toFixed(0));
  const pe=document.getElementById('s-pnl');
  pe.textContent=(pnl>=0?'+$':'-$')+Math.abs(pnl).toFixed(2);
  pe.style.color=pnl>=0?'var(--grn)':'var(--red)';
  set('s-roi',(((bal-100)/100)*100).toFixed(1)+'% ROI');
  const ot=d.open_trades||[];
  set('s-open',ot.length);
  const unr=ot.reduce((s,t)=>s+(t.unrealized_pnl||0),0);
  set('s-unr',(unr>=0?'+':'')+'$'+Math.abs(unr).toFixed(2)+' unreal');
  set('s-wins',w);set('s-loss',l);
  set('s-avgw','avg $'+(st.avg_win_usdc||0).toFixed(2));
  set('s-avgl','avg $'+(st.avg_loss_usdc||0).toFixed(2));
  set('s-dd',((st.max_drawdown_pct||0)*100).toFixed(1)+'%');
  if(wu>0)set('s-streak',`⏳ ${wu}s`,'var(--ora)');
  else set('s-streak',st.win_streak>1?`🔥${st.win_streak}x`:st.loss_streak>1?`❌${st.loss_streak}x`:'');
  set('pos-cnt',ot.length?ot.length+' open':'');

  if(d.equity_curve?.length)eq=d.equity_curve.map(([ts,v])=>({ts,v}));
  drawEq();
  set('eq-sub',`fees $${(st.total_fees||0).toFixed(3)} · wagered $${(st.total_wagered||0).toFixed(2)} · ${st.total_trades||0} trades`);

  set('cnt-short',(d.short_markets||[]).length);
  set('cnt-all',(d.all_markets||[]).length);

  renderSpot(d.signals||{},d.buffers||{});
  renderMarkets(d.short_markets||[],d.all_markets||[],d.signals||{});
  renderTrades(ot,d.closed_trades||[]);
  renderLog(d.event_log||[]);
  set('log-cnt',(d.event_log||[]).length);
}

function set(id,v,c){const e=document.getElementById(id);if(!e)return;e.textContent=v;if(c)e.style.color=c;}
function setPnl(id,v){const e=document.getElementById(id);if(!e)return;e.textContent=(v>=0?'+$':'-$')+Math.abs(v).toFixed(2);e.style.color=v>=0?'var(--grn)':'var(--red)';}
function fPx(s,p){if(!p)return'—';if(s==='BTC')return'$'+Math.round(p).toLocaleString();if(s==='ETH')return'$'+p.toFixed(1);return'$'+p.toFixed(3);}
function fNum(n){if(!n)return'0';if(n>=1e6)return(n/1e6).toFixed(1)+'M';if(n>=1e3)return(n/1e3).toFixed(0)+'k';return n.toFixed(0);}
function fTime(m){if(m<60)return Math.round(m)+'m';if(m<1440)return(m/60).toFixed(1)+'h';return(m/1440).toFixed(1)+'d';}

function flashPx(sym,p){
  const el=document.getElementById('px-'+sym);if(!el)return;
  const pr=prevPx[sym];
  if(pr&&p!==pr){el.classList.remove('fu','fd');void el.offsetWidth;el.classList.add(p>pr?'fu':'fd');}
  el.textContent=fPx(sym,p);prevPx[sym]=p;
}

function renderSpot(sigs,bufs){
  const c=document.getElementById('spot');
  let h='';
  for(const sym of SYMS){
    const buf=bufs[sym]||{},sig=sigs[sym]||{};
    const p=buf.price||ph[sym]?.at(-1);
    const dir=sig.direction||'SKIP';
    const dc=dir==='YES'?'c-yes':dir==='NO'?'c-no':'c-skip';
    const cc=sig.confidence==='high'?'c-hi':sig.confidence==='medium'?'c-med':'c-lo';
    const mt=sig.market_type||'price';
    const m1=sig.momentum_1m||0,m5=sig.momentum_5m||0,r=sig.rsi||50,vd=sig.vwap_dev||0,bb=sig.bb_position||.5;
    h+=`<div class="spot">
      <div class="st"><span class="sn">${sym}</span><span class="sp" id="px-${sym}">${fPx(sym,p)}</span></div>
      <canvas class="spark" id="sp-${sym}" height="28"></canvas>
      <div class="chips">
        <span class="chip ${dc}">${dir==='YES'?'▲ YES':dir==='NO'?'▼ NO':'— SKIP'}</span>
        <span class="chip ${cc}">${sig.confidence||'—'}</span>
        ${sig.edge!=null?`<span class="chip c-pur">e${(sig.edge*100).toFixed(1)}%</span>`:''}
        <span class="chip ${mt==='price'?'c-hi':'c-ev'}">${mt}</span>
      </div>
      <div class="inds">
        <div class="ind"><div class="il">mom1m</div><div class="iv ${m1>0?'pos':m1<0?'neg':'neu'}">${m1>=0?'+':''}${m1.toFixed(2)}%</div></div>
        <div class="ind"><div class="il">mom5m</div><div class="iv ${m5>0?'pos':m5<0?'neg':'neu'}">${m5>=0?'+':''}${m5.toFixed(2)}%</div></div>
        <div class="ind"><div class="il">RSI</div><div class="iv ${r>65?'neg':r<35?'pos':'neu'}">${r.toFixed(0)}</div></div>
        <div class="ind"><div class="il">VWAP</div><div class="iv ${vd>0?'neg':vd<0?'pos':'neu'}">${vd>=0?'+':''}${vd.toFixed(2)}%</div></div>
        <div class="ind"><div class="il">BB%</div><div class="iv">${(bb*100).toFixed(0)}</div></div>
        <div class="ind"><div class="il">ticks</div><div class="iv neu">${buf.ticks||0}</div></div>
      </div>
      ${sig.reasons?.length?`<div class="reasons">${sig.reasons.join(' · ')}</div>`:''}
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
    c.innerHTML=`<div class="empty">${activeTab==='short'?'Nenhum mercado short-term (3–120min)<br><small>Mercados de curto prazo aparecem aqui</small>':'Buscando mercados...'}</div>`;
    return;
  }
  const sorted=[...markets].sort((a,b)=>a.mins_left-b.mins_left);
  maxVol=Math.max(...markets.map(m=>m.volume||1),1);
  let h=`<div class="mhdr"><div>Mercado</div><div style="text-align:right">YES</div><div style="text-align:center">Sig</div><div style="text-align:right">Encerra</div></div>`;
  for(const m of sorted){
    const p=m.yes_price||0.5;
    const pc=p>0.65?'var(--grn)':p<0.35?'var(--red)':'var(--acc)';
    const sym=m.symbol==='CRYPTO'?'BTC':m.symbol;
    const sig=sigs[sym]||{};
    const dir=sig.direction||'SKIP';
    const dc=dir==='YES'?'c-yes':dir==='NO'?'c-no':'c-skip';
    const ml=m.mins_left??9999;
    const tc=ml<30?'var(--red)':ml<120?'var(--yel)':'var(--dim)';
    const vpct=Math.min(100,(m.volume||0)/maxVol*100);
    const isShort=m.is_short||false;
    h+=`<div class="mrow">
      <div>
        <div class="mq">${m.question}</div>
        <div class="msub">
          <span style="color:var(--acc);font-size:6.5px">${m.symbol}</span>
          ${isShort?'<span class="short-badge">SHORT</span>':''}
          <span>$${fNum(m.volume)}</span>
          <span style="color:var(--dim)">liq $${fNum(m.liquidity)}</span>
        </div>
        <div class="vbar"><div class="vfill" style="width:${vpct.toFixed(0)}%"></div></div>
      </div>
      <div class="mp" style="color:${pc}">${(p*100).toFixed(1)}%</div>
      <div class="mc"><span class="chip ${dc}" style="font-size:6.5px">${dir}</span></div>
      <div class="mt" style="color:${tc}">${fTime(ml)}</div>
    </div>`;
  }
  c.innerHTML=h;
}

function renderTrades(open,closed){
  const c=document.getElementById('trades');
  if(!open.length&&!closed.length){
    c.innerHTML='<div class="empty">Aguardando warm-up (3min).<br>Só opera mercados 3–120min.<br><small>Resolução via CLOB API quando disponível.</small></div>';
    return;
  }
  const now=Date.now()/1e3;
  function row(t,isOpen){
    const pnl=isOpen?t.unrealized_pnl:t.pnl_usdc;
    const pc=pnl==null?'var(--txt)':pnl>=0?'var(--grn)':'var(--red)';
    const ps=pnl==null?'—':(pnl>=0?'+$':'-$')+Math.abs(pnl).toFixed(2);
    const cls=isOpen?(t.action==='BUY_YES'?'yes':'no'):(t.won?'win':'loss');
    const ac=t.action==='BUY_YES'?'tg-y':'tg-n';
    const al=t.action==='BUY_YES'?'▲ YES':'▼ NO';
    const prog=isOpen?Math.min(100,(now-t.entry_ts)/(t.horizon_min*60)*100):100;
    const enc=t.end_date_iso?new Date(t.end_date_iso).toLocaleDateString('pt-BR',{month:'short',day:'numeric'}):'';
    const resolveTag=!isOpen&&t.exit_reason==='EXPIRED'?
      `<span class="tg tg-real">REAL CLOB</span>`:'';
    const mtype=t.market_type||'price';
    return `<div class="tcard ${cls}">
      <div class="tt"><span class="tsym">${t.symbol}</span><span class="tpnl" style="color:${pc}">${isOpen?'~':''} ${ps}</span></div>
      <div class="tq">${t.question}</div>
      <div class="tmeta">
        <span class="tg ${ac}">${al}</span>
        <span class="tg ${isOpen?'tg-o':'tg-c'}">${isOpen?'OPEN':t.exit_reason||'CLOSED'}</span>
        <span class="tg tg-k">gross $${(t.gross_usdc||t.amount_usdc).toFixed(2)}</span>
        <span class="tg tg-fee">fee $${(t.fee_usdc||0).toFixed(4)}</span>
        <span class="tg tg-k">in ${(t.entry_price*100).toFixed(1)}%</span>
        ${isOpen&&t.current_price!=null?`<span class="tg tg-k">now ${(t.current_price*100).toFixed(1)}%</span>`:''}
        <span class="tg tg-k">edge ${(t.signal_edge*100).toFixed(1)}%</span>
        <span class="tg ${mtype==='price'?'tg-o':'tg-sim'}">${mtype}</span>
        ${resolveTag}
        ${enc?`<span class="tg tg-k">${enc}</span>`:''}
      </div>
      ${isOpen?`<div class="prog"><div class="progf" style="width:${prog.toFixed(0)}%"></div></div>`:''}
    </div>`;
  }
  c.innerHTML=open.map(t=>row(t,true)).join('')+[...closed].reverse().slice(0,30).map(t=>row(t,false)).join('');
}

function renderLog(logs){
  const c=document.getElementById('log');
  c.innerHTML=[...logs].reverse().slice(0,100).map(e=>{
    const cls=e.event==='TRADE_OPEN'?'lo':
              e.level==='win'?'lw':e.level==='loss'?'ll':
              e.event?.includes('START')||e.event?.includes('RESET')?'ls':
              e.event==='SKIP'?'lk':
              e.event==='RESOLVE_REAL'?'lr':'lo2';
    return `<div class="loge ${cls}"><span class="lts">${e.ts_str}</span><span class="lev">${e.event}</span><span style="color:var(--txt)">${e.message}</span></div>`;
  }).join('');
}

function drawEq(){
  const cv=document.getElementById('eq');if(!cv||eq.length<2)return;
  const w=cv.offsetWidth||600,h=65;cv.width=w;cv.height=h;
  const ctx=cv.getContext('2d');ctx.clearRect(0,0,w,h);
  const vals=eq.map(p=>p.v),mn=Math.min(...vals)*.996,mx=Math.max(...vals)*1.004,rng=mx-mn||1;
  const pts=vals.map((v,i)=>[i/(vals.length-1)*w,h-((v-mn)/rng*(h-6)+3)]);
  const cur=vals.at(-1),up=cur>=100,col=up?'#00e676':'#ff1744';
  const bl=h-((100-mn)/rng*(h-6)+3);
  ctx.beginPath();ctx.moveTo(0,bl);ctx.lineTo(w,bl);
  ctx.strokeStyle='rgba(18,40,64,.6)';ctx.lineWidth=1;ctx.setLineDash([3,5]);ctx.stroke();ctx.setLineDash([]);
  const gr=ctx.createLinearGradient(0,0,0,h);
  gr.addColorStop(0,up?'rgba(0,230,118,.14)':'rgba(255,23,68,.14)');gr.addColorStop(1,'rgba(0,0,0,0)');
  ctx.beginPath();ctx.moveTo(pts[0][0],pts[0][1]);pts.slice(1).forEach(([x,y])=>ctx.lineTo(x,y));
  ctx.lineTo(w,h);ctx.lineTo(0,h);ctx.closePath();ctx.fillStyle=gr;ctx.fill();
  ctx.beginPath();ctx.moveTo(pts[0][0],pts[0][1]);pts.slice(1).forEach(([x,y])=>ctx.lineTo(x,y));
  ctx.strokeStyle=col;ctx.lineWidth=1.5;ctx.stroke();
  ctx.fillStyle=col;ctx.font='bold 8px JetBrains Mono';
  ctx.fillText('$'+cur.toFixed(2),w-60,pts.at(-1)[1]-4);
}

function drawSpark(cv,prices){
  const w=cv.offsetWidth||220,h=28;cv.width=w;cv.height=h;
  const ctx=cv.getContext('2d');ctx.clearRect(0,0,w,h);
  if(prices.length<2)return;
  const mn=Math.min(...prices),mx=Math.max(...prices),rng=mx-mn||1;
  const pts=prices.map((p,i)=>[i/(prices.length-1)*w,h-((p-mn)/rng*(h-2)+1)]);
  const up=pts.at(-1)[1]<=pts[0][1],col=up?'#00e676':'#ff1744';
  const gr=ctx.createLinearGradient(0,0,0,h);
  gr.addColorStop(0,up?'rgba(0,230,118,.16)':'rgba(255,23,68,.16)');gr.addColorStop(1,'rgba(0,0,0,0)');
  ctx.beginPath();ctx.moveTo(pts[0][0],pts[0][1]);pts.slice(1).forEach(([x,y])=>ctx.lineTo(x,y));
  ctx.lineTo(w,h);ctx.lineTo(0,h);ctx.closePath();ctx.fillStyle=gr;ctx.fill();
  ctx.beginPath();ctx.moveTo(pts[0][0],pts[0][1]);pts.slice(1).forEach(([x,y])=>ctx.lineTo(x,y));
  ctx.strokeStyle=col;ctx.lineWidth=1.5;ctx.stroke();
}

async function toggleSim(){await fetch('/api/sim/toggle',{method:'POST'});}
async function resetSim(){if(!confirm('Resetar simulação?'))return;await fetch('/api/sim/reset',{method:'POST'});eq=[{ts:Date.now()/1e3,v:100}];}
async function refreshMarkets(){set('cnt-short','...','var(--yel)');await fetch('/api/refresh',{method:'POST'});}
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