"""
Polymarket Crypto Options Bot — Real-Time Dashboard
Run: python crypto_dashboard.py
Open: http://localhost:5001
"""
from __future__ import annotations
import json
import math
import queue
import time
import threading
import traceback
from flask import Flask, Response, jsonify, render_template_string, request

from src.signals.engine import PriceBuffer, compute_signal
from src.execution.bot import CryptoBot

try:
    from src.feed.binance import BinanceFeed
    USE_MOCK = False
except Exception:
    USE_MOCK = True

app = Flask(__name__)

# ── Global state ───────────────────────────────────────────────────────────────
SYMBOLS = ["BTC", "ETH", "SOL"]
buffers: dict[str, PriceBuffer] = {s: PriceBuffer(maxlen=200) for s in SYMBOLS}
bot = CryptoBot(buffers)
feed = None
_sse_clients: list[queue.Queue] = []
_sse_lock = threading.Lock()


def on_tick(symbol: str, price: float, volume: float):
    if symbol in buffers:
        buffers[symbol].push(price, volume)
    push_sse_event("tick", {"symbol": symbol, "price": price, "ts": time.time()})


def push_sse_event(event: str, data: dict):
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


def start_feed():
    global feed, USE_MOCK
    try:
        if not USE_MOCK:
            feed = BinanceFeed(SYMBOLS, on_tick)
            feed.start()
            # Wait 2s to confirm connection
            time.sleep(2)
            if not feed.connected:
                raise ConnectionError("Binance WS did not connect")
            print("[App] Binance live feed active")
        else:
            raise ImportError("websockets not available")
    except Exception as e:
        print(f"[App] Falling back to mock feed: {e}")
        from src.feed.binance import MockFeed
        feed = MockFeed(SYMBOLS, on_tick)
        feed.start()
        USE_MOCK = True


# Start feeds + bot in background
def _background_start():
    time.sleep(0.5)
    start_feed()
    time.sleep(3)  # Let buffers warm up
    bot.start()
    print("[App] Bot started")

threading.Thread(target=_background_start, daemon=True).start()


# ── SSE endpoint ───────────────────────────────────────────────────────────────
@app.route("/stream")
def stream():
    q: queue.Queue = queue.Queue(maxsize=50)
    with _sse_lock:
        _sse_clients.append(q)

    def generate():
        # Initial state push
        snap = bot.state_snapshot()
        yield f"event: state\ndata: {json.dumps(snap)}\n\n"
        try:
            while True:
                try:
                    msg = q.get(timeout=25)
                    yield msg
                except queue.Empty:
                    yield ": ping\n\n"
        except GeneratorExit:
            with _sse_lock:
                if q in _sse_clients:
                    _sse_clients.remove(q)

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/api/state")
def api_state():
    snap = bot.state_snapshot()
    # Add signal snapshots for each symbol
    signals = {}
    for sym, buf in buffers.items():
        if buf.latest_price():
            sig = compute_signal(sym, buf, 0.5, 10)
            signals[sym] = {
                "direction": sig.direction,
                "strength": sig.strength,
                "confidence": sig.confidence,
                "edge": sig.edge,
                "momentum_1m": sig.momentum_1m,
                "momentum_5m": sig.momentum_5m,
                "rsi": sig.rsi,
                "vwap_dev": sig.vwap_dev,
                "bb_position": sig.bb_position,
                "reasons": sig.reasons,
            }
    snap["signals"] = signals
    snap["feed_live"] = not USE_MOCK
    return jsonify(snap)


@app.route("/api/bot/toggle", methods=["POST"])
def toggle_bot():
    if bot._running:
        bot.stop()
        return jsonify({"running": False})
    else:
        bot.start()
        return jsonify({"running": True})


@app.route("/api/bot/reset", methods=["POST"])
def reset_bot():
    was_running = bot._running
    bot.stop()
    time.sleep(0.5)
    bot.balance = CryptoBot.STARTING_BALANCE
    bot.positions.clear()
    bot.closed_positions.clear()
    from src.execution.bot import BotStats
    bot.stats = BotStats(peak_balance=CryptoBot.STARTING_BALANCE)
    bot.log.clear()
    if was_running:
        bot.start()
    return jsonify({"ok": True, "balance": bot.balance})


@app.route("/")
def index():
    return render_template_string(DASHBOARD_HTML)


