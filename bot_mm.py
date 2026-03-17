"""
bot_mm.py — Market Maker Profissional para Polymarket
======================================================
Ctrl+C → cancel_all() imediato + shutdown limpo.
Dashboard: http://localhost:5002
"""
from __future__ import annotations

import json
import logging
import os
import signal
import sys
import threading
import time
from collections import deque
from threading import Event

from dotenv import load_dotenv
from flask import Flask, Response, jsonify

load_dotenv()

# ── Logging ────────────────────────────────────────────────────────────────────

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("bot_mm.log", encoding="utf-8"),
    ],
)
# Silencia logs verbose do Flask/Werkzeug
logging.getLogger("werkzeug").setLevel(logging.ERROR)
logging.getLogger("flask.app").setLevel(logging.ERROR)
logger = logging.getLogger("bot_mm")

# ── Configuração ───────────────────────────────────────────────────────────────

DRY_RUN             = os.getenv("DRY_RUN", "true").lower() == "true"
POLYGON_PRIVATE_KEY = os.getenv("POLYGON_PRIVATE_KEY", "")
POLYGON_ADDRESS     = os.getenv("POLYGON_ADDRESS", "")

MAX_USDC_PER_MARKET = float(os.getenv("MM_MAX_USDC_PER_MARKET", "8.0"))
MAX_TOTAL_USDC      = float(os.getenv("MM_MAX_TOTAL_USDC", "20.0"))
ORDER_USDC          = float(os.getenv("MM_ORDER_USDC", "5.0"))

QUOTE_INTERVAL      = int(os.getenv("MM_QUOTE_INTERVAL", "15"))
FILL_POLL_INTERVAL  = int(os.getenv("MM_FILL_POLL_INTERVAL", "15"))
MARKET_REFRESH_MIN  = int(os.getenv("MM_MARKET_REFRESH_MIN", "5"))
DASHBOARD_PORT      = int(os.getenv("MM_DASHBOARD_PORT", "5003"))

# ── Imports internos ───────────────────────────────────────────────────────────

from src.feed.polymarket import ClobFeed, fetch_gamma_markets, CLOB_URL
from src.mm.intelligence_service import IntelligenceService
from src.mm.risk_manager import RiskManager, RiskConfig
from src.mm.market_engine import MarketEngine


# ── CLOB Client ────────────────────────────────────────────────────────────────

class MockClobClient:
    def create_order(self, args):           return args
    def post_order(self, order, *a, **kw):  return {"orderID": f"mock_{int(time.time()*1000)}"}
    def cancel(self, order_id):             pass
    def cancel_all(self):                   pass
    def get_orders(self, params=None):      return []
    def get_order(self, order_id):          return {"status": "matched"}


def build_clob_client():
    if DRY_RUN:
        logger.info("DRY_RUN ativo — MockClobClient")
        return MockClobClient()
    if not POLYGON_PRIVATE_KEY:
        logger.error("POLYGON_PRIVATE_KEY ausente — forçando DRY_RUN")
        return MockClobClient()
    try:
        from py_clob_client.client import ClobClient
        client = ClobClient(host=CLOB_URL, key=POLYGON_PRIVATE_KEY, chain_id=137, signature_type=0)
        try:
            creds = client.derive_api_key()
        except Exception:
            creds = client.create_api_key()
        client.set_api_creds(creds)
        logger.info("CLOB autenticado")
        return client
    except Exception as e:
        logger.error("CLOB init falhou: %s — usando mock", e)
        return MockClobClient()


