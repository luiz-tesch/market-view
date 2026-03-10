"""
Dashboard web local — Polymarket Bot
Run: python dashboard.py
Open: http://localhost:5000
"""
import json
import random
import time
from flask import Flask, jsonify, render_template_string, request
from src.config import validate, DRY_RUN
from src.market.client import get_client, get_markets, get_market_price, get_yes_token_id
from src.analysis.analyzer import analyze_market

app = Flask(__name__)
is_running = False
analysis_cache = {}
CACHE_TTL = 60 * 30  # 30 min

# Paper trading state (in-memory, resets on server restart)
portfolio = {
    "balance": 100.0,
    "bets": []  # {id, slug, question, side, amount, entry_price, placed_at, resolved, pnl}
}

def get_market_slug(market):
    return market.get("slug") or market.get("id") or market.get("question", "")

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Polymarket Bot</title>
<link href="https://fonts.googleapis.com/css2?family=Space+Mono:wght@400;700&family=Syne:wght@400;600;800&display=swap" rel="stylesheet">
<style>
  :root {
    --bg:#080c10; --surface:#0d1117; --surface2:#111820; --border:#1a2030;
    --accent:#00ff88; --accent2:#ff3366; --accent3:#3399ff;
    --text:#e2e8f0; --muted:#4a5568;
    --buy:#00ff88; --sell:#ff3366; --skip:#4a5568;
  }
  *{margin:0;padding:0;box-sizing:border-box;}
  body{background:var(--bg);color:var(--text);font-family:'Syne',sans-serif;min-height:100vh;overflow-x:hidden;}
  body::before{content:'';position:fixed;top:-50%;left:-50%;width:200%;height:200%;
    background:radial-gradient(ellipse at 20% 20%,rgba(0,255,136,.03) 0%,transparent 50%),
               radial-gradient(ellipse at 80% 80%,rgba(51,153,255,.03) 0%,transparent 50%);
    pointer-events:none;z-index:0;}
  .container{max-width:1440px;margin:0 auto;padding:2rem;position:relative;z-index:1;}

  /* NAV */
  nav{display:flex;align-items:center;justify-content:space-between;margin-bottom:2.5rem;padding-bottom:1.5rem;border-bottom:1px solid var(--border);}
  .logo{display:flex;align-items:baseline;gap:.75rem;}
  .logo h1{font-size:1.8rem;font-weight:800;letter-spacing:-.02em;color:#fff;}
  .logo-tag{font-family:'Space Mono',monospace;font-size:.7rem;color:var(--accent);background:rgba(0,255,136,.08);border:1px solid rgba(0,255,136,.2);padding:.2rem .6rem;border-radius:2px;letter-spacing:.1em;}
  .nav-right{display:flex;align-items:center;gap:1rem;}
  .tab-btn{font-family:'Space Mono',monospace;font-size:.7rem;letter-spacing:.1em;text-transform:uppercase;background:transparent;border:1px solid var(--border);color:var(--muted);padding:.5rem 1.2rem;cursor:pointer;transition:all .15s;}
  .tab-btn.active{border-color:var(--accent3);color:var(--accent3);background:rgba(51,153,255,.06);}
  .tab-btn:hover:not(.active){border-color:#2a3a50;color:var(--text);}
  .dry-badge{font-family:'Space Mono',monospace;font-size:.65rem;color:#f59e0b;background:rgba(245,158,11,.08);border:1px solid rgba(245,158,11,.25);padding:.3rem .8rem;border-radius:2px;letter-spacing:.1em;animation:pulse 2s ease-in-out infinite;}
  @keyframes pulse{0%,100%{opacity:1}50%{opacity:.5}}

  /* VIEWS */
  .view{display:none;} .view.active{display:block;}

  /* STATS */
  .stats-grid{display:grid;grid-template-columns:repeat(5,1fr);gap:1rem;margin-bottom:2rem;}
  .stat-card{background:var(--surface);border:1px solid var(--border);padding:1.25rem 1.5rem;position:relative;overflow:hidden;}
  .stat-card::before{content:'';position:absolute;top:0;left:0;width:3px;height:100%;}
  .stat-card.green::before{background:var(--accent);}  .stat-card.red::before{background:var(--accent2);}
  .stat-card.blue::before{background:var(--accent3);}  .stat-card.yellow::before{background:#f59e0b;}
  .stat-card.gray::before{background:var(--muted);}
  .stat-label{font-size:.65rem;letter-spacing:.15em;color:var(--muted);text-transform:uppercase;margin-bottom:.5rem;font-family:'Space Mono',monospace;}
  .stat-value{font-size:2rem;font-weight:800;letter-spacing:-.03em;line-height:1;}
  .stat-card.green .stat-value{color:var(--accent);} .stat-card.red .stat-value{color:var(--accent2);}
  .stat-card.blue .stat-value{color:var(--accent3);} .stat-card.yellow .stat-value{color:#f59e0b;}
  .stat-card.gray .stat-value{color:var(--muted);}

  /* LEGEND */
  .legend{background:var(--surface);border:1px solid var(--border);padding:1.25rem 1.5rem;margin-bottom:2rem;display:grid;grid-template-columns:repeat(3,1fr);gap:1.5rem;}
  .legend-title{font-size:.6rem;letter-spacing:.15em;color:var(--muted);text-transform:uppercase;font-family:'Space Mono',monospace;grid-column:1/-1;margin-bottom:.25rem;}
  .legend-item{display:flex;flex-direction:column;gap:.3rem;}
  .legend-field{font-family:'Space Mono',monospace;font-size:.7rem;color:var(--accent3);font-weight:700;letter-spacing:.05em;}
  .legend-desc{font-size:.75rem;color:var(--muted);line-height:1.4;}

  /* CONTROLS */
  .controls{display:flex;gap:1rem;margin-bottom:2rem;align-items:center;flex-wrap:wrap;}
  .btn-run{font-family:'Space Mono',monospace;font-size:.75rem;letter-spacing:.1em;text-transform:uppercase;background:var(--accent);color:#000;border:none;padding:.75rem 2rem;cursor:pointer;font-weight:700;transition:all .15s;}
  .btn-run:hover{background:#00cc6e;transform:translateY(-1px);} .btn-run:disabled{background:var(--muted);cursor:not-allowed;transform:none;}
  .btn-secondary{font-family:'Space Mono',monospace;font-size:.65rem;background:transparent;border:1px solid rgba(255,51,102,.3);color:rgba(255,51,102,.6);padding:.4rem .9rem;border-radius:2px;cursor:pointer;letter-spacing:.08em;transition:all .15s;}
  .btn-secondary:hover{border-color:var(--accent2);color:var(--accent2);}
  .limit-control{display:flex;align-items:center;gap:.5rem;font-family:'Space Mono',monospace;font-size:.7rem;color:var(--muted);}
  .limit-control select{background:var(--surface);border:1px solid var(--border);color:var(--text);font-family:'Space Mono',monospace;font-size:.7rem;padding:.4rem .6rem;cursor:pointer;outline:none;}
  .status-text{font-family:'Space Mono',monospace;font-size:.7rem;color:var(--muted);letter-spacing:.05em;}
  .status-text.running{color:var(--accent);}
  .spinner{display:inline-block;width:10px;height:10px;border:2px solid var(--accent);border-top-color:transparent;border-radius:50%;animation:spin .6s linear infinite;margin-right:.5rem;vertical-align:middle;}
  @keyframes spin{to{transform:rotate(360deg)}}

  /* MARKET CARDS */
  .markets-grid{display:grid;gap:.75rem;}
  .market-card{background:var(--surface);border:1px solid var(--border);padding:1.25rem 1.5rem;display:grid;grid-template-columns:1fr auto auto auto auto auto auto;gap:1.5rem;align-items:center;transition:border-color .2s,opacity .3s;animation:slideIn .3s ease both;position:relative;}
  @keyframes slideIn{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:translateY(0)}}
  .market-card:hover{border-color:#2a3a50;}
  .market-card.buy{border-left:3px solid var(--buy);} .market-card.sell{border-left:3px solid var(--sell);} .market-card.skip{border-left:3px solid var(--skip);}
  .market-card.cached{opacity:.65;} .market-card.dismissed{display:none;}
  .cached-badge{position:absolute;top:.6rem;right:1rem;font-family:'Space Mono',monospace;font-size:.55rem;letter-spacing:.1em;background:rgba(51,153,255,.08);border:1px solid rgba(51,153,255,.15);color:#4a6a8a;padding:.15rem .5rem;border-radius:2px;text-transform:uppercase;}
  .market-question{font-size:.9rem;font-weight:600;color:#fff;line-height:1.3;}
  .market-reasoning{font-size:.72rem;color:var(--muted);margin-top:.4rem;line-height:1.5;font-family:'Space Mono',monospace;}
  .market-factors{margin-top:.35rem;display:flex;gap:.4rem;flex-wrap:wrap;}
  .factor-tag{font-family:'Space Mono',monospace;font-size:.58rem;background:rgba(51,153,255,.08);border:1px solid rgba(51,153,255,.15);color:#6699cc;padding:.15rem .45rem;border-radius:2px;}
  .metric{text-align:center;min-width:70px;}
  .metric-label{font-size:.58rem;letter-spacing:.12em;color:var(--muted);text-transform:uppercase;font-family:'Space Mono',monospace;margin-bottom:.3rem;}
  .metric-value{font-size:1.1rem;font-weight:700;font-family:'Space Mono',monospace;}
  .edge-positive{color:var(--accent);} .edge-negative{color:var(--accent2);} .edge-neutral{color:var(--muted);}
  .confidence-badge{font-family:'Space Mono',monospace;font-size:.6rem;letter-spacing:.1em;padding:.2rem .5rem;border-radius:2px;text-transform:uppercase;display:block;}
  .conf-high{background:rgba(0,255,136,.1);color:var(--accent);border:1px solid rgba(0,255,136,.2);}
  .conf-medium{background:rgba(51,153,255,.1);color:var(--accent3);border:1px solid rgba(51,153,255,.2);}
  .conf-low{background:rgba(74,85,104,.2);color:var(--muted);border:1px solid rgba(74,85,104,.3);}
  .action-badge{font-family:'Space Mono',monospace;font-size:.7rem;font-weight:700;letter-spacing:.1em;padding:.4rem .9rem;border-radius:2px;text-transform:uppercase;min-width:65px;text-align:center;display:block;}
  .action-BUY{background:rgba(0,255,136,.12);color:var(--buy);border:1px solid rgba(0,255,136,.3);}
  .action-SELL{background:rgba(255,51,102,.12);color:var(--sell);border:1px solid rgba(255,51,102,.3);}
  .action-SKIP{background:rgba(74,85,104,.15);color:var(--muted);border:1px solid rgba(74,85,104,.2);}
  .btn-dismiss{background:transparent;border:1px solid rgba(255,51,102,.2);color:rgba(255,51,102,.4);font-family:'Space Mono',monospace;font-size:.6rem;padding:.3rem .6rem;cursor:pointer;border-radius:2px;transition:all .15s;letter-spacing:.08em;white-space:nowrap;display:block;margin-bottom:.4rem;}
  .btn-dismiss:hover{border-color:var(--accent2);color:var(--accent2);background:rgba(255,51,102,.06);}

  /* BET PANEL */
  .bet-panel{margin-top:.75rem;background:var(--surface2);border:1px solid var(--border);border-radius:3px;padding:.75rem 1rem;display:none;}
  .bet-panel.open{display:block;}
  .bet-panel-title{font-family:'Space Mono',monospace;font-size:.65rem;letter-spacing:.1em;color:var(--accent3);text-transform:uppercase;margin-bottom:.6rem;}
  .bet-row{display:flex;gap:.6rem;align-items:center;flex-wrap:wrap;}
  .side-btn{font-family:'Space Mono',monospace;font-size:.65rem;font-weight:700;letter-spacing:.08em;padding:.35rem .8rem;border:1px solid var(--border);background:transparent;color:var(--muted);cursor:pointer;border-radius:2px;transition:all .15s;}
  .side-btn.yes.active{background:rgba(0,255,136,.12);border-color:var(--accent);color:var(--accent);}
  .side-btn.no.active{background:rgba(255,51,102,.12);border-color:var(--accent2);color:var(--accent2);}
  .side-btn:hover{border-color:#2a3a50;color:var(--text);}
  .bet-amount{background:var(--surface);border:1px solid var(--border);color:var(--text);font-family:'Space Mono',monospace;font-size:.75rem;padding:.35rem .6rem;width:90px;outline:none;}
  .bet-amount:focus{border-color:var(--accent3);}
  .btn-place{font-family:'Space Mono',monospace;font-size:.65rem;font-weight:700;letter-spacing:.1em;text-transform:uppercase;background:var(--accent);color:#000;border:none;padding:.4rem 1.2rem;cursor:pointer;border-radius:2px;transition:all .15s;}
  .btn-place:hover{background:#00cc6e;}
  .bet-preview{font-family:'Space Mono',monospace;font-size:.65rem;color:var(--muted);margin-left:.5rem;}
  .btn-open-bet{font-family:'Space Mono',monospace;font-size:.65rem;letter-spacing:.08em;text-transform:uppercase;background:rgba(0,255,136,.08);border:1px solid rgba(0,255,136,.2);color:var(--accent);padding:.3rem .7rem;cursor:pointer;border-radius:2px;transition:all .15s;white-space:nowrap;display:block;}
  .btn-open-bet:hover{background:rgba(0,255,136,.14);}

  /* PORTFOLIO VIEW */
  .portfolio-header{display:flex;gap:1rem;margin-bottom:2rem;flex-wrap:wrap;}
  .balance-card{background:var(--surface);border:1px solid var(--border);padding:1.5rem 2rem;flex:1;min-width:160px;position:relative;overflow:hidden;}
  .balance-card::before{content:'';position:absolute;top:0;left:0;width:3px;height:100%;}
  .balance-card.main::before{background:var(--accent);}
  .balance-card.pnl-pos::before{background:var(--accent);}
  .balance-card.pnl-neg::before{background:var(--accent2);}
  .balance-card.neutral::before{background:var(--muted);}
  .balance-label{font-size:.65rem;letter-spacing:.15em;color:var(--muted);text-transform:uppercase;margin-bottom:.5rem;font-family:'Space Mono',monospace;}
  .balance-value{font-size:2.2rem;font-weight:800;letter-spacing:-.03em;line-height:1;}
  .balance-card.main .balance-value{color:var(--accent);}
  .pnl-positive{color:var(--accent);} .pnl-negative{color:var(--accent2);} .pnl-neutral{color:var(--muted);}

  .bets-table-wrap{background:var(--surface);border:1px solid var(--border);overflow:hidden;}
  .bets-table-header{display:grid;grid-template-columns:1fr 80px 80px 90px 90px 90px 90px 80px;gap:1rem;padding:.75rem 1.25rem;border-bottom:1px solid var(--border);background:rgba(26,32,48,.6);}
  .th{font-family:'Space Mono',monospace;font-size:.58rem;letter-spacing:.12em;color:var(--muted);text-transform:uppercase;}
  .bet-row-item{display:grid;grid-template-columns:1fr 80px 80px 90px 90px 90px 90px 80px;gap:1rem;padding:1rem 1.25rem;border-bottom:1px solid rgba(26,32,48,.8);align-items:center;transition:background .15s;}
  .bet-row-item:hover{background:rgba(255,255,255,.02);}
  .bet-row-item:last-child{border-bottom:none;}
  .bet-q{font-size:.82rem;font-weight:600;color:var(--text);line-height:1.3;}
  .bet-date{font-family:'Space Mono',monospace;font-size:.65rem;color:var(--muted);}
  .td{font-family:'Space Mono',monospace;font-size:.75rem;color:var(--text);text-align:center;}
  .side-yes{color:var(--accent);} .side-no{color:var(--accent2);}
  .pnl-cell{font-family:'Space Mono',monospace;font-size:.8rem;font-weight:700;text-align:center;}
  .btn-resolve{font-family:'Space Mono',monospace;font-size:.6rem;padding:.25rem .6rem;border-radius:2px;cursor:pointer;letter-spacing:.08em;transition:all .15s;border:1px solid rgba(0,255,136,.25);background:rgba(0,255,136,.06);color:var(--accent);}
  .btn-resolve:hover{background:rgba(0,255,136,.14);}
  .btn-resolve.no{border-color:rgba(255,51,102,.25);background:rgba(255,51,102,.06);color:var(--accent2);}
  .btn-resolve.no:hover{background:rgba(255,51,102,.14);}
  .resolved-yes{color:var(--accent);font-family:'Space Mono',monospace;font-size:.65rem;letter-spacing:.05em;}
  .resolved-no{color:var(--accent2);font-family:'Space Mono',monospace;font-size:.65rem;letter-spacing:.05em;}

  .empty-portfolio{text-align:center;padding:4rem 2rem;color:var(--muted);}
  .empty-portfolio p{font-family:'Space Mono',monospace;font-size:.8rem;letter-spacing:.05em;line-height:1.8;}

  /* MISC */
  .empty-state{text-align:center;padding:5rem 2rem;color:var(--muted);}
  .empty-state .icon{font-size:3rem;margin-bottom:1rem;opacity:.3;}
  .empty-state p{font-family:'Space Mono',monospace;font-size:.8rem;letter-spacing:.05em;}
  footer{margin-top:3rem;padding-top:1.5rem;border-top:1px solid var(--border);font-family:'Space Mono',monospace;font-size:.65rem;color:var(--muted);letter-spacing:.05em;display:flex;justify-content:space-between;}

  .toast{position:fixed;bottom:2rem;right:2rem;background:var(--surface);border:1px solid var(--accent);color:var(--accent);font-family:'Space Mono',monospace;font-size:.75rem;padding:.75rem 1.25rem;border-radius:3px;z-index:999;opacity:0;transform:translateY(10px);transition:all .25s;pointer-events:none;}
  .toast.show{opacity:1;transform:translateY(0);}
  .toast.error{border-color:var(--accent2);color:var(--accent2);}

  @media(max-width:900px){
    .stats-grid{grid-template-columns:repeat(2,1fr);}
    .market-card{grid-template-columns:1fr;gap:.75rem;}
    .legend{grid-template-columns:1fr;}
    .bets-table-header,.bet-row-item{grid-template-columns:1fr 70px 70px 80px 80px;}
  }
</style>
</head>
<body>
<div class="container">
  <nav>
    <div class="logo">
      <h1>Polymarket Bot</h1>
      <span class="logo-tag">GROQ · LLAMA 3.3</span>
    </div>
    <div class="nav-right">
      <button class="tab-btn active" onclick="showView('markets', this)">Markets</button>
      <button class="tab-btn" onclick="showView('portfolio', this)">Portfolio <span id="nav-bet-count"></span></button>
      <button class="btn-secondary" onclick="clearBanned()">↺ Clear Banned</button>
      <div class="dry-badge">● PAPER TRADING</div>
    </div>
  </nav>

  <!-- MARKETS VIEW -->
  <div class="view active" id="view-markets">
    <div class="stats-grid">
      <div class="stat-card green"><div class="stat-label">Markets Analyzed</div><div class="stat-value" id="stat-total">—</div></div>
      <div class="stat-card green"><div class="stat-label">BUY Signals</div><div class="stat-value" id="stat-buy">—</div></div>
      <div class="stat-card red"><div class="stat-label">SELL Signals</div><div class="stat-value" id="stat-sell">—</div></div>
      <div class="stat-card blue"><div class="stat-label">Avg. Abs. Edge</div><div class="stat-value" id="stat-edge">—</div></div>
      <div class="stat-card gray"><div class="stat-label">Banned</div><div class="stat-value" id="stat-banned">0</div></div>
    </div>

    <div class="legend">
      <div class="legend-title">Field Reference</div>
      <div class="legend-item"><div class="legend-field">Market Price</div><div class="legend-desc">Current implied probability on Polymarket. 53% = crowd estimates 53% chance of YES.</div></div>
      <div class="legend-item"><div class="legend-field">LLaMA Est.</div><div class="legend-desc">Probability estimated by LLaMA 3.3 using market context and recent news.</div></div>
      <div class="legend-item"><div class="legend-field">Edge</div><div class="legend-desc">LLaMA Est. minus Market Price. Positive = market underpriced → BUY opportunity.</div></div>
      <div class="legend-item"><div class="legend-field">Confidence</div><div class="legend-desc">How certain LLaMA is. Low confidence requires a larger edge before acting.</div></div>
      <div class="legend-item"><div class="legend-field">Action</div><div class="legend-desc">BUY/SELL if edge exceeds MIN_EDGE threshold. SKIP if risk isn't justified.</div></div>
      <div class="legend-item"><div class="legend-field">Place Bet</div><div class="legend-desc">Paper trade with fictional $100 balance. Choose YES/NO, set amount, and track P&L in Portfolio.</div></div>
    </div>

    <div class="controls">
      <button class="btn-run" id="btn-run" onclick="runAnalysis()">▶ Run Analysis</button>
      <div class="limit-control">
        <label for="limit-select">Markets:</label>
        <select id="limit-select">
          <option value="5">5</option>
          <option value="10" selected>10</option>
          <option value="20">20</option>
          <option value="30">30</option>
        </select>
      </div>
      <span class="status-text" id="status-text">Click to start analysis</span>
    </div>

    <div class="markets-grid" id="markets-grid">
      <div class="empty-state">
        <div class="icon">◎</div>
        <p>No analysis yet.<br>Click "Run Analysis" to get started.</p>
      </div>
    </div>
  </div>

  <!-- PORTFOLIO VIEW -->
  <div class="view" id="view-portfolio">
    <div class="portfolio-header" id="portfolio-header"></div>
    <div id="portfolio-body"></div>
  </div>

  <footer>
    <span>POLYMARKET BOT · PAPER TRADING</span>
    <span id="last-update">—</span>
  </footer>
</div>

<div class="toast" id="toast"></div>

<script>
// ── Helpers ────────────────────────────────────────────────────────────────
function showToast(msg, isError=false) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.className = 'toast show' + (isError ? ' error' : '');
  clearTimeout(t._t);
  t._t = setTimeout(() => t.className = 'toast', 2500);
}

function showView(name, btn) {
  document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  document.getElementById('view-' + name).classList.add('active');
  btn.classList.add('active');
  if (name === 'portfolio') renderPortfolio();
}

// ── Banned ─────────────────────────────────────────────────────────────────
function getBanned() { try { return JSON.parse(sessionStorage.getItem('banned_markets')||'[]'); } catch { return []; } }
function saveBanned(l) { sessionStorage.setItem('banned_markets', JSON.stringify(l)); document.getElementById('stat-banned').textContent = l.length; }
function banMarket(slug) {
  const l = getBanned(); if (!l.includes(slug)) { l.push(slug); saveBanned(l); }
  const c = document.getElementById('card-' + slug);
  if (c) { c.style.opacity='0'; c.style.transform='translateX(30px)'; c.style.transition='all .3s'; setTimeout(()=>c.classList.add('dismissed'),300); }
  showToast('Market banned');
}
function clearBanned() { saveBanned([]); document.querySelectorAll('.market-card.dismissed').forEach(c=>c.classList.remove('dismissed')); showToast('Banned list cleared'); }

// ── Portfolio (sessionStorage) ─────────────────────────────────────────────
function getPortfolio() {
  try { return JSON.parse(sessionStorage.getItem('portfolio') || 'null') || { balance: 100, bets: [] }; }
  catch { return { balance: 100, bets: [] }; }
}
function savePortfolio(p) {
  sessionStorage.setItem('portfolio', JSON.stringify(p));
  const open = p.bets.filter(b => !b.resolved).length;
  document.getElementById('nav-bet-count').textContent = open ? `(${open})` : '';
}

function placeBet(slug, question, entryPrice, side, amount) {
  const p = getPortfolio();
  amount = parseFloat(amount);
  if (isNaN(amount) || amount <= 0) { showToast('Invalid amount', true); return; }
  if (amount > p.balance) { showToast('Insufficient balance ($' + p.balance.toFixed(2) + ' left)', true); return; }
  p.balance -= amount;
  p.bets.push({
    id: Date.now(),
    slug, question, side, amount,
    entry_price: parseFloat(entryPrice),
    placed_at: new Date().toISOString(),
    resolved: false, outcome: null, pnl: null
  });
  savePortfolio(p);
  showToast(`✓ Bet placed: ${side} $${amount.toFixed(2)} @ ${(entryPrice*100).toFixed(1)}%`);
  closeBetPanel(slug);
}

function resolveBet(id, won) {
  const p = getPortfolio();
  const bet = p.bets.find(b => b.id === id);
  if (!bet || bet.resolved) return;
  bet.resolved = true;
  bet.outcome = won ? 'YES' : 'NO';
  // Payout: if side=YES and won → payout = amount / entry_price
  //         if side=NO  and won → payout = amount / (1 - entry_price)
  let payout = 0;
  if (won) {
    payout = bet.side === 'YES'
      ? bet.amount / bet.entry_price
      : bet.amount / (1 - bet.entry_price);
  }
  bet.pnl = payout - bet.amount;
  p.balance += payout;
  savePortfolio(p);
  renderPortfolio();
  showToast(won ? `✓ Won! +$${payout.toFixed(2)}` : `✗ Lost $${bet.amount.toFixed(2)}`, !won);
}

// ── Bet Panel UI ───────────────────────────────────────────────────────────
function toggleBetPanel(slug) {
  const panel = document.getElementById('bet-' + slug);
  if (!panel) return;
  panel.classList.toggle('open');
}
function closeBetPanel(slug) {
  const panel = document.getElementById('bet-' + slug);
  if (panel) panel.classList.remove('open');
}
function selectSide(slug, side) {
  document.querySelectorAll(`#bet-${slug} .side-btn`).forEach(b => b.classList.remove('active'));
  const btn = document.getElementById(`side-${side}-${slug}`);
  if (btn) btn.classList.add('active');
  updatePreview(slug);
}
function updatePreview(slug) {
  const panel = document.getElementById('bet-' + slug);
  if (!panel) return;
  const amtInput = panel.querySelector('.bet-amount');
  const preview = panel.querySelector('.bet-preview');
  const p = getPortfolio();
  const amt = parseFloat(amtInput.value) || 0;
  const sideBtn = panel.querySelector('.side-btn.active');
  if (!sideBtn || amt <= 0) { preview.textContent = ''; return; }
  const side = sideBtn.dataset.side;
  const price = parseFloat(panel.dataset.price);
  const payout = side === 'YES' ? amt / price : amt / (1 - price);
  const profit = payout - amt;
  preview.textContent = `→ win $${payout.toFixed(2)} (+$${profit.toFixed(2)}) · balance after: $${Math.max(0, p.balance - amt).toFixed(2)}`;
}

// ── Analysis ───────────────────────────────────────────────────────────────
async function runAnalysis() {
  const btn = document.getElementById('btn-run');
  const status = document.getElementById('status-text');
  const limit = document.getElementById('limit-select').value;
  const banned = getBanned();
  btn.disabled = true;
  status.className = 'status-text running';
  status.innerHTML = '<span class="spinner"></span>Querying Polymarket + LLaMA 3.3...';
  document.getElementById('markets-grid').innerHTML = `
    <div class="empty-state">
      <div class="icon"><span class="spinner" style="width:24px;height:24px;border-width:3px"></span></div>
      <p style="margin-top:1rem">Fetching ${limit} markets...<br><small style="opacity:.5">~${Math.round(limit*4)}s estimated</small></p>
    </div>`;
  try {
    const resp = await fetch('/api/analyze?limit='+limit+'&banned='+encodeURIComponent(JSON.stringify(banned)));
    const data = await resp.json();
    if (data.error) { status.innerHTML = '❌ '+data.error; status.className='status-text'; btn.disabled=false; return; }
    renderResults(data.results);
    updateStats(data.results);
    document.getElementById('last-update').textContent =
      'Last run: '+new Date().toLocaleTimeString('en-US')+' · '+data.results.length+' markets'+(data.cached_count?` · ${data.cached_count} cached`:'');
    status.innerHTML = `✓ Done — ${data.results.length} markets (${data.cached_count||0} cached, ${data.fresh_count||0} fresh)`;
    status.className='status-text';
  } catch(e) { status.innerHTML='❌ Error: '+e.message; status.className='status-text'; }
  btn.disabled=false;
}

function renderResults(results) {
  const grid = document.getElementById('markets-grid');
  if (!results.length) { grid.innerHTML='<div class="empty-state"><p>No markets returned.</p></div>'; return; }
  results.sort((a,b)=>Math.abs(b.edge)-Math.abs(a.edge));
  const banned = getBanned();
  grid.innerHTML = results.map((r,i)=>{
    const edgeClass = r.edge>.01?'edge-positive':r.edge<-.01?'edge-negative':'edge-neutral';
    const edgeStr = (r.edge>=0?'+':'')+(r.edge*100).toFixed(1)+'%';
    const cardClass = r.recommendation.toLowerCase();
    const confClass = 'conf-'+r.confidence;
    const delay = i*.04;
    const factors = (r.key_factors||[]).map(f=>`<span class="factor-tag">${f}</span>`).join('');
    const safeSlug = r.slug.replace(/[^a-zA-Z0-9_-]/g,'_');
    const isBanned = banned.includes(r.slug);
    const cachedBadge = r.cached ? '<span class="cached-badge">cached</span>' : '';
    return `
    <div class="market-card ${cardClass}${r.cached?' cached':''}${isBanned?' dismissed':''}" id="card-${safeSlug}" style="animation-delay:${delay}s">
      ${cachedBadge}
      <div>
        <div class="market-question">${r.question}</div>
        <div class="market-reasoning">${r.reasoning}</div>
        <div class="market-factors">${factors}</div>
        <div class="bet-panel" id="bet-${safeSlug}" data-price="${r.current_price}" data-slug="${r.slug}">
          <div class="bet-panel-title">Place Paper Bet</div>
          <div class="bet-row">
            <button class="side-btn yes" id="side-YES-${safeSlug}" data-side="YES" onclick="selectSide('${safeSlug}','YES')">YES</button>
            <button class="side-btn no"  id="side-NO-${safeSlug}"  data-side="NO"  onclick="selectSide('${safeSlug}','NO')">NO</button>
            <input class="bet-amount" type="number" min="0.5" max="100" step="0.5" value="5" oninput="updatePreview('${safeSlug}')">
            <span style="font-family:'Space Mono',monospace;font-size:.7rem;color:var(--muted);">USDC</span>
            <button class="btn-place" onclick="
              const side = document.querySelector('#bet-${safeSlug} .side-btn.active')?.dataset.side;
              const amt  = document.querySelector('#bet-${safeSlug} .bet-amount').value;
              if(!side){showToast('Select YES or NO first',true);return;}
              placeBet('${r.slug}','${r.question.replace(/'/g,"\\'")}',${r.current_price},side,amt);
            ">Confirm</button>
            <span class="bet-preview"></span>
          </div>
        </div>
      </div>
      <div class="metric"><div class="metric-label">Market Price</div><div class="metric-value">${(r.current_price*100).toFixed(1)}%</div></div>
      <div class="metric"><div class="metric-label">LLaMA Est.</div><div class="metric-value">${(r.estimated_prob*100).toFixed(1)}%</div></div>
      <div class="metric"><div class="metric-label">Edge</div><div class="metric-value ${edgeClass}">${edgeStr}</div></div>
      <div class="metric"><div class="metric-label">Confidence</div><span class="confidence-badge ${confClass}">${r.confidence}</span></div>
      <div class="metric"><div class="metric-label">Action</div><span class="action-badge action-${r.recommendation}">${r.recommendation}</span></div>
      <div class="metric">
        <div class="metric-label">Trade</div>
        <button class="btn-open-bet" onclick="toggleBetPanel('${safeSlug}')">+ Bet</button>
        <button class="btn-dismiss" style="margin-top:.4rem" onclick="banMarket('${r.slug}')">✕ Ban</button>
      </div>
    </div>`;
  }).join('');
}

function updateStats(results) {
  document.getElementById('stat-total').textContent = results.length;
  document.getElementById('stat-buy').textContent   = results.filter(r=>r.recommendation==='BUY').length;
  document.getElementById('stat-sell').textContent  = results.filter(r=>r.recommendation==='SELL').length;
  const avgEdge = results.reduce((s,r)=>s+Math.abs(r.edge),0)/results.length;
  document.getElementById('stat-edge').textContent  = (avgEdge*100).toFixed(1)+'%';
  document.getElementById('stat-banned').textContent = getBanned().length;
}

// ── Portfolio Render ───────────────────────────────────────────────────────
function renderPortfolio() {
  const p = getPortfolio();
  const open = p.bets.filter(b=>!b.resolved);
  const closed = p.bets.filter(b=>b.resolved);
  const totalPnl = closed.reduce((s,b)=>s+(b.pnl||0),0);
  const totalWagered = p.bets.reduce((s,b)=>s+b.amount,0);
  const winRate = closed.length ? (closed.filter(b=>b.pnl>0).length/closed.length*100).toFixed(0)+'%' : '—';

  const pnlClass = totalPnl>0?'pnl-pos':totalPnl<0?'pnl-neg':'neutral';
  document.getElementById('portfolio-header').innerHTML = `
    <div class="balance-card main"><div class="balance-label">Available Balance</div><div class="balance-value">$${p.balance.toFixed(2)}</div></div>
    <div class="balance-card ${pnlClass}"><div class="balance-label">Total P&amp;L</div><div class="balance-value ${totalPnl>=0?'pnl-positive':'pnl-negative'}">${totalPnl>=0?'+':''}$${totalPnl.toFixed(2)}</div></div>
    <div class="balance-card neutral"><div class="balance-label">Open Bets</div><div class="balance-value" style="color:var(--accent3)">${open.length}</div></div>
    <div class="balance-card neutral"><div class="balance-label">Win Rate</div><div class="balance-value" style="color:#f59e0b">${winRate}</div></div>
    <div class="balance-card neutral"><div class="balance-label">Total Wagered</div><div class="balance-value" style="color:var(--muted)">$${totalWagered.toFixed(2)}</div></div>
  `;

  if (!p.bets.length) {
    document.getElementById('portfolio-body').innerHTML =
      '<div class="empty-portfolio"><p>No bets yet.<br>Go to Markets, click "+ Bet" on any card and place a paper trade.</p></div>';
    return;
  }

  const renderRow = (b) => {
    const pnlHtml = b.resolved
      ? `<div class="pnl-cell ${b.pnl>=0?'pnl-positive':'pnl-negative'}">${b.pnl>=0?'+':''}$${b.pnl.toFixed(2)}</div>`
      : `<div class="pnl-cell" style="color:var(--muted)">open</div>`;
    const resolveHtml = b.resolved
      ? `<div class="${b.outcome==='YES'?'resolved-yes':'resolved-no'}">${b.outcome==='YES'?'✓ YES':'✗ NO'}</div>`
      : `<button class="btn-resolve" onclick="resolveBet(${b.id},true)">YES</button>
         <button class="btn-resolve no" style="margin-top:.3rem" onclick="resolveBet(${b.id},false)">NO</button>`;
    return `
    <div class="bet-row-item">
      <div><div class="bet-q">${b.question}</div><div class="bet-date">${new Date(b.placed_at).toLocaleDateString('en-US',{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'})}</div></div>
      <div class="td side-${b.side.toLowerCase()}">${b.side}</div>
      <div class="td">$${b.amount.toFixed(2)}</div>
      <div class="td">${(b.entry_price*100).toFixed(1)}%</div>
      <div class="td">${b.side==='YES'?(1/b.entry_price).toFixed(2)+'x':(1/(1-b.entry_price)).toFixed(2)+'x'}</div>
      ${pnlHtml}
      <div class="td">${b.resolved?'closed':'open'}</div>
      <div class="td" style="display:flex;flex-direction:column;gap:.3rem;align-items:center;">${resolveHtml}</div>
    </div>`;
  };

  document.getElementById('portfolio-body').innerHTML = `
    ${open.length ? `
    <div style="font-family:'Space Mono',monospace;font-size:.65rem;letter-spacing:.12em;color:var(--accent3);text-transform:uppercase;margin-bottom:.75rem;">Open Positions (${open.length})</div>
    <div class="bets-table-wrap" style="margin-bottom:1.5rem;">
      <div class="bets-table-header"><div class="th">Market</div><div class="th">Side</div><div class="th">Amount</div><div class="th">Entry</div><div class="th">Odds</div><div class="th">P&L</div><div class="th">Status</div><div class="th">Resolve</div></div>
      ${open.map(renderRow).join('')}
    </div>` : ''}
    ${closed.length ? `
    <div style="font-family:'Space Mono',monospace;font-size:.65rem;letter-spacing:.12em;color:var(--muted);text-transform:uppercase;margin-bottom:.75rem;">Closed Positions (${closed.length})</div>
    <div class="bets-table-wrap">
      <div class="bets-table-header"><div class="th">Market</div><div class="th">Side</div><div class="th">Amount</div><div class="th">Entry</div><div class="th">Odds</div><div class="th">P&L</div><div class="th">Status</div><div class="th">Result</div></div>
      ${closed.map(renderRow).join('')}
    </div>` : ''}
  `;
}

// Init
document.getElementById('stat-banned').textContent = getBanned().length;
const openCount = getPortfolio().bets.filter(b=>!b.resolved).length;
if (openCount) document.getElementById('nav-bet-count').textContent = `(${openCount})`;
</script>
</body>
</html>"""


@app.route("/")
def index():
    return render_template_string(HTML)


@app.route("/api/analyze")
def api_analyze():
    global is_running, analysis_cache
    if is_running:
        return jsonify({"error": "Analysis already running, please wait..."}), 429
    is_running = True
    try:
        validate()
        client = get_client()
        limit = int(request.args.get("limit", 10))
        banned = json.loads(request.args.get("banned", "[]"))

        pool = get_markets(client, limit=100)
        pool = [m for m in pool if get_market_slug(m) not in banned]
        sample = random.sample(pool, min(limit, len(pool)))

        results = []
        cached_count = fresh_count = 0
        now = time.time()

        for market in sample:
            question = market.get("question", "?")
            slug = get_market_slug(market)
            token_id = get_yes_token_id(market)
            if not token_id:
                continue
            try:
                price = get_market_price(client, market)
                market["end_date_iso"] = market.get("endDateIso", "?")
                cached = analysis_cache.get(slug)
                if cached and (now - cached["timestamp"]) < CACHE_TTL:
                    result = dict(cached["result"])
                    result["cached"] = True
                    result["current_price"] = price
                    cached_count += 1
                else:
                    result = analyze_market(market, price)
                    result["cached"] = False
                    analysis_cache[slug] = {"result": dict(result), "timestamp": now}
                    fresh_count += 1
                result["slug"] = slug
                results.append(result)
            except Exception as e:
                results.append({"question": question, "slug": slug, "current_price": 0,
                                 "estimated_prob": 0, "edge": 0, "confidence": "low",
                                 "recommendation": "SKIP", "reasoning": f"Error: {str(e)}",
                                 "key_factors": [], "cached": False})

        return jsonify({"results": results, "dry_run": DRY_RUN,
                        "cached_count": cached_count, "fresh_count": fresh_count})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        is_running = False


if __name__ == "__main__":
    print("🚀 Dashboard running at http://localhost:5000")
    app.run(debug=False, port=5000)