# ── HTML Dashboard ─────────────────────────────────────────────────────────────
DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Crypto Options Bot</title>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@300;400;500;600;700&family=Barlow+Condensed:wght@300;400;600;800&display=swap" rel="stylesheet">
<style>
:root {
  --bg: #020509;
  --s1: #060c12;
  --s2: #0a1520;
  --b: #0d2035;
  --b2: #112840;
  --acc: #00e5ff;
  --grn: #00ff88;
  --red: #ff2255;
  --yel: #ffcc00;
  --muted: #2a4a6a;
  --txt: #7aacc0;
  --white: #cce8f4;
  --font-mono: 'IBM Plex Mono', monospace;
  --font-display: 'Barlow Condensed', sans-serif;
}
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
body {
  background: var(--bg);
  color: var(--txt);
  font-family: var(--font-mono);
  font-size: 12px;
  overflow-x: hidden;
  min-height: 100vh;
}
body::before {
  content: '';
  position: fixed; inset: 0;
  background:
    radial-gradient(ellipse 600px 400px at 10% 20%, rgba(0,229,255,.04) 0%, transparent 70%),
    radial-gradient(ellipse 400px 600px at 90% 80%, rgba(0,255,136,.03) 0%, transparent 70%);
  pointer-events: none; z-index: 0;
}

/* ── Layout ── */
.root { display: grid; grid-template-rows: 48px 1fr; height: 100vh; position: relative; z-index: 1; }
.header { display: flex; align-items: center; padding: 0 20px; gap: 24px; border-bottom: 1px solid var(--b); background: rgba(6,12,18,.95); backdrop-filter: blur(8px); }
.logo { font-family: var(--font-display); font-size: 22px; font-weight: 800; letter-spacing: .02em; color: var(--white); display: flex; align-items: baseline; gap: 10px; }
.logo-sub { font-size: 11px; font-weight: 300; letter-spacing: .2em; color: var(--acc); opacity: .8; }
.header-stats { display: flex; gap: 20px; margin-left: auto; align-items: center; }
.hstat { display: flex; flex-direction: column; align-items: flex-end; }
.hstat-label { font-size: 8px; letter-spacing: .2em; color: var(--muted); text-transform: uppercase; }
.hstat-value { font-size: 14px; font-weight: 600; letter-spacing: -.01em; }
.feed-dot { width: 7px; height: 7px; border-radius: 50%; background: var(--muted); display: inline-block; margin-right: 6px; }
.feed-dot.live { background: var(--grn); box-shadow: 0 0 8px var(--grn); animation: blink 1.5s ease-in-out infinite; }
.feed-dot.mock { background: var(--yel); }
@keyframes blink { 0%,100% { opacity: 1 } 50% { opacity: .3 } }
.btn-ctrl { font-family: var(--font-mono); font-size: 10px; letter-spacing: .12em; text-transform: uppercase; padding: 5px 14px; border: 1px solid var(--b2); background: transparent; color: var(--txt); cursor: pointer; transition: all .15s; }
.btn-ctrl:hover { border-color: var(--acc); color: var(--acc); }
.btn-ctrl.running { border-color: var(--grn); color: var(--grn); background: rgba(0,255,136,.06); }
.btn-ctrl.stop { border-color: var(--red); color: var(--red); background: rgba(255,34,85,.06); }
.btn-reset { font-family: var(--font-mono); font-size: 9px; letter-spacing: .1em; text-transform: uppercase; padding: 4px 10px; border: 1px solid rgba(255,204,0,.2); background: transparent; color: rgba(255,204,0,.5); cursor: pointer; transition: all .15s; }
.btn-reset:hover { border-color: var(--yel); color: var(--yel); }

/* ── Main grid ── */
.main { display: grid; grid-template-columns: 260px 1fr 300px; height: 100%; overflow: hidden; }
.panel { border-right: 1px solid var(--b); overflow-y: auto; overflow-x: hidden; }
.panel:last-child { border-right: none; border-left: 1px solid var(--b); }
.panel-title { font-family: var(--font-display); font-size: 11px; font-weight: 600; letter-spacing: .2em; text-transform: uppercase; color: var(--muted); padding: 12px 16px; border-bottom: 1px solid var(--b); display: flex; align-items: center; justify-content: space-between; position: sticky; top: 0; background: var(--s1); z-index: 10; }