# ── Dashboard HTML ─────────────────────────────────────────────────────────────

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="pt">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>MM Bot</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    background: #0d0d0d; color: #e0e0e0;
    font-family: 'SF Mono', 'Fira Code', monospace;
    font-size: 13px; padding: 20px;
  }
  header {
    display: flex; align-items: center; gap: 12px;
    border-bottom: 1px solid #1f1f1f; padding-bottom: 14px; margin-bottom: 20px;
  }
  header h1 { font-size: 15px; font-weight: 600; letter-spacing: 1px; color: #fff; }
  .badge {
    font-size: 10px; font-weight: 700; padding: 3px 8px;
    border-radius: 4px; letter-spacing: 0.5px;
  }
  .badge-dry  { background: #2a2a00; color: #f5c400; border: 1px solid #f5c40044; }
  .badge-real { background: #002a0a; color: #00e676; border: 1px solid #00e67644; }
  .badge-off  { background: #1a0000; color: #ff5252; border: 1px solid #ff525244; }
  .dot { width: 8px; height: 8px; border-radius: 50%; background: #555; }
  .dot.ok  { background: #00e676; box-shadow: 0 0 6px #00e676; animation: pulse 2s infinite; }
  .dot.err { background: #ff5252; }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.4} }

  .stats-grid {
    display: grid; grid-template-columns: repeat(5, 1fr);
    gap: 10px; margin-bottom: 20px;
  }
  @media(max-width:900px){ .stats-grid { grid-template-columns: repeat(3,1fr); } }
  .card {
    background: #141414; border: 1px solid #1f1f1f;
    border-radius: 6px; padding: 14px 16px;
  }
  .card .label { font-size: 10px; color: #555; text-transform: uppercase; letter-spacing: 0.8px; margin-bottom: 6px; }
  .card .value { font-size: 22px; font-weight: 700; color: #fff; }
  .card .sub   { font-size: 11px; color: #444; margin-top: 4px; }
  .pnl-pos { color: #00e676 !important; }
  .pnl-neg { color: #ff5252 !important; }
  .pnl-neu { color: #888 !important; }

  .section-title {
    font-size: 10px; text-transform: uppercase; letter-spacing: 1px;
    color: #444; margin-bottom: 10px;
  }
  table { width: 100%; border-collapse: collapse; margin-bottom: 20px; }
  th {
    font-size: 10px; text-transform: uppercase; letter-spacing: 0.8px;
    color: #444; text-align: left; padding: 6px 10px;
    border-bottom: 1px solid #1a1a1a;
  }
  td { padding: 8px 10px; border-bottom: 1px solid #141414; color: #bbb; }
  tr:hover td { background: #161616; }
  td.sym  { color: #fff; font-weight: 600; }
  td.bid  { color: #4caf50; }
  td.ask  { color: #ef5350; }
  td.spr  { color: #888; }
  td.agg  { color: #00bcd4; }
  td.def  { color: #ff9800; }
  .skew-bar {
    display: inline-block; width: 60px; height: 6px;
    background: #1a1a1a; border-radius: 3px; vertical-align: middle; position: relative;
  }
  .skew-fill {
    position: absolute; top: 0; height: 100%; border-radius: 3px;
  }
  .skew-pos { background: #ef5350; }
  .skew-neg { background: #4caf50; }

  .log-box {
    background: #0a0a0a; border: 1px solid #1a1a1a;
    border-radius: 6px; padding: 12px; height: 220px;
    overflow-y: auto; font-size: 11px; line-height: 1.7;
  }
  .log-box .ts   { color: #333; }
  .log-box .fill { color: #00e676; }
  .log-box .quot { color: #4a9eff; }
  .log-box .warn { color: #ff9800; }
  .log-box .err  { color: #ff5252; }
  .log-box .info { color: #666; }

  .footer {
    margin-top: 20px; font-size: 10px; color: #333;
    display: flex; justify-content: space-between;
  }
  #upd { color: #2a2a2a; }
</style>
</head>
<body>
<header>
  <div class="dot" id="dot"></div>
  <h1>POLYMARKET · MARKET MAKER</h1>
  <span class="badge" id="mode-badge">—</span>
  <span style="margin-left:auto;color:#333;font-size:11px" id="uptime">—</span>
  <button id="stop-btn" onclick="stopBot()" style="
    margin-left:12px; background:#1a0000; color:#ff5252;
    border:1px solid #ff525244; border-radius:4px;
    padding:5px 14px; font-family:inherit; font-size:11px;
    font-weight:700; letter-spacing:0.5px; cursor:pointer;
  ">⏹ STOP</button>
</header>

<div class="stats-grid">
  <div class="card">
    <div class="label">P&amp;L Realizado</div>
    <div class="value" id="pnl">—</div>
    <div class="sub" id="fills-sub">0 fills</div>
  </div>
  <div class="card">
    <div class="label">Saldo Wallet</div>
    <div class="value" id="wallet-usdc">—</div>
    <div class="sub" id="wallet-addr" style="color:#333;font-size:10px">—</div>
    <div class="sub" id="capital-max" style="color:#555">limite: —</div>
  </div>
  <div class="card">
    <div class="label">Em Ordens Abertas</div>
    <div class="value" id="capital-reserved" style="color:#f5c400">—</div>
    <div class="sub" id="capital-spent" style="color:#888">fills executados: —</div>
    <div class="sub" id="capital-free" style="color:#3a3">disponível p/ novas: —</div>
  </div>
  <div class="card">
    <div class="label">Ordens / Mercados</div>
    <div class="value" id="orders">—</div>
    <div class="sub" id="orders-sub">0 colocadas</div>
    <div class="sub" id="markets-sub" style="color:#555">— mercados</div>
  </div>
  <div class="card">
    <div class="label">LLM</div>
    <div class="value" id="llm-mode" style="font-size:14px">—</div>
    <div class="sub" id="llm-sub">—</div>
  </div>
</div>

<div class="section-title">Mercados Ativos</div>
<table id="tbl">
  <thead>
    <tr>
      <th>Símbolo</th><th>Mid</th><th>Bid</th><th>Ask</th>
      <th>Spread</th><th>Modo LLM</th><th>Skew</th>
      <th>Shares YES</th><th>P&L</th><th>Expira</th>
    </tr>
  </thead>
  <tbody id="tbl-body">
    <tr><td colspan="10" style="color:#333;text-align:center;padding:20px">
      aguardando dados…
    </td></tr>
  </tbody>
</table>

<div class="section-title">Atividade Recente</div>
<div class="log-box" id="log"></div>

<div class="footer">
  <span>http://localhost:""" + str(DASHBOARD_PORT) + """</span>
  <span id="upd">—</span>
</div>

<script>
const $ = id => document.getElementById(id);
const fmt = (n, d=3) => n == null ? '—' : Number(n).toFixed(d);
const fmtUSD = n => n == null ? '—' : '$' + fmt(n, 2);

let lastLog = [];

function pnlClass(v) {
  if (v > 0.0001) return 'pnl-pos';
  if (v < -0.0001) return 'pnl-neg';
  return 'pnl-neu';
}

function skewBar(skew) {
  const pct = Math.abs(skew) * 50;
  const side = skew >= 0 ? 'right' : 'left';
  const cls  = skew >= 0 ? 'skew-pos' : 'skew-neg';
  return `<span class="skew-bar">
    <span class="skew-fill ${cls}" style="width:${pct}%;${side}:50%"></span>
  </span> ${fmt(skew,2)}`;
}

async function stopBot() {
  const btn = $('stop-btn');
  btn.textContent = 'parando...';
  btn.disabled = true;
  btn.style.opacity = '0.5';
  try {
    await fetch('/api/stop', { method: 'POST' });
  } catch(e) {}
}

async function refresh() {
  try {
    const r = await fetch('/api/state');
    const d = await r.json();

    // Dot
    $('dot').className = 'dot ' + (d.running ? 'ok' : 'err');
    const btn = $('stop-btn');
    if (!d.running) { btn.textContent = 'ENCERRADO'; btn.disabled = true; btn.style.opacity = '0.3'; }

    // Badge
    const mb = $('mode-badge');
    if (!d.running) { mb.textContent = 'OFFLINE'; mb.className = 'badge badge-off'; }
    else if (d.dry_run) { mb.textContent = 'DRY RUN'; mb.className = 'badge badge-dry'; }
    else { mb.textContent = 'LIVE'; mb.className = 'badge badge-real'; }

    // Uptime
    const s = d.uptime_sec || 0;
    const h = Math.floor(s/3600), m = Math.floor((s%3600)/60), sc = Math.floor(s%60);
    $('uptime').textContent = `${String(h).padStart(2,'0')}:${String(m).padStart(2,'0')}:${String(sc).padStart(2,'0')}`;

    // Stats cards
    const pnl = d.total_pnl || 0;
    $('pnl').textContent = (pnl >= 0 ? '+' : '') + fmtUSD(pnl);
    $('pnl').className   = 'value ' + pnlClass(pnl);
    $('fills-sub').textContent = `${d.fills_total || 0} fills executados`;

    // Saldo wallet
    const walletUSD = d.wallet_usdc;
    $('wallet-usdc').textContent = walletUSD != null ? fmtUSD(walletUSD) : (d.dry_run ? '(simulado)' : '—');
    $('wallet-addr').textContent = d.wallet_address || '—';
    $('capital-max').textContent = `limite banca: $${fmt(d.capital_max, 0)}`;

    // Capital breakdown
    const reserved = d.capital_reserved || 0;
    const spent    = d.capital_spent    || 0;
    const capFree  = Math.max(0, (d.capital_max || 0) - (d.capital_used || 0));
    $('capital-reserved').textContent = fmtUSD(reserved);
    $('capital-spent').textContent    = `fills executados: ${fmtUSD(spent)}`;
    $('capital-free').textContent     = `disponível p/ novas: ${fmtUSD(capFree)}`;

    $('orders').textContent     = d.active_orders ?? '—';
    $('orders-sub').textContent = `${d.total_placed || 0} colocadas`;
    $('markets-sub').textContent = `${d.market_count ?? 0} mercados`;

    // LLM
    $('llm-mode').textContent = d.llm_primary || '—';
    $('llm-sub').textContent  = d.llm_status  || '—';

    // Tabela de mercados
    const rows = d.markets || [];
    const tbody = $('tbl-body');
    if (rows.length === 0) {
      tbody.innerHTML = '<tr><td colspan="10" style="color:#333;text-align:center;padding:20px">sem mercados</td></tr>';
    } else {
      tbody.innerHTML = rows.map(m => {
        const modeCell = m.llm_mode === 'aggressive'
          ? `<td class="agg">agg</td>` : `<td class="def">def</td>`;
        const pnlTd = `<td class="${pnlClass(m.pnl)}">${m.pnl >= 0 ? '+' : ''}${fmt(m.pnl,4)}</td>`;
        return `<tr>
          <td class="sym">${m.symbol}</td>
          <td>${fmt(m.mid)}</td>
          <td class="bid">${fmt(m.bid)}</td>
          <td class="ask">${fmt(m.ask)}</td>
          <td class="spr">${fmt(m.spread,4)}</td>
          ${modeCell}
          <td>${skewBar(m.skew)}</td>
          <td>${fmt(m.yes_shares, 1)}</td>
          ${pnlTd}
          <td style="color:#444">${m.mins_left}m</td>
        </tr>`;
      }).join('');
    }

    // Log
    const logs = d.log || [];
    if (JSON.stringify(logs) !== JSON.stringify(lastLog)) {
      lastLog = logs;
      const box = $('log');
      box.innerHTML = logs.slice().reverse().map(l => {
        const cls = l.type || 'info';
        return `<div><span class="ts">${l.ts}</span> <span class="${cls}">${l.msg}</span></div>`;
      }).join('');
    }

    $('upd').textContent = 'atualizado ' + new Date().toLocaleTimeString('pt-BR');

  } catch(e) {
    $('dot').className = 'dot err';
  }
}

refresh();
setInterval(refresh, 2000);
</script>
</body>
</html>"""


# ── Bot Principal ──────────────────────────────────────────────────────────────

class MarketMakerBot:

    def __init__(self) -> None:
        self._shutdown_event = Event()
        self._start_ts       = time.time()

        self.intel  = IntelligenceService()
        self.risk   = RiskManager(RiskConfig(
            max_usdc_per_market=MAX_USDC_PER_MARKET,
            max_total_usdc=MAX_TOTAL_USDC,
            order_usdc=ORDER_USDC,
        ))
        clob = build_clob_client()
        self.engine = MarketEngine(
            clob_client=clob,
            risk_manager=self.risk,
            intelligence_service=self.intel,
            dry_run=DRY_RUN,
            on_fill_cb=self._on_fill,
            on_quote_cb=self._on_quote,
            on_allowance_needed=self._fix_allowance_for_token,
        )
        if not DRY_RUN:
            self._recover_state(clob)
            self._analyze_recovered_positions()
            self._setup_allowances(clob)

        self._markets:            list  = []
        self._markets_meta:       dict  = {}
        self._last_market_refresh = 0.0
        self._last_fill_poll      = 0.0
        self._last_quote_cycle    = 0.0
        self._last_status_log     = 0.0
        self._last_balance_refresh = 0.0
        self._clob_feed: ClobFeed | None = None
        self._fills_total         = 0

        # Saldo real da wallet (atualizado via API em live mode)
        self._wallet_usdc: float | None = None
        self._clob_ref = clob  # referência para consultas de saldo

        # Estado do dashboard por mercado: token_id → {...}
        self._market_state: dict[str, dict] = {}
        self._lock_state    = threading.Lock()

        # Log circular para o dashboard (últimos 60 eventos)
        self._activity_log: deque = deque(maxlen=60)

    # ── Dashboard state ───────────────────────────────────────────────────────

    def _log_activity(self, msg: str, kind: str = "info") -> None:
        ts = time.strftime("%H:%M:%S")
        with self._lock_state:
            self._activity_log.append({"ts": ts, "msg": msg, "type": kind})

    def get_state(self) -> dict:
        """Snapshot do estado para o dashboard."""
        risk_stats  = self.risk.stats()
        engine_stats = self.engine.stats()

        with self._lock_state:
            log = list(self._activity_log)
            mkt_state = dict(self._market_state)

        markets_rows = []
        for m in self._markets:
            ms    = mkt_state.get(m.token_id, {})
            pos   = risk_stats["positions"].get(m.token_id, {})
            skew  = self.risk.get_inventory_skew(m.token_id)
            book  = self.engine._current_book.get(m.token_id)
            row   = {
                "symbol":    m.symbol,
                "mid":       round(book.mid, 3) if book else round(m.yes_price, 3),
                "bid":       round(ms.get("bid", 0), 3),
                "ask":       round(ms.get("ask", 0), 3),
                "spread":    round(ms.get("spread", 0), 4),
                "llm_mode":  ms.get("mode", "—"),
                "skew":      round(skew, 3),
                "yes_shares": round(pos.get("yes_shares", 0), 2),
                "pnl":       round(pos.get("realized_pnl", 0), 4),
                "mins_left": round(m.mins_left(), 1),
            }
            markets_rows.append(row)

        markets_rows.sort(key=lambda r: r["mins_left"])

        llm_primary = "groq" if os.getenv("GROQ_API_KEY") else "gemini"
        llm_status  = "cache 5min" if not DRY_RUN else "simulado"

        # Saldo wallet: real em live mode, estimativa em dry run
        if DRY_RUN:
            wallet_usdc = MAX_TOTAL_USDC - risk_stats["total_usdc_spent"]
        else:
            wallet_usdc = self._wallet_usdc

        addr_display = (POLYGON_ADDRESS[:6] + "…" + POLYGON_ADDRESS[-4:]) if POLYGON_ADDRESS else "—"

        return {
            "running":           not self._shutdown_event.is_set(),
            "dry_run":           DRY_RUN,
            "uptime_sec":        int(time.time() - self._start_ts),
            "fills_total":       self._fills_total,
            "total_pnl":         risk_stats["total_realized_pnl"],
            "wallet_usdc":       wallet_usdc,
            "capital_used":      risk_stats["capital_committed"],
            "capital_spent":     risk_stats["total_usdc_spent"],
            "capital_reserved":  risk_stats["open_buy_value"],
            "capital_max":       MAX_TOTAL_USDC,
            "active_orders":     engine_stats["active_orders"],
            "total_placed":      engine_stats["total_placed"],
            "market_count":      len(self._markets),
            "llm_primary":       llm_primary,
            "llm_status":        llm_status,
            "wallet_address":    addr_display,
            "markets":           markets_rows,
            "log":               log,
        }

    # ── Flask Dashboard ───────────────────────────────────────────────────────

    def start_dashboard(self) -> None:
        flask_app = Flask("mm_dashboard")

        @flask_app.get("/")
        def index():
            return DASHBOARD_HTML, 200, {"Content-Type": "text/html; charset=utf-8"}

        @flask_app.get("/api/state")
        def state():
            return jsonify(self.get_state())

        @flask_app.post("/api/stop")
        def stop():
            threading.Thread(target=self._shutdown, daemon=True, name="StopBtn").start()
            return jsonify({"ok": True})

        def _run_flask():
            from waitress import serve
            for port in (DASHBOARD_PORT, DASHBOARD_PORT + 1, DASHBOARD_PORT + 2, 8080):
                try:
                    logger.info("Dashboard ativo em http://localhost:%d", port)
                    serve(flask_app, host="127.0.0.1", port=port, threads=4)
                    break
                except OSError:
                    logger.warning("Dashboard porta %d ocupada — tentando %d", port, port + 1)
                except Exception as e:
                    logger.error("Dashboard erro: %s", e)
                    break

        t = threading.Thread(target=_run_flask, daemon=True, name="Dashboard")
        t.start()
        logger.info("Dashboard: http://localhost:%d", DASHBOARD_PORT)

    # ── Signal Handlers ───────────────────────────────────────────────────────

    def setup_signal_handlers(self) -> None:
        def _handler(signum, frame):
            logger.warning("Sinal %s — shutdown", signum)
            self._shutdown()
        signal.signal(signal.SIGINT,  _handler)
        signal.signal(signal.SIGTERM, _handler)

    def _shutdown(self) -> None:
        logger.warning("=" * 50)
        logger.warning("SHUTDOWN — cancelando ordens...")
        self._log_activity("Shutdown iniciado — cancelando ordens", "warn")
        try:
            n = self.engine.cancel_all()
            logger.warning("%d ordens canceladas", n)
            self._log_activity(f"{n} ordens canceladas — bot encerrado", "warn")
        except Exception as e:
            logger.error("cancel_all erro: %s", e)
        finally:
            self._shutdown_event.set()

    # ── Market Discovery ──────────────────────────────────────────────────────

    def _refresh_markets(self) -> None:
        logger.info("Buscando mercados...")
        try:
            all_markets = fetch_gamma_markets(
                limit=300, min_liquidity=50.0, max_days=7.0, _debug=False,
            )
        except Exception as e:
            logger.error("fetch_gamma_markets: %s", e)
            self._log_activity(f"Erro ao buscar mercados: {e}", "err")
            return

        viable = [
            m for m in all_markets
            if 0.10 <= m.yes_price <= 0.90 and m.mins_left() > 30
        ]
        viable.sort(key=lambda m: m.liquidity, reverse=True)
        self._markets = viable[:10]

        self._markets_meta = {
            m.token_id: {"question": m.question, "symbol": m.symbol}
            for m in self._markets
        }

        # Injeta meta no engine para o FillSimulator ter acesso ao symbol
        self.engine.set_markets_meta(self._markets_meta)

        self._reconnect_feed()

        logger.info("%d mercados viáveis", len(self._markets))
        self._log_activity(f"{len(self._markets)} mercados carregados", "info")

    def _reconnect_feed(self) -> None:
        if self._clob_feed:
            self._clob_feed.stop()
            time.sleep(0.3)

        token_ids = [m.token_id for m in self._markets]
        if not token_ids:
            return

        self._clob_feed = ClobFeed(
            token_ids=token_ids,
            on_update=self.engine.on_book_update,
        )
        self._clob_feed.start()

    # ── Quote Loop ────────────────────────────────────────────────────────────

    def _run_quote_cycle(self) -> None:
        import requests as _req

        market_ids = {m.token_id for m in self._markets}

        # Ciclo normal — top-10 mercados
        for market in list(self._markets):
            if self._shutdown_event.is_set():
                break
            if market.mins_left() < 5:
                continue
            if self.engine.should_requote(market.token_id):
                self.engine.cancel_market_orders(market.token_id)
                self.engine.quote_market(
                    token_id=market.token_id,
                    question=market.question,
                    symbol=market.symbol,
                    market_mid=market.yes_price,
                )

        # Liquidação — inventário fora do top-10 (apenas venda)
        for token_id, pos in list(self.risk._positions.items()):
            if self._shutdown_event.is_set():
                break
            if token_id in market_ids or pos.yes_shares < 0.5:
                continue
            if not self.engine.should_requote(token_id):
                continue
            try:
                r = _req.get(
                    f"https://clob.polymarket.com/book?token_id={token_id}",
                    timeout=5,
                )
                if r.status_code != 200:
                    logger.warning("Sem orderbook para inventário ...%s — mercado fechado?", token_id[-12:])
                    continue
                book = r.json()
                bids = book.get("bids", [])
                asks = book.get("asks", [])
                if bids and asks:
                    mid = (float(bids[0]["price"]) + float(asks[0]["price"])) / 2
                elif bids:
                    mid = float(bids[0]["price"]) + 0.01
                else:
                    logger.warning("Orderbook vazio para ...%s", token_id[-12:])
                    continue
            except Exception as e:
                logger.warning("Erro ao obter book de ...%s: %s", token_id[-12:], e)
                continue
            self.engine.cancel_market_orders(token_id)
            self.engine.quote_market(
                token_id=token_id,
                question=pos.question or f"...{token_id[-12:]}",
                symbol=pos.symbol or "SELL",
                market_mid=mid,
            )

    # ── Callbacks ─────────────────────────────────────────────────────────────

    def _on_fill(self, order_id, token_id, side, size, price, symbol, **kw):
        self._fills_total += 1
        msg = f"FILL #{self._fills_total} {symbol} {side.upper()} {size:.2f}sh @ {price:.3f}"
        logger.info(msg)
        self._log_activity(msg, "fill")
        # Após BUY fill, garante allowance para poder vender depois
        if side == "buy" and not DRY_RUN:
            threading.Thread(
                target=self._setup_allowances, daemon=True, name="AllowanceFix"
            ).start()

    def _on_quote(self, token_id, symbol, bid, ask, spread, mode, **kw):
        with self._lock_state:
            self._market_state[token_id] = {
                "bid": bid, "ask": ask, "spread": spread, "mode": mode,
            }
        logger.info("QUOTE %s bid=%.3f ask=%.3f spr=%.4f [%s]", symbol, bid, ask, spread, mode)
        self._log_activity(
            f"Quote {symbol}  bid={bid:.3f}  ask={ask:.3f}  [{mode}]", "quot"
        )

    # ── Allowances ────────────────────────────────────────────────────────────

    def _setup_allowances(self, clob=None) -> None:
        """
        Garante que o CLOB exchange tem allowance para:
          - USDC (COLLATERAL)  → executar BUY orders
          - Cada conditional token com posição → executar SELL orders
        Deve ser chamado no startup e após novos fills BUY.
        """
        if DRY_RUN:
            return
        clob = clob or self._clob_ref
        try:
            from py_clob_client.clob_types import BalanceAllowanceParams, AssetType

            # 1. USDC
            try:
                clob.update_balance_allowance(
                    params=BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
                )
                logger.info("Allowance USDC (COLLATERAL) atualizada OK")
            except Exception as e:
                logger.warning("Allowance USDC falhou: %s", e)

            # 2. Conditional tokens (YES/NO shares) para cada posição existente
            token_ids = list(self.risk._positions.keys())
            for token_id in token_ids:
                pos = self.risk._positions.get(token_id)
                if pos and pos.yes_shares < 0.01:
                    continue  # sem shares, não precisa
                try:
                    clob.update_balance_allowance(
                        params=BalanceAllowanceParams(
                            asset_type=AssetType.CONDITIONAL,
                            token_id=token_id,
                        )
                    )
                    logger.info("Allowance CONDITIONAL OK ...%s (%.2f shares)",
                                token_id[-12:], pos.yes_shares if pos else 0)
                except Exception as e:
                    logger.warning("Allowance CONDITIONAL falhou ...%s: %s", token_id[-12:], e)

        except Exception as e:
            logger.error("_setup_allowances erro geral: %s", e)

    def _fix_allowance_for_token(self, token_id: str) -> None:
        """Callback chamado pelo engine quando SELL falha por allowance — aprova o token específico."""
        if DRY_RUN:
            return
        try:
            from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
            self._clob_ref.update_balance_allowance(
                params=BalanceAllowanceParams(
                    asset_type=AssetType.CONDITIONAL,
                    token_id=token_id,
                )
            )
            logger.info("Allowance CONDITIONAL aprovada para ...%s — SELL liberado", token_id[-12:])
        except Exception as e:
            logger.error("Falha ao aprovar allowance para ...%s: %s", token_id[-12:], e)

    # ── Wallet Balance ────────────────────────────────────────────────────────

    def _refresh_wallet_balance(self) -> None:
        """Atualiza saldo USDC real da wallet via CLOB API (apenas em live mode)."""
        if DRY_RUN:
            return
        try:
            from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
            bal = self._clob_ref.get_balance_allowance(
                params=BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            )
            usdc = float(bal.get("balance", "0")) / 1e6
            self._wallet_usdc = usdc
        except Exception as e:
            logger.debug("Erro ao buscar saldo wallet: %s", e)

    # ── State Recovery ────────────────────────────────────────────────────────

    def _recover_state(self, clob) -> None:
        """Reconstrói posições usando a API de posições do Polymarket (IDs completos)."""
        import requests as _req
        logger.info("Recuperando estado de trades anteriores...")

        # Cancela TODAS as ordens abertas da sessão anterior antes de começar
        # Isso libera os conditional tokens que estavam bloqueados em escrow,
        # permitindo que novas SELL orders sejam colocadas com preços frescos.
        try:
            clob.cancel_all()
            logger.info("Ordens da sessão anterior canceladas — slate limpo")
        except Exception as e:
            logger.warning("Erro ao cancelar ordens anteriores: %s", e)
        try:
            from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
            from src.mm.risk_manager import Position as _Pos

            # Saldo real de USDC
            bal = clob.get_balance_allowance(
                params=BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            )
            usdc_real = float(bal.get("balance", "0")) / 1e6

            # Posições reais com token IDs completos (77 dígitos)
            resp = _req.get(
                f"https://data-api.polymarket.com/positions?user={POLYGON_ADDRESS}",
                timeout=10,
            )
            api_positions = resp.json() if resp.status_code == 200 else []

            count = 0
            for p in (api_positions or []):
                asset_id  = p.get("asset", "")
                size      = float(p.get("size", 0))
                avg_price = float(p.get("avgPrice", 0))
                outcome   = p.get("outcome", "")
                if not asset_id or size < 0.1:
                    continue
                self.risk._positions[asset_id] = _Pos(
                    token_id=asset_id, question="", symbol=outcome,
                    total_bought=size, total_sold=0.0,
                    total_buy_cost=size * avg_price, total_sell_revenue=0.0,
                )
                logger.info("  Posição recuperada ...%s: %.2f shares [%s] @ $%.3f",
                            asset_id[-12:], size, outcome, avg_price)
                count += 1

            # Capital: usa saldo real como base
            self.risk._total_buy_spent = max(0.0, MAX_TOTAL_USDC - usdc_real)
            logger.info("Estado recuperado: %d posições | USDC real=$%.4f | capital_spent=$%.2f",
                        count, usdc_real, self.risk._total_buy_spent)

        except Exception as e:
            logger.error("Erro ao recuperar estado: %s", e)

    # ── Position Analysis ─────────────────────────────────────────────────────

    def _analyze_recovered_positions(self) -> None:
        """
        Para cada posição recuperada, busca preço atual + tempo restante
        e loga recomendação: SELL NOW / HOLD / AGUARDAR RESOLUÇÃO.
        """
        import requests as _req

        positions = {k: v for k, v in self.risk._positions.items() if v.yes_shares >= 0.1}
        if not positions:
            logger.info("Nenhuma posicao aberta para analisar.")
            return

        logger.info("=" * 50)
        logger.info("ANALISE DAS POSICOES ABERTAS (%d)", len(positions))
        logger.info("=" * 50)

        for token_id, pos in positions.items():
            try:
                # Preço atual do orderbook
                r = _req.get(
                    f"https://clob.polymarket.com/book?token_id={token_id}",
                    timeout=5,
                )
                book = r.json() if r.status_code == 200 else {}
                bids = book.get("bids", [])
                asks = book.get("asks", [])
                if bids and asks:
                    mid = (float(bids[0]["price"]) + float(asks[0]["price"])) / 2
                    best_bid = float(bids[0]["price"])
                elif bids:
                    mid = best_bid = float(bids[0]["price"])
                else:
                    logger.warning("  ...%s: orderbook vazio — mercado fechado?", token_id[-12:])
                    continue

                avg_buy   = pos.avg_buy_price
                shares    = pos.yes_shares
                cost      = shares * avg_buy
                value_now = shares * best_bid           # valor se vender agora (ao bid)
                unreal_pnl = value_now - cost
                pnl_pct    = (unreal_pnl / cost * 100) if cost > 0 else 0.0

                # Tempo restante — tenta buscar da Gamma API
                mins_left: float | None = None
                try:
                    gr = _req.get(
                        f"https://gamma-api.polymarket.com/markets?clob_token_ids={token_id}",
                        timeout=5,
                    )
                    if gr.status_code == 200:
                        gdata = gr.json()
                        if isinstance(gdata, list) and gdata:
                            end_ts = gdata[0].get("endDate") or gdata[0].get("end_date_iso")
                            if end_ts:
                                import datetime as _dt
                                end = _dt.datetime.fromisoformat(end_ts.replace("Z", "+00:00"))
                                now = _dt.datetime.now(_dt.timezone.utc)
                                mins_left = max(0.0, (end - now).total_seconds() / 60)
                except Exception:
                    pass

                time_str = f"{mins_left:.0f}min" if mins_left is not None else "?"

                # Recomendacao
                if unreal_pnl >= 0:
                    if mins_left is not None and mins_left < 45:
                        rec = ">> VENDER AGORA  (mercado fechando, assegure o lucro)"
                    elif mins_left is not None and mins_left < 180:
                        rec = ">> VENDER EM BREVE  (janela se fechando)"
                    else:
                        rec = "OK HOLD  (lucrativo, tempo suficiente)"
                else:
                    if mins_left is not None and mins_left < 30:
                        rec = "!! AGUARDAR RESOLUCAO  (perda flutuante - pode resolver YES=1)"
                    else:
                        rec = "XX MANTER/DESCARREGAR  (perda - revisar se mercado mudou)"

                sign = "+" if unreal_pnl >= 0 else ""
                logger.info(
                    "  ...%s | %.2f sh [%s] | compra=%.3f mid=%.3f bid=%.3f | "
                    "P&L nao-realiz %s$%.3f (%s%.1f%%) | tempo=%s | %s",
                    token_id[-12:], shares, pos.symbol,
                    avg_buy, mid, best_bid,
                    sign, unreal_pnl, sign, pnl_pct,
                    time_str, rec,
                )

            except Exception as e:
                logger.warning("  Erro analisando ...%s: %s", token_id[-12:], e)

        logger.info("=" * 50)

    # ── Main Loop ─────────────────────────────────────────────────────────────

    def run(self) -> None:
        self.setup_signal_handlers()
        self.start_dashboard()

        mode_tag = "DRY_RUN" if DRY_RUN else "REAL"
        logger.info(
            "\n%s\n  MARKET MAKER — %s\n  Capital $%.0f | Ordem $%.0f/side | Dashboard :%d\n%s",
            "=" * 50, mode_tag, MAX_TOTAL_USDC, ORDER_USDC, DASHBOARD_PORT, "=" * 50,
        )
        self._log_activity(f"Bot iniciado — {mode_tag}", "info")

        while not self._shutdown_event.is_set():
            now = time.time()

            if now - self._last_market_refresh > MARKET_REFRESH_MIN * 60:
                self._refresh_markets()
                self._last_market_refresh = now

            if now - self._last_quote_cycle > QUOTE_INTERVAL:
                if self._markets:
                    self._run_quote_cycle()
                self._last_quote_cycle = now

            if now - self._last_fill_poll > FILL_POLL_INTERVAL:
                fills = self.engine.poll_fills(self._markets_meta)
                if fills:
                    logger.info("%d fill(s)", len(fills))
                self._last_fill_poll = now

            if now - self._last_balance_refresh > 30:
                self._refresh_wallet_balance()
                self._last_balance_refresh = now

            if now - self._last_status_log > 300:
                rs = self.risk.stats()
                logger.info(
                    "STATUS | ordens=%d | mercados=%d | capital=$%.2f | pnl=$%+.4f",
                    self.engine.stats()["active_orders"], len(self._markets),
                    rs["total_usdc_spent"], rs["total_realized_pnl"],
                )
                self._last_status_log = now

            self._shutdown_event.wait(timeout=1.0)

        logger.info("Encerrado.")


# ── Entry Point ────────────────────────────────────────────────────────────────

_LOCK_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".bot_mm.lock")
_LOCK_PORT = 54321  # porta interna de lock — não exposta externamente


def _acquire_instance_lock() -> None:
    """
    Garante que apenas uma instância rode por vez (cross-platform).
    Usa combinação de:
      1. Arquivo PID — para identificar o processo anterior
      2. Socket TCP local — como trava exclusiva (a porta fica ocupada enquanto o processo vive)
    Se outra instância estiver rodando, loga e encerra a nova.
    """
    import socket
    import atexit

    # Tenta ocupar a porta de lock
    _lock_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    _lock_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
    try:
        _lock_sock.bind(("127.0.0.1", _LOCK_PORT))
        _lock_sock.listen(1)
    except OSError:
        # Porta já ocupada — outra instância rodando
        try:
            with open(_LOCK_FILE) as f:
                old_pid = int(f.read().strip())
            logger.error(
                "Bot ja esta rodando (PID=%d). Encerre a instancia anterior antes de iniciar outra. Saindo.", old_pid
            )
        except Exception:
            logger.error("Bot ja esta rodando em outra instancia. Saindo.")
        sys.exit(1)

    # Registra PID
    try:
        with open(_LOCK_FILE, "w") as f:
            f.write(str(os.getpid()))
    except Exception:
        pass

    def _release():
        try:
            _lock_sock.close()
        except Exception:
            pass
        try:
            os.unlink(_LOCK_FILE)
        except Exception:
            pass

    atexit.register(_release)
    logger.info("Lock de instancia adquirido (PID=%d, porta=%d)", os.getpid(), _LOCK_PORT)


def main() -> None:
    if not DRY_RUN and not POLYGON_PRIVATE_KEY:
        logger.error("POLYGON_PRIVATE_KEY obrigatória para modo real")
        sys.exit(1)
    _acquire_instance_lock()
    MarketMakerBot().run()


if __name__ == "__main__":
    main()
