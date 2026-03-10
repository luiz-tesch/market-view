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

# Server-side cache: slug -> {result, timestamp}
analysis_cache = {}
CACHE_TTL_SECONDS = 60 * 30  # 30 minutes


def get_market_slug(market: dict) -> str:
    return market.get("slug") or market.get("id") or market.get("question", "")


HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Polymarket Bot</title>
<link href="https://fonts.googleapis.com/css2?family=Space+Mono:wght@400;700&family=Syne:wght@400;600;800&display=swap" rel="stylesheet">
<style>
  :root {
    --bg: #080c10; --surface: #0d1117; --border: #1a2030;
    --accent: #00ff88; --accent2: #ff3366; --accent3: #3399ff;
    --text: #e2e8f0; --muted: #4a5568;
    --buy: #00ff88; --sell: #ff3366; --skip: #4a5568;
  }
  * { margin:0; padding:0; box-sizing:border-box; }
  body { background:var(--bg); color:var(--text); font-family:'Syne',sans-serif; min-height:100vh; overflow-x:hidden; }
  body::before {
    content:''; position:fixed; top:-50%; left:-50%; width:200%; height:200%;
    background: radial-gradient(ellipse at 20% 20%,rgba(0,255,136,.03) 0%,transparent 50%),
                radial-gradient(ellipse at 80% 80%,rgba(51,153,255,.03) 0%,transparent 50%);
    pointer-events:none; z-index:0;
  }
  .container { max-width:1400px; margin:0 auto; padding:2rem; position:relative; z-index:1; }

  header { display:flex; align-items:center; justify-content:space-between; margin-bottom:2.5rem; padding-bottom:1.5rem; border-bottom:1px solid var(--border); }
  .logo { display:flex; align-items:baseline; gap:.75rem; }
  .logo h1 { font-size:1.8rem; font-weight:800; letter-spacing:-.02em; color:#fff; }
  .logo span { font-family:'Space Mono',monospace; font-size:.7rem; color:var(--accent); background:rgba(0,255,136,.08); border:1px solid rgba(0,255,136,.2); padding:.2rem .6rem; border-radius:2px; letter-spacing:.1em; }
  .header-right { display:flex; align-items:center; gap:1rem; }
  .dry-badge { font-family:'Space Mono',monospace; font-size:.65rem; color:#f59e0b; background:rgba(245,158,11,.08); border:1px solid rgba(245,158,11,.25); padding:.3rem .8rem; border-radius:2px; letter-spacing:.1em; animation:pulse 2s ease-in-out infinite; }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.5} }

  .btn-clear-banned { font-family:'Space Mono',monospace; font-size:.65rem; background:transparent; border:1px solid rgba(255,51,102,.3); color:rgba(255,51,102,.6); padding:.3rem .8rem; border-radius:2px; cursor:pointer; letter-spacing:.08em; transition:all .15s; }
  .btn-clear-banned:hover { border-color:var(--accent2); color:var(--accent2); }

  .stats-grid { display:grid; grid-template-columns:repeat(5,1fr); gap:1rem; margin-bottom:2rem; }
  .stat-card { background:var(--surface); border:1px solid var(--border); padding:1.25rem 1.5rem; position:relative; overflow:hidden; }
  .stat-card::before { content:''; position:absolute; top:0; left:0; width:3px; height:100%; }
  .stat-card.green::before  { background:var(--accent); }
  .stat-card.red::before    { background:var(--accent2); }
  .stat-card.blue::before   { background:var(--accent3); }
  .stat-card.yellow::before { background:#f59e0b; }
  .stat-card.gray::before   { background:var(--muted); }
  .stat-label { font-size:.65rem; letter-spacing:.15em; color:var(--muted); text-transform:uppercase; margin-bottom:.5rem; font-family:'Space Mono',monospace; }
  .stat-value { font-size:2rem; font-weight:800; letter-spacing:-.03em; line-height:1; }
  .stat-card.green .stat-value  { color:var(--accent); }
  .stat-card.red .stat-value    { color:var(--accent2); }
  .stat-card.blue .stat-value   { color:var(--accent3); }
  .stat-card.yellow .stat-value { color:#f59e0b; }
  .stat-card.gray .stat-value   { color:var(--muted); }

  .legend { background:var(--surface); border:1px solid var(--border); padding:1.25rem 1.5rem; margin-bottom:2rem; display:grid; grid-template-columns:repeat(3,1fr); gap:1.5rem; }
  .legend-title { font-size:.6rem; letter-spacing:.15em; color:var(--muted); text-transform:uppercase; font-family:'Space Mono',monospace; grid-column:1/-1; margin-bottom:.25rem; }
  .legend-item { display:flex; flex-direction:column; gap:.3rem; }
  .legend-field { font-family:'Space Mono',monospace; font-size:.7rem; color:var(--accent3); font-weight:700; letter-spacing:.05em; }
  .legend-desc { font-size:.75rem; color:var(--muted); line-height:1.4; }

  .controls { display:flex; gap:1rem; margin-bottom:2rem; align-items:center; flex-wrap:wrap; }
  .btn-run { font-family:'Space Mono',monospace; font-size:.75rem; letter-spacing:.1em; text-transform:uppercase; background:var(--accent); color:#000; border:none; padding:.75rem 2rem; cursor:pointer; font-weight:700; transition:all .15s; }
  .btn-run:hover { background:#00cc6e; transform:translateY(-1px); }
  .btn-run:disabled { background:var(--muted); cursor:not-allowed; transform:none; }
  .limit-control { display:flex; align-items:center; gap:.5rem; font-family:'Space Mono',monospace; font-size:.7rem; color:var(--muted); }
  .limit-control select { background:var(--surface); border:1px solid var(--border); color:var(--text); font-family:'Space Mono',monospace; font-size:.7rem; padding:.4rem .6rem; cursor:pointer; outline:none; }
  .status-text { font-family:'Space Mono',monospace; font-size:.7rem; color:var(--muted); letter-spacing:.05em; }
  .status-text.running { color:var(--accent); }
  .spinner { display:inline-block; width:10px; height:10px; border:2px solid var(--accent); border-top-color:transparent; border-radius:50%; animation:spin .6s linear infinite; margin-right:.5rem; vertical-align:middle; }
  @keyframes spin { to{transform:rotate(360deg)} }

  .markets-grid { display:grid; gap:.75rem; }

  .market-card {
    background:var(--surface); border:1px solid var(--border);
    padding:1.25rem 1.5rem;
    display:grid; grid-template-columns:1fr auto auto auto auto auto auto;
    gap:1.5rem; align-items:center; transition:border-color .2s, opacity .3s;
    animation:slideIn .3s ease both; position:relative;
  }
  @keyframes slideIn { from{opacity:0;transform:translateY(8px)} to{opacity:1;transform:translateY(0)} }
  .market-card:hover { border-color:#2a3a50; }
  .market-card.buy  { border-left:3px solid var(--buy); }
  .market-card.sell { border-left:3px solid var(--sell); }
  .market-card.skip { border-left:3px solid var(--skip); }
  .market-card.cached { opacity:.65; }
  .market-card.dismissed { display:none; }

  .cached-badge {
    position:absolute; top:.6rem; right:4.5rem;
    font-family:'Space Mono',monospace; font-size:.55rem; letter-spacing:.1em;
    background:rgba(51,153,255,.08); border:1px solid rgba(51,153,255,.15);
    color:#4a6a8a; padding:.15rem .5rem; border-radius:2px; text-transform:uppercase;
  }

  .market-question { font-size:.9rem; font-weight:600; color:#fff; line-height:1.3; }
  .market-reasoning { font-size:.72rem; color:var(--muted); margin-top:.4rem; line-height:1.5; font-family:'Space Mono',monospace; }
  .market-factors { margin-top:.35rem; display:flex; gap:.4rem; flex-wrap:wrap; }
  .factor-tag { font-family:'Space Mono',monospace; font-size:.58rem; background:rgba(51,153,255,.08); border:1px solid rgba(51,153,255,.15); color:#6699cc; padding:.15rem .45rem; border-radius:2px; }

  .metric { text-align:center; min-width:70px; }
  .metric-label { font-size:.58rem; letter-spacing:.12em; color:var(--muted); text-transform:uppercase; font-family:'Space Mono',monospace; margin-bottom:.3rem; }
  .metric-value { font-size:1.1rem; font-weight:700; font-family:'Space Mono',monospace; }
  .edge-positive { color:var(--accent); }
  .edge-negative { color:var(--accent2); }
  .edge-neutral  { color:var(--muted); }

  .confidence-badge { font-family:'Space Mono',monospace; font-size:.6rem; letter-spacing:.1em; padding:.2rem .5rem; border-radius:2px; text-transform:uppercase; display:block; }
  .conf-high   { background:rgba(0,255,136,.1);  color:var(--accent);  border:1px solid rgba(0,255,136,.2); }
  .conf-medium { background:rgba(51,153,255,.1); color:var(--accent3); border:1px solid rgba(51,153,255,.2); }
  .conf-low    { background:rgba(74,85,104,.2);  color:var(--muted);   border:1px solid rgba(74,85,104,.3); }

  .action-badge { font-family:'Space Mono',monospace; font-size:.7rem; font-weight:700; letter-spacing:.1em; padding:.4rem .9rem; border-radius:2px; text-transform:uppercase; min-width:65px; text-align:center; display:block; }
  .action-BUY  { background:rgba(0,255,136,.12); color:var(--buy);  border:1px solid rgba(0,255,136,.3); }
  .action-SELL { background:rgba(255,51,102,.12);color:var(--sell); border:1px solid rgba(255,51,102,.3); }
  .action-SKIP { background:rgba(74,85,104,.15); color:var(--muted);border:1px solid rgba(74,85,104,.2); }

  .btn-dismiss {
    background:transparent; border:1px solid rgba(255,51,102,.2); color:rgba(255,51,102,.4);
    font-family:'Space Mono',monospace; font-size:.6rem; padding:.3rem .6rem;
    cursor:pointer; border-radius:2px; transition:all .15s; letter-spacing:.08em; white-space:nowrap;
  }
  .btn-dismiss:hover { border-color:var(--accent2); color:var(--accent2); background:rgba(255,51,102,.06); }

  .empty-state { text-align:center; padding:5rem 2rem; color:var(--muted); }
  .empty-state .icon { font-size:3rem; margin-bottom:1rem; opacity:.3; }
  .empty-state p { font-family:'Space Mono',monospace; font-size:.8rem; letter-spacing:.05em; }

  footer { margin-top:3rem; padding-top:1.5rem; border-top:1px solid var(--border); font-family:'Space Mono',monospace; font-size:.65rem; color:var(--muted); letter-spacing:.05em; display:flex; justify-content:space-between; }

  @media(max-width:900px) {
    .stats-grid{grid-template-columns:repeat(2,1fr)}
    .market-card{grid-template-columns:1fr;gap:.75rem}
    .legend{grid-template-columns:1fr}
  }
</style>
</head>
<body>
<div class="container">
  <header>
    <div class="logo">
      <h1>Polymarket Bot</h1>
      <span>GROQ · LLAMA 3.3</span>
    </div>
    <div class="header-right">
      <button class="btn-clear-banned" onclick="clearBanned()">↺ Clear Banned</button>
      <div class="dry-badge">● DRY RUN</div>
    </div>
  </header>

  <div class="stats-grid">
    <div class="stat-card green">
      <div class="stat-label">Markets Analyzed</div>
      <div class="stat-value" id="stat-total">—</div>
    </div>
    <div class="stat-card green">
      <div class="stat-label">BUY Signals</div>
      <div class="stat-value" id="stat-buy">—</div>
    </div>
    <div class="stat-card red">
      <div class="stat-label">SELL Signals</div>
      <div class="stat-value" id="stat-sell">—</div>
    </div>
    <div class="stat-card blue">
      <div class="stat-label">Avg. Abs. Edge</div>
      <div class="stat-value" id="stat-edge">—</div>
    </div>
    <div class="stat-card gray">
      <div class="stat-label">Banned Markets</div>
      <div class="stat-value" id="stat-banned">0</div>
    </div>
  </div>

  <div class="legend">
    <div class="legend-title">Field Reference</div>
    <div class="legend-item">
      <div class="legend-field">Market Price</div>
      <div class="legend-desc">Current implied probability on Polymarket. E.g. 53% means the crowd bets 53% chance of YES.</div>
    </div>
    <div class="legend-item">
      <div class="legend-field">LLaMA Est.</div>
      <div class="legend-desc">Probability estimated by LLaMA 3.3 using market context and recent news.</div>
    </div>
    <div class="legend-item">
      <div class="legend-field">Edge</div>
      <div class="legend-desc">LLaMA Est. minus Market Price. Positive = market underpriced → BUY. Negative = overpriced → SELL.</div>
    </div>
    <div class="legend-item">
      <div class="legend-field">Confidence</div>
      <div class="legend-desc">How certain LLaMA is of its estimate. Low confidence requires a larger edge before acting.</div>
    </div>
    <div class="legend-item">
      <div class="legend-field">Action</div>
      <div class="legend-desc">BUY/SELL if edge exceeds MIN_EDGE. SKIP if the opportunity is too small to justify the risk.</div>
    </div>
    <div class="legend-item">
      <div class="legend-field">Cached / Ban</div>
      <div class="legend-desc">"CACHED" means the result was reused from the last 30 min — no new API call made. Ban permanently hides a market from future runs.</div>
    </div>
  </div>

  <div class="controls">
    <button class="btn-run" id="btn-run" onclick="runAnalysis()">▶ Run Analysis</button>
    <div class="limit-control">
      <label for="limit-select">Markets to sample:</label>
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

  <footer>
    <span>POLYMARKET BOT · DRY RUN MODE</span>
    <span id="last-update">—</span>
  </footer>
</div>

<script>
// ── Banned list (persisted in sessionStorage) ──────────────────────────────
function getBanned() {
  try { return JSON.parse(sessionStorage.getItem('banned_markets') || '[]'); }
  catch { return []; }
}
function saveBanned(list) {
  sessionStorage.setItem('banned_markets', JSON.stringify(list));
  document.getElementById('stat-banned').textContent = list.length;
}
function banMarket(slug) {
  const list = getBanned();
  if (!list.includes(slug)) { list.push(slug); saveBanned(list); }
  const card = document.getElementById('card-' + slug);
  if (card) { card.style.opacity = '0'; card.style.transform = 'translateX(30px)'; card.style.transition = 'all .3s'; setTimeout(() => card.classList.add('dismissed'), 300); }
}
function clearBanned() {
  saveBanned([]);
  document.querySelectorAll('.market-card.dismissed').forEach(c => c.classList.remove('dismissed'));
  document.getElementById('stat-banned').textContent = '0';
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
      <p style="margin-top:1rem">Fetching ${limit} markets — banned: ${banned.length}<br><small style="opacity:.5">~${Math.round(limit*4)}s estimated</small></p>
    </div>`;

  try {
    const resp = await fetch('/api/analyze?limit=' + limit + '&banned=' + encodeURIComponent(JSON.stringify(banned)));
    const data = await resp.json();
    if (data.error) { status.innerHTML = '❌ ' + data.error; status.className = 'status-text'; btn.disabled = false; return; }

    renderResults(data.results);
    updateStats(data.results);
    document.getElementById('stat-banned').textContent = getBanned().length;
    document.getElementById('last-update').textContent =
      'Last run: ' + new Date().toLocaleTimeString('en-US') +
      ' · ' + data.results.length + ' markets' +
      (data.cached_count ? ` · ${data.cached_count} cached` : '');
    status.innerHTML = `✓ Done — ${data.results.length} markets (${data.cached_count || 0} from cache, ${data.fresh_count || 0} fresh)`;
    status.className = 'status-text';
  } catch(e) {
    status.innerHTML = '❌ Error: ' + e.message;
    status.className = 'status-text';
  }
  btn.disabled = false;
}

function renderResults(results) {
  const grid = document.getElementById('markets-grid');
  if (!results.length) { grid.innerHTML = '<div class="empty-state"><p>No markets returned.</p></div>'; return; }

  results.sort((a, b) => Math.abs(b.edge) - Math.abs(a.edge));
  const banned = getBanned();

  grid.innerHTML = results.map((r, i) => {
    const edgeClass = r.edge > 0.01 ? 'edge-positive' : r.edge < -0.01 ? 'edge-negative' : 'edge-neutral';
    const edgeStr = (r.edge >= 0 ? '+' : '') + (r.edge * 100).toFixed(1) + '%';
    const cardClass = r.recommendation.toLowerCase();
    const confClass = 'conf-' + r.confidence;
    const delay = i * 0.04;
    const factors = (r.key_factors || []).map(f => `<span class="factor-tag">${f}</span>`).join('');
    const safeSlug = r.slug.replace(/[^a-zA-Z0-9_-]/g, '_');
    const isBanned = banned.includes(r.slug);
    const cachedBadge = r.cached ? '<span class="cached-badge">cached</span>' : '';

    return `
    <div class="market-card ${cardClass}${r.cached ? ' cached' : ''}${isBanned ? ' dismissed' : ''}"
         id="card-${safeSlug}" style="animation-delay:${delay}s">
      ${cachedBadge}
      <div>
        <div class="market-question">${r.question}</div>
        <div class="market-reasoning">${r.reasoning}</div>
        <div class="market-factors">${factors}</div>
      </div>
      <div class="metric"><div class="metric-label">Market Price</div><div class="metric-value">${(r.current_price*100).toFixed(1)}%</div></div>
      <div class="metric"><div class="metric-label">LLaMA Est.</div><div class="metric-value">${(r.estimated_prob*100).toFixed(1)}%</div></div>
      <div class="metric"><div class="metric-label">Edge</div><div class="metric-value ${edgeClass}">${edgeStr}</div></div>
      <div class="metric"><div class="metric-label">Confidence</div><span class="confidence-badge ${confClass}">${r.confidence}</span></div>
      <div class="metric"><div class="metric-label">Action</div><span class="action-badge action-${r.recommendation}">${r.recommendation}</span></div>
      <div class="metric"><div class="metric-label">Ban</div><button class="btn-dismiss" onclick="banMarket('${r.slug}')">✕ Ban</button></div>
    </div>`;
  }).join('');
}

function updateStats(results) {
  document.getElementById('stat-total').textContent = results.length;
  document.getElementById('stat-buy').textContent   = results.filter(r => r.recommendation === 'BUY').length;
  document.getElementById('stat-sell').textContent  = results.filter(r => r.recommendation === 'SELL').length;
  const avgEdge = results.reduce((s, r) => s + Math.abs(r.edge), 0) / results.length;
  document.getElementById('stat-edge').textContent  = (avgEdge * 100).toFixed(1) + '%';
  document.getElementById('stat-banned').textContent = getBanned().length;
}

// Init banned count on load
document.getElementById('stat-banned').textContent = getBanned().length;
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

        # Fetch large pool, exclude banned
        pool = get_markets(client, limit=100)
        pool = [m for m in pool if get_market_slug(m) not in banned]
        sample = random.sample(pool, min(limit, len(pool)))

        results = []
        cached_count = 0
        fresh_count = 0
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

                # Check cache
                cached = analysis_cache.get(slug)
                if cached and (now - cached["timestamp"]) < CACHE_TTL_SECONDS:
                    result = dict(cached["result"])
                    result["cached"] = True
                    result["current_price"] = price  # refresh live price
                    cached_count += 1
                else:
                    result = analyze_market(market, price)
                    result["cached"] = False
                    analysis_cache[slug] = {"result": dict(result), "timestamp": now}
                    fresh_count += 1

                result["slug"] = slug
                results.append(result)

            except Exception as e:
                results.append({
                    "question": question, "slug": slug,
                    "current_price": 0, "estimated_prob": 0, "edge": 0,
                    "confidence": "low", "recommendation": "SKIP",
                    "reasoning": f"Error: {str(e)}", "key_factors": [], "cached": False,
                })

        return jsonify({"results": results, "dry_run": DRY_RUN,
                        "cached_count": cached_count, "fresh_count": fresh_count})

    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        is_running = False


if __name__ == "__main__":
    print("🚀 Dashboard running at http://localhost:5000")
    app.run(debug=False, port=5000)