/* ── Price feeds panel ── */
.symbol-block { padding: 14px 16px; border-bottom: 1px solid var(--b); }
.symbol-header { display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 8px; }
.symbol-name { font-family: var(--font-display); font-size: 18px; font-weight: 800; color: var(--white); letter-spacing: .02em; }
.symbol-price { font-size: 15px; font-weight: 600; font-family: var(--font-mono); color: var(--acc); letter-spacing: -.01em; }
.mini-chart { height: 36px; width: 100%; margin-bottom: 8px; }
.signal-row { display: flex; gap: 6px; flex-wrap: wrap; }
.sig-chip { font-size: 9px; font-weight: 600; letter-spacing: .1em; padding: 2px 7px; border-radius: 2px; text-transform: uppercase; }
.sig-yes { background: rgba(0,255,136,.12); color: var(--grn); border: 1px solid rgba(0,255,136,.25); }
.sig-no  { background: rgba(255,34,85,.12); color: var(--red); border: 1px solid rgba(255,34,85,.25); }
.sig-skip { background: rgba(42,74,106,.3); color: var(--muted); border: 1px solid var(--b); }
.sig-conf-high { background: rgba(0,229,255,.08); color: var(--acc); border: 1px solid rgba(0,229,255,.2); }
.sig-conf-med  { background: rgba(255,204,0,.08); color: var(--yel); border: 1px solid rgba(255,204,0,.2); }
.sig-conf-low  { background: rgba(42,74,106,.2); color: var(--muted); border: 1px solid var(--b); }
.indicators { display: grid; grid-template-columns: 1fr 1fr; gap: 4px; margin-top: 6px; }
.ind { background: var(--s2); padding: 4px 8px; }
.ind-label { font-size: 7px; letter-spacing: .15em; color: var(--muted); text-transform: uppercase; }
.ind-value { font-size: 11px; font-weight: 500; color: var(--white); }
.pos { color: var(--grn) !important; }
.neg { color: var(--red) !important; }

/* ── Center: P&L + activity ── */
.center { display: grid; grid-template-rows: auto 1fr; overflow: hidden; }
.pnl-section { padding: 16px 20px; border-bottom: 1px solid var(--b); }
.pnl-grid { display: grid; grid-template-columns: repeat(6, 1fr); gap: 10px; }
.pnl-card { background: var(--s1); border: 1px solid var(--b); padding: 10px 14px; position: relative; overflow: hidden; }
.pnl-card::before { content: ''; position: absolute; top: 0; left: 0; width: 2px; height: 100%; }
.pnl-card.c-grn::before { background: var(--grn); }
.pnl-card.c-red::before { background: var(--red); }
.pnl-card.c-acc::before { background: var(--acc); }
.pnl-card.c-yel::before { background: var(--yel); }
.pnl-card.c-muted::before { background: var(--muted); }
.pnl-label { font-size: 7px; letter-spacing: .2em; color: var(--muted); text-transform: uppercase; margin-bottom: 4px; }
.pnl-value { font-family: var(--font-display); font-size: 26px; font-weight: 800; letter-spacing: -.02em; line-height: 1; }
.pnl-card.c-grn .pnl-value { color: var(--grn); }
.pnl-card.c-red .pnl-value { color: var(--red); }
.pnl-card.c-acc .pnl-value { color: var(--acc); }
.pnl-card.c-yel .pnl-value { color: var(--yel); }
.pnl-card.c-muted .pnl-value { color: var(--muted); }

/* ── Equity chart ── */
.equity-section { padding: 12px 20px; border-bottom: 1px solid var(--b); }
.equity-section canvas { width: 100%; height: 80px; display: block; }

/* ── Positions ── */
.positions-section { overflow-y: auto; flex: 1; }
.pos-list { padding: 8px 12px; display: flex; flex-direction: column; gap: 6px; }
.pos-card { background: var(--s2); border: 1px solid var(--b); padding: 10px 12px; border-left: 2px solid transparent; transition: border-color .2s; animation: fadeSlide .3s ease; }
@keyframes fadeSlide { from { opacity: 0; transform: translateY(-6px) } to { opacity: 1; transform: translateY(0) } }
.pos-card.yes { border-left-color: var(--grn); }
.pos-card.no  { border-left-color: var(--red); }
.pos-card.resolved-win  { border-left-color: var(--grn); opacity: .7; }
.pos-card.resolved-loss { border-left-color: var(--red); opacity: .7; }
.pos-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 4px; }
.pos-sym { font-family: var(--font-display); font-size: 13px; font-weight: 800; color: var(--white); letter-spacing: .05em; }
.pos-pnl { font-size: 12px; font-weight: 600; }
.pos-q { font-size: 9px; color: var(--txt); line-height: 1.4; margin-bottom: 5px; }
.pos-meta { display: flex; gap: 8px; flex-wrap: wrap; }
.pos-tag { font-size: 8px; letter-spacing: .08em; padding: 1px 5px; border-radius: 1px; }
.tag-yes { background: rgba(0,255,136,.1); color: var(--grn); border: 1px solid rgba(0,255,136,.2); }
.tag-no  { background: rgba(255,34,85,.1); color: var(--red); border: 1px solid rgba(255,34,85,.2); }
.tag-open { background: rgba(0,229,255,.06); color: var(--acc); border: 1px solid rgba(0,229,255,.15); }
.tag-closed { background: rgba(42,74,106,.2); color: var(--muted); border: 1px solid var(--b); }
.tag-neutral { background: var(--s1); color: var(--muted); border: 1px solid var(--b); }
.progress-bar { height: 2px; background: var(--b); margin-top: 5px; border-radius: 1px; overflow: hidden; }
.progress-fill { height: 100%; background: var(--acc); transition: width 1s linear; }

/* ── Log panel ── */
.log-list { padding: 6px 10px; display: flex; flex-direction: column; gap: 3px; }
.log-entry { padding: 5px 8px; border-left: 2px solid transparent; background: var(--s1); font-size: 9px; line-height: 1.5; animation: fadeSlide .25s ease; }
.log-entry.BET_PLACED  { border-left-color: var(--acc); }
.log-entry.BET_RESOLVED { border-left-color: var(--grn); }
.log-entry.BET_RESOLVED.loss { border-left-color: var(--red); }
.log-entry.ERROR { border-left-color: var(--red); }
.log-entry.BOT_START { border-left-color: var(--yel); }
.log-ts { color: var(--muted); margin-right: 6px; }
.log-event { font-weight: 600; letter-spacing: .06em; color: var(--acc); margin-right: 6px; }
.log-event.BET_PLACED { color: var(--acc); }
.log-event.BET_RESOLVED { color: var(--grn); }
.log-event.BOT_START { color: var(--yel); }
.log-event.ERROR { color: var(--red); }
.log-msg { color: var(--txt); }

/* ── Scrollbar ── */
::-webkit-scrollbar { width: 4px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { background: var(--b2); border-radius: 2px; }

/* ── No positions ── */
.empty-msg { padding: 24px; text-align: center; color: var(--muted); font-size: 10px; letter-spacing: .1em; line-height: 2; }
</style>
</head>
<body>
<div class="root">
  <!-- HEADER -->
  <header class="header">
    <div class="logo">
      CRYPTO OPTIONS BOT
      <span class="logo-sub">POLYMARKET · AUTONOMOUS</span>
    </div>
    <div class="header-stats">
      <div class="hstat">
        <span class="hstat-label">Balance</span>
        <span class="hstat-value" id="h-balance" style="color:var(--grn)">$100.00</span>
      </div>
      <div class="hstat">
        <span class="hstat-label">P&L</span>
        <span class="hstat-value" id="h-pnl">$0.00</span>
      </div>
      <div class="hstat">
        <span class="hstat-label">Win Rate</span>
        <span class="hstat-value" id="h-winrate" style="color:var(--acc)">—</span>
      </div>
      <div class="hstat">
        <span class="hstat-label">Feed</span>
        <span class="hstat-value" id="h-feed"><span class="feed-dot" id="feed-dot"></span><span id="feed-label">—</span></span>
      </div>
      <button class="btn-ctrl running" id="btn-toggle" onclick="toggleBot()">■ Running</button>
      <button class="btn-reset" onclick="resetBot()">↺ Reset</button>
    </div>
  </header>

  <!-- MAIN -->
  <div class="main">
    <!-- LEFT: Signals -->
    <div class="panel" id="panel-signals">
      <div class="panel-title">
        Live Signals
        <span id="scan-age" style="font-size:8px;font-family:var(--font-mono)">—</span>
      </div>
      <div id="symbols-container"></div>
    </div>

    <!-- CENTER: P&L + Positions -->
    <div class="center">
      <div class="pnl-section">
        <div class="pnl-grid" id="pnl-grid">
          <div class="pnl-card c-grn"><div class="pnl-label">Balance</div><div class="pnl-value" id="c-balance">$100</div></div>
          <div class="pnl-card c-acc"><div class="pnl-label">Total P&L</div><div class="pnl-value" id="c-pnl">$0</div></div>
          <div class="pnl-card c-yel"><div class="pnl-label">Open Bets</div><div class="pnl-value" id="c-open">0</div></div>
          <div class="pnl-card c-grn"><div class="pnl-label">Wins</div><div class="pnl-value" id="c-wins">0</div></div>
          <div class="pnl-card c-red"><div class="pnl-label">Losses</div><div class="pnl-value" id="c-losses">0</div></div>
          <div class="pnl-card c-muted"><div class="pnl-label">Max DD</div><div class="pnl-value" id="c-dd">0%</div></div>
        </div>
      </div>

      <div class="equity-section">
        <div class="panel-title" style="padding:0 0 8px 0;position:relative;background:transparent;border:none;">
          Equity Curve
          <span id="wagered-label" style="font-size:8px"></span>
        </div>
        <canvas id="equity-canvas" height="80"></canvas>
      </div>

      <div class="positions-section">
        <div class="panel-title" style="position:sticky;top:0;">
          Positions
          <span id="pos-count" style="font-size:9px;color:var(--acc)"></span>
        </div>
        <div class="pos-list" id="pos-list"></div>
      </div>
    </div>

    <!-- RIGHT: Log -->
    <div class="panel" id="panel-log">
      <div class="panel-title">Activity Log</div>
      <div class="log-list" id="log-list"></div>
    </div>
  </div>
</div>

<script>
// ── State ──────────────────────────────────────────────────────────────────────
const state = { balance: 100, stats: {}, signals: {}, buffers: {}, open_positions: [], closed_positions: [], log: [], running: true, feed_live: false };
const equityHistory = [100];
const MAX_EQUITY = 200;
let priceHistory = { BTC: [], ETH: [], SOL: [] };
const MAX_PRICES = 60;

// ── SSE ────────────────────────────────────────────────────────────────────────
function connect() {
  const es = new EventSource('/stream');
  es.addEventListener('state', e => { applyState(JSON.parse(e.data)); });
  es.addEventListener('tick', e => {
    const d = JSON.parse(e.data);
    if (priceHistory[d.symbol]) {
      priceHistory[d.symbol].push(d.price);
      if (priceHistory[d.symbol].length > MAX_PRICES) priceHistory[d.symbol].shift();
    }
  });
  es.onerror = () => { setTimeout(connect, 3000); es.close(); };
}
connect();

// Poll full state every 5s as fallback
setInterval(async () => {
  try {
    const r = await fetch('/api/state');
    const d = await r.json();
    applyState(d);
  } catch {}
}, 5000);

// ── Apply state ────────────────────────────────────────────────────────────────
function applyState(d) {
  Object.assign(state, d);
  const pnl = (d.stats?.total_pnl ?? 0);
  const balance = d.balance ?? 100;

  // Update equity history
  equityHistory.push(balance);
  if (equityHistory.length > MAX_EQUITY) equityHistory.shift();

  // Header
  document.getElementById('h-balance').textContent = '$' + balance.toFixed(2);
  const pnlEl = document.getElementById('h-pnl');
  pnlEl.textContent = (pnl >= 0 ? '+' : '') + '$' + pnl.toFixed(2);
  pnlEl.style.color = pnl >= 0 ? 'var(--grn)' : 'var(--red)';
  const wins = d.stats?.wins ?? 0, losses = d.stats?.losses ?? 0;
  const wr = wins + losses > 0 ? (wins / (wins + losses) * 100).toFixed(0) + '%' : '—';
  document.getElementById('h-winrate').textContent = wr;

  // Feed indicator
  const feedLive = d.feed_live;
  document.getElementById('feed-dot').className = 'feed-dot ' + (feedLive ? 'live' : 'mock');
  document.getElementById('feed-label').textContent = feedLive ? 'LIVE' : 'MOCK';

  // Bot toggle
  const btn = document.getElementById('btn-toggle');
  btn.textContent = d.running ? '■ Running' : '▶ Start';
  btn.className = 'btn-ctrl ' + (d.running ? 'stop' : 'running');

  // Big stats
  const pnlCard = document.getElementById('c-pnl');
  document.getElementById('c-balance').textContent = '$' + balance.toFixed(0);
  pnlCard.textContent = (pnl >= 0 ? '+$' : '-$') + Math.abs(pnl).toFixed(2);
  pnlCard.style.color = pnl >= 0 ? 'var(--grn)' : 'var(--red)';
  document.getElementById('c-open').textContent = (d.open_positions ?? []).length;
  document.getElementById('c-wins').textContent = wins;
  document.getElementById('c-losses').textContent = losses;
  const dd = d.stats?.max_drawdown ?? 0;
  document.getElementById('c-dd').textContent = (dd * 100).toFixed(1) + '%';

  const wagered = d.stats?.total_wagered ?? 0;
  document.getElementById('wagered-label').textContent = wagered > 0 ? `wagered $${wagered.toFixed(2)}` : '';

  // Signals panel
  renderSignals(d.signals ?? {}, d.buffers ?? {});

  // Positions
  renderPositions(d.open_positions ?? [], d.closed_positions ?? []);

  // Log
  renderLog(d.log ?? []);

  // Equity chart
  drawEquity();

  // Scan age
  const lastScan = d.stats?.last_scan_ts ?? 0;
  if (lastScan > 0) {
    const ago = Math.round((Date.now() / 1000) - lastScan);
    document.getElementById('scan-age').textContent = `scan ${ago}s ago`;
  }
}

// ── Signals ────────────────────────────────────────────────────────────────────
function renderSignals(signals, buffers) {
  const container = document.getElementById('symbols-container');
  let html = '';
  for (const sym of ['BTC', 'ETH', 'SOL']) {
    const buf = buffers[sym] || {};
    const sig = signals[sym] || {};
    const price = buf.price ?? priceHistory[sym]?.at(-1);
    const priceStr = price ? formatPrice(sym, price) : '—';
    const dir = sig.direction ?? 'SKIP';
    const dirClass = dir === 'YES' ? 'sig-yes' : dir === 'NO' ? 'sig-no' : 'sig-skip';
    const confClass = sig.confidence === 'high' ? 'sig-conf-high' : sig.confidence === 'medium' ? 'sig-conf-med' : 'sig-conf-low';
    const mom1 = sig.momentum_1m ?? buf.mom_1m ?? 0;
    const mom5 = sig.momentum_5m ?? buf.mom_5m ?? 0;
    const rsi = sig.rsi ?? 50;
    const vwap = sig.vwap_dev ?? 0;
    const bb = sig.bb_position ?? 0.5;
    const reasons = (sig.reasons ?? []).join(' · ');
    const ticks = buf.ticks ?? 0;

    html += `
    <div class="symbol-block">
      <div class="symbol-header">
        <span class="symbol-name">${sym}</span>
        <span class="symbol-price">${priceStr}</span>
      </div>
      <canvas class="mini-chart" id="chart-${sym}" height="36"></canvas>
      <div class="signal-row">
        <span class="sig-chip ${dirClass}">${dir === 'YES' ? '▲ YES' : dir === 'NO' ? '▼ NO' : '— SKIP'}</span>
        <span class="sig-chip ${confClass}">${sig.confidence ?? 'low'}</span>
        ${sig.edge != null ? `<span class="sig-chip" style="background:rgba(0,0,0,.3);color:var(--txt);border:1px solid var(--b)">edge ${(sig.edge*100).toFixed(1)}%</span>` : ''}
      </div>
      <div class="indicators">
        <div class="ind"><div class="ind-label">1m mom</div><div class="ind-value ${mom1>0?'pos':mom1<0?'neg':''}">${mom1>=0?'+':''}${mom1.toFixed(2)}%</div></div>
        <div class="ind"><div class="ind-label">5m mom</div><div class="ind-value ${mom5>0?'pos':mom5<0?'neg':''}">${mom5>=0?'+':''}${mom5.toFixed(2)}%</div></div>
        <div class="ind"><div class="ind-label">RSI</div><div class="ind-value ${rsi>65?'neg':rsi<35?'pos':''}">${rsi.toFixed(0)}</div></div>
        <div class="ind"><div class="ind-label">VWAP dev</div><div class="ind-value ${vwap>0?'neg':vwap<0?'pos':''}">${vwap>=0?'+':''}${vwap.toFixed(2)}%</div></div>
        <div class="ind"><div class="ind-label">BB pos</div><div class="ind-value">${(bb*100).toFixed(0)}%</div></div>
        <div class="ind"><div class="ind-label">Ticks</div><div class="ind-value" style="color:var(--muted)">${ticks}</div></div>
      </div>
      ${reasons ? `<div style="font-size:8px;color:var(--muted);margin-top:5px;line-height:1.6;">${reasons}</div>` : ''}
    </div>`;
  }
  container.innerHTML = html;
  // Draw mini charts
  for (const sym of ['BTC', 'ETH', 'SOL']) {
    const canvas = document.getElementById('chart-' + sym);
    if (canvas && priceHistory[sym]?.length > 1) drawMiniChart(canvas, priceHistory[sym]);
  }
}

function formatPrice(sym, price) {
  if (sym === 'BTC') return '$' + price.toLocaleString('en', {maximumFractionDigits: 0});
  if (sym === 'ETH') return '$' + price.toLocaleString('en', {maximumFractionDigits: 1});
  return '$' + price.toFixed(2);
}

// ── Mini sparkline ─────────────────────────────────────────────────────────────
function drawMiniChart(canvas, prices) {
  const ctx = canvas.getContext('2d');
  const w = canvas.offsetWidth || 228;
  const h = 36;
  canvas.width = w;
  canvas.height = h;
  ctx.clearRect(0, 0, w, h);
  if (prices.length < 2) return;

  const min = Math.min(...prices), max = Math.max(...prices);
  const range = max - min || 1;
  const pts = prices.map((p, i) => [i / (prices.length - 1) * w, h - ((p - min) / range * (h - 4) + 2)]);

  const last = pts[pts.length - 1][1], first = pts[0][1];
  const isUp = last <= first;
  const color = isUp ? '#00ff88' : '#ff2255';

  const grad = ctx.createLinearGradient(0, 0, 0, h);
  grad.addColorStop(0, isUp ? 'rgba(0,255,136,.25)' : 'rgba(255,34,85,.25)');
  grad.addColorStop(1, 'rgba(0,0,0,0)');

  ctx.beginPath();
  ctx.moveTo(pts[0][0], pts[0][1]);
  pts.slice(1).forEach(([x, y]) => ctx.lineTo(x, y));
  ctx.lineTo(w, h);
  ctx.lineTo(0, h);
  ctx.closePath();
  ctx.fillStyle = grad;
  ctx.fill();

  ctx.beginPath();
  ctx.moveTo(pts[0][0], pts[0][1]);
  pts.slice(1).forEach(([x, y]) => ctx.lineTo(x, y));
  ctx.strokeStyle = color;
  ctx.lineWidth = 1.5;
  ctx.stroke();
}

// ── Equity chart ───────────────────────────────────────────────────────────────
function drawEquity() {
  const canvas = document.getElementById('equity-canvas');
  if (!canvas) return;
  const ctx = canvas.getContext('2d');
  const w = canvas.offsetWidth || 600;
  const h = 80;
  canvas.width = w; canvas.height = h;
  ctx.clearRect(0, 0, w, h);
  if (equityHistory.length < 2) return;

  const min = Math.min(...equityHistory) * 0.99;
  const max = Math.max(...equityHistory) * 1.01;
  const range = max - min || 1;
  const pts = equityHistory.map((v, i) => [
    i / (equityHistory.length - 1) * w,
    h - ((v - min) / range * (h - 4) + 2)
  ]);

  const current = equityHistory[equityHistory.length - 1];
  const isProfit = current >= 100;
  const color = isProfit ? '#00ff88' : '#ff2255';

  // Baseline at $100
  const baseline = h - ((100 - min) / range * (h - 4) + 2);
  ctx.beginPath();
  ctx.moveTo(0, baseline);
  ctx.lineTo(w, baseline);
  ctx.strokeStyle = 'rgba(42,74,106,.5)';
  ctx.lineWidth = 1;
  ctx.setLineDash([4, 4]);
  ctx.stroke();
  ctx.setLineDash([]);

  const grad = ctx.createLinearGradient(0, 0, 0, h);
  grad.addColorStop(0, isProfit ? 'rgba(0,255,136,.2)' : 'rgba(255,34,85,.2)');
  grad.addColorStop(1, 'rgba(0,0,0,0)');

  ctx.beginPath();
  ctx.moveTo(pts[0][0], pts[0][1]);
  pts.slice(1).forEach(([x, y]) => ctx.lineTo(x, y));
  ctx.lineTo(w, h); ctx.lineTo(0, h);
  ctx.closePath();
  ctx.fillStyle = grad;
  ctx.fill();

  ctx.beginPath();
  ctx.moveTo(pts[0][0], pts[0][1]);
  pts.slice(1).forEach(([x, y]) => ctx.lineTo(x, y));
  ctx.strokeStyle = color;
  ctx.lineWidth = 2;
  ctx.stroke();

  // Current value label
  ctx.fillStyle = color;
  ctx.font = '600 10px IBM Plex Mono';
  ctx.fillText('$' + current.toFixed(2), w - 70, pts[pts.length-1][1] - 4);
}

// ── Positions ──────────────────────────────────────────────────────────────────
function renderPositions(open, closed) {
  const list = document.getElementById('pos-list');
  const count = document.getElementById('pos-count');
  count.textContent = open.length > 0 ? `${open.length} open` : '';

  if (open.length === 0 && closed.length === 0) {
    list.innerHTML = '<div class="empty-msg">No positions yet.<br>Bot is scanning markets every 30s.</div>';
    return;
  }

  const now = Date.now() / 1000;
  const renderPos = (p, isOpen) => {
    const pnlColor = p.pnl == null ? 'var(--txt)' : p.pnl >= 0 ? 'var(--grn)' : 'var(--red)';
    const pnlStr = p.pnl != null ? (p.pnl >= 0 ? '+$' : '-$') + Math.abs(p.pnl).toFixed(2) : 'open';
    let progress = 0;
    if (isOpen) {
      const age = now - p.entry_ts;
      progress = Math.min(100, age / (p.horizon_minutes * 60) * 100);
    }
    const cardClass = isOpen ? p.side.toLowerCase() :
      (p.pnl >= 0 ? 'resolved-win' : 'resolved-loss');

    return `
    <div class="pos-card ${cardClass}">
      <div class="pos-header">
        <span class="pos-sym">${p.symbol}</span>
        <span class="pos-pnl" style="color:${pnlColor}">${pnlStr}</span>
      </div>
      <div class="pos-q">${p.question}</div>
      <div class="pos-meta">
        <span class="pos-tag ${p.side === 'YES' ? 'tag-yes' : 'tag-no'}">${p.side}</span>
        <span class="pos-tag ${isOpen ? 'tag-open' : 'tag-closed'}">${isOpen ? 'OPEN' : 'CLOSED'}</span>
        <span class="pos-tag tag-neutral">$${p.amount.toFixed(2)}</span>
        <span class="pos-tag tag-neutral">edge ${(p.signal_edge*100).toFixed(1)}%</span>
        <span class="pos-tag tag-neutral">${p.horizon_minutes}min</span>
      </div>
      ${isOpen ? `<div class="progress-bar"><div class="progress-fill" style="width:${progress.toFixed(0)}%"></div></div>` : ''}
    </div>`;
  };

  list.innerHTML =
    open.map(p => renderPos(p, true)).join('') +
    closed.slice(-15).reverse().map(p => renderPos(p, false)).join('');
}

// ── Log ────────────────────────────────────────────────────────────────────────
function renderLog(logs) {
  const list = document.getElementById('log-list');
  const recent = [...logs].reverse().slice(0, 60);
  list.innerHTML = recent.map(e => {
    const isLoss = e.message.includes('LOSS') || e.message.includes('Lost');
    return `
    <div class="log-entry ${e.event}${isLoss ? ' loss' : ''}">
      <span class="log-ts">${e.ts_str}</span>
      <span class="log-event ${e.event}">${e.event}</span>
      <span class="log-msg">${e.message}</span>
    </div>`;
  }).join('');
}

// ── Controls ───────────────────────────────────────────────────────────────────
async function toggleBot() {
  await fetch('/api/bot/toggle', { method: 'POST' });
}
async function resetBot() {
  if (!confirm('Reset all positions and P&L?')) return;
  await fetch('/api/bot/reset', { method: 'POST' });
  equityHistory.length = 0; equityHistory.push(100);
}
</script>
</body>
</html>"""


if __name__ == "__main__":
    print("🚀 Crypto Options Bot dashboard → http://localhost:5001")
    print("   Symbols:", SYMBOLS)
    print("   Auto-starts Binance feed + autonomous bot")
    app.run(debug=False, port=5001, threaded=True)