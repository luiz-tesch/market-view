"""
Dashboard web local para o Polymarket Bot
Execute: python dashboard.py
Acesse: http://localhost:5000
"""
import json
import threading
from flask import Flask, jsonify, render_template_string
from src.config import validate, DRY_RUN
from src.market.client import get_client, get_markets, get_market_price, get_yes_token_id
from src.analysis.analyzer import analyze_market
from src.execution.risk import calculate_bet_size, can_bet

app = Flask(__name__)
latest_results = []
is_running = False

HTML = """<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Polymarket Bot</title>
<link href="https://fonts.googleapis.com/css2?family=Space+Mono:wght@400;700&family=Syne:wght@400;600;800&display=swap" rel="stylesheet">
<style>
  :root {
    --bg: #080c10;
    --surface: #0d1117;
    --border: #1a2030;
    --accent: #00ff88;
    --accent2: #ff3366;
    --accent3: #3399ff;
    --text: #e2e8f0;
    --muted: #4a5568;
    --buy: #00ff88;
    --sell: #ff3366;
    --skip: #4a5568;
  }

  * { margin: 0; padding: 0; box-sizing: border-box; }

  body {
    background: var(--bg);
    color: var(--text);
    font-family: 'Syne', sans-serif;
    min-height: 100vh;
    overflow-x: hidden;
  }

  body::before {
    content: '';
    position: fixed;
    top: -50%;
    left: -50%;
    width: 200%;
    height: 200%;
    background: radial-gradient(ellipse at 20% 20%, rgba(0,255,136,0.03) 0%, transparent 50%),
                radial-gradient(ellipse at 80% 80%, rgba(51,153,255,0.03) 0%, transparent 50%);
    pointer-events: none;
    z-index: 0;
  }

  .container {
    max-width: 1400px;
    margin: 0 auto;
    padding: 2rem;
    position: relative;
    z-index: 1;
  }

  header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 3rem;
    padding-bottom: 1.5rem;
    border-bottom: 1px solid var(--border);
  }

  .logo {
    display: flex;
    align-items: baseline;
    gap: 0.75rem;
  }

  .logo h1 {
    font-size: 1.8rem;
    font-weight: 800;
    letter-spacing: -0.02em;
    color: #fff;
  }

  .logo span {
    font-family: 'Space Mono', monospace;
    font-size: 0.7rem;
    color: var(--accent);
    background: rgba(0,255,136,0.08);
    border: 1px solid rgba(0,255,136,0.2);
    padding: 0.2rem 0.6rem;
    border-radius: 2px;
    letter-spacing: 0.1em;
  }

  .dry-badge {
    font-family: 'Space Mono', monospace;
    font-size: 0.65rem;
    color: #f59e0b;
    background: rgba(245,158,11,0.08);
    border: 1px solid rgba(245,158,11,0.25);
    padding: 0.3rem 0.8rem;
    border-radius: 2px;
    letter-spacing: 0.1em;
    animation: pulse-badge 2s ease-in-out infinite;
  }

  @keyframes pulse-badge {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.5; }
  }

  .stats-grid {
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: 1rem;
    margin-bottom: 2rem;
  }

  .stat-card {
    background: var(--surface);
    border: 1px solid var(--border);
    padding: 1.25rem 1.5rem;
    position: relative;
    overflow: hidden;
  }

  .stat-card::before {
    content: '';
    position: absolute;
    top: 0; left: 0;
    width: 3px; height: 100%;
  }

  .stat-card.green::before { background: var(--accent); }
  .stat-card.red::before { background: var(--accent2); }
  .stat-card.blue::before { background: var(--accent3); }
  .stat-card.yellow::before { background: #f59e0b; }

  .stat-label {
    font-size: 0.65rem;
    letter-spacing: 0.15em;
    color: var(--muted);
    text-transform: uppercase;
    margin-bottom: 0.5rem;
    font-family: 'Space Mono', monospace;
  }

  .stat-value {
    font-size: 2rem;
    font-weight: 800;
    letter-spacing: -0.03em;
    line-height: 1;
  }

  .stat-card.green .stat-value { color: var(--accent); }
  .stat-card.red .stat-value { color: var(--accent2); }
  .stat-card.blue .stat-value { color: var(--accent3); }
  .stat-card.yellow .stat-value { color: #f59e0b; }

  .controls {
    display: flex;
    gap: 1rem;
    margin-bottom: 2rem;
    align-items: center;
  }

  .btn-run {
    font-family: 'Space Mono', monospace;
    font-size: 0.75rem;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    background: var(--accent);
    color: #000;
    border: none;
    padding: 0.75rem 2rem;
    cursor: pointer;
    font-weight: 700;
    transition: all 0.15s;
    position: relative;
    overflow: hidden;
  }

  .btn-run:hover { background: #00cc6e; transform: translateY(-1px); }
  .btn-run:active { transform: translateY(0); }
  .btn-run:disabled {
    background: var(--muted);
    cursor: not-allowed;
    transform: none;
  }

  .status-text {
    font-family: 'Space Mono', monospace;
    font-size: 0.7rem;
    color: var(--muted);
    letter-spacing: 0.05em;
  }

  .status-text.running { color: var(--accent); }

  .spinner {
    display: inline-block;
    width: 10px; height: 10px;
    border: 2px solid var(--accent);
    border-top-color: transparent;
    border-radius: 50%;
    animation: spin 0.6s linear infinite;
    margin-right: 0.5rem;
    vertical-align: middle;
  }

  @keyframes spin { to { transform: rotate(360deg); } }

  .markets-grid {
    display: grid;
    gap: 0.75rem;
  }

  .market-card {
    background: var(--surface);
    border: 1px solid var(--border);
    padding: 1.25rem 1.5rem;
    display: grid;
    grid-template-columns: 1fr auto auto auto auto auto;
    gap: 1.5rem;
    align-items: center;
    transition: border-color 0.2s;
    animation: slideIn 0.3s ease both;
  }

  @keyframes slideIn {
    from { opacity: 0; transform: translateY(8px); }
    to { opacity: 1; transform: translateY(0); }
  }

  .market-card:hover { border-color: #2a3a50; }
  .market-card.buy { border-left: 3px solid var(--buy); }
  .market-card.sell { border-left: 3px solid var(--sell); }
  .market-card.skip { border-left: 3px solid var(--skip); }

  .market-question {
    font-size: 0.9rem;
    font-weight: 600;
    color: #fff;
    line-height: 1.3;
  }

  .market-reasoning {
    font-size: 0.72rem;
    color: var(--muted);
    margin-top: 0.4rem;
    line-height: 1.5;
    font-family: 'Space Mono', monospace;
  }

  .metric {
    text-align: center;
    min-width: 70px;
  }

  .metric-label {
    font-size: 0.58rem;
    letter-spacing: 0.12em;
    color: var(--muted);
    text-transform: uppercase;
    font-family: 'Space Mono', monospace;
    margin-bottom: 0.3rem;
  }

  .metric-value {
    font-size: 1.1rem;
    font-weight: 700;
    font-family: 'Space Mono', monospace;
  }

  .edge-positive { color: var(--accent); }
  .edge-negative { color: var(--accent2); }
  .edge-neutral { color: var(--muted); }

  .confidence-badge {
    font-family: 'Space Mono', monospace;
    font-size: 0.6rem;
    letter-spacing: 0.1em;
    padding: 0.2rem 0.5rem;
    border-radius: 2px;
    text-transform: uppercase;
  }

  .conf-high { background: rgba(0,255,136,0.1); color: var(--accent); border: 1px solid rgba(0,255,136,0.2); }
  .conf-medium { background: rgba(51,153,255,0.1); color: var(--accent3); border: 1px solid rgba(51,153,255,0.2); }
  .conf-low { background: rgba(74,85,104,0.2); color: var(--muted); border: 1px solid rgba(74,85,104,0.3); }

  .action-badge {
    font-family: 'Space Mono', monospace;
    font-size: 0.7rem;
    font-weight: 700;
    letter-spacing: 0.1em;
    padding: 0.4rem 0.9rem;
    border-radius: 2px;
    text-transform: uppercase;
    min-width: 65px;
    text-align: center;
  }

  .action-BUY { background: rgba(0,255,136,0.12); color: var(--buy); border: 1px solid rgba(0,255,136,0.3); }
  .action-SELL { background: rgba(255,51,102,0.12); color: var(--sell); border: 1px solid rgba(255,51,102,0.3); }
  .action-SKIP { background: rgba(74,85,104,0.15); color: var(--muted); border: 1px solid rgba(74,85,104,0.2); }

  .empty-state {
    text-align: center;
    padding: 5rem 2rem;
    color: var(--muted);
  }

  .empty-state .icon { font-size: 3rem; margin-bottom: 1rem; opacity: 0.3; }
  .empty-state p { font-family: 'Space Mono', monospace; font-size: 0.8rem; letter-spacing: 0.05em; }

  footer {
    margin-top: 3rem;
    padding-top: 1.5rem;
    border-top: 1px solid var(--border);
    font-family: 'Space Mono', monospace;
    font-size: 0.65rem;
    color: var(--muted);
    letter-spacing: 0.05em;
    display: flex;
    justify-content: space-between;
  }

  @media (max-width: 900px) {
    .stats-grid { grid-template-columns: repeat(2, 1fr); }
    .market-card { grid-template-columns: 1fr; gap: 0.75rem; }
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
    <div id="dry-badge" class="dry-badge">● DRY RUN</div>
  </header>

  <div class="stats-grid">
    <div class="stat-card green">
      <div class="stat-label">Mercados</div>
      <div class="stat-value" id="stat-total">—</div>
    </div>
    <div class="stat-card green">
      <div class="stat-label">BUY</div>
      <div class="stat-value" id="stat-buy">—</div>
    </div>
    <div class="stat-card red">
      <div class="stat-label">SELL</div>
      <div class="stat-value" id="stat-sell">—</div>
    </div>
    <div class="stat-card blue">
      <div class="stat-label">Edge Médio</div>
      <div class="stat-value" id="stat-edge">—</div>
    </div>
  </div>

  <div class="controls">
    <button class="btn-run" id="btn-run" onclick="runAnalysis()">▶ Analisar Mercados</button>
    <span class="status-text" id="status-text">Clique para iniciar análise</span>
  </div>

  <div class="markets-grid" id="markets-grid">
    <div class="empty-state">
      <div class="icon">◎</div>
      <p>Nenhuma análise ainda.<br>Clique em "Analisar Mercados" para começar.</p>
    </div>
  </div>

  <footer>
    <span>POLYMARKET BOT · DRY RUN MODE</span>
    <span id="last-update">—</span>
  </footer>
</div>

<script>
async function runAnalysis() {
  const btn = document.getElementById('btn-run');
  const status = document.getElementById('status-text');
  btn.disabled = true;
  status.className = 'status-text running';
  status.innerHTML = '<span class="spinner"></span>Analisando mercados...';

  document.getElementById('markets-grid').innerHTML = '<div class="empty-state"><div class="icon"><span class="spinner" style="width:24px;height:24px;border-width:3px"></span></div><p style="margin-top:1rem">Consultando Polymarket + LLaMA 3.3...</p></div>';

  try {
    const resp = await fetch('/api/analyze');
    const data = await resp.json();

    if (data.error) {
      status.innerHTML = '❌ ' + data.error;
      status.className = 'status-text';
      btn.disabled = false;
      return;
    }

    renderResults(data.results);
    updateStats(data.results);
    document.getElementById('last-update').textContent = 'Última análise: ' + new Date().toLocaleTimeString('pt-BR');
    status.innerHTML = `✓ ${data.results.length} mercados analisados`;
    status.className = 'status-text';
  } catch(e) {
    status.innerHTML = '❌ Erro: ' + e.message;
    status.className = 'status-text';
  }
  btn.disabled = false;
}

function renderResults(results) {
  const grid = document.getElementById('markets-grid');
  if (!results.length) {
    grid.innerHTML = '<div class="empty-state"><p>Nenhum mercado retornado.</p></div>';
    return;
  }

  grid.innerHTML = results.map((r, i) => {
    const edgeClass = r.edge > 0 ? 'edge-positive' : r.edge < 0 ? 'edge-negative' : 'edge-neutral';
    const edgeStr = (r.edge >= 0 ? '+' : '') + (r.edge * 100).toFixed(1) + '%';
    const cardClass = r.recommendation.toLowerCase();
    const confClass = 'conf-' + r.confidence;
    const delay = i * 0.05;

    return `
    <div class="market-card ${cardClass}" style="animation-delay:${delay}s">
      <div>
        <div class="market-question">${r.question}</div>
        <div class="market-reasoning">${r.reasoning}</div>
      </div>
      <div class="metric">
        <div class="metric-label">Atual</div>
        <div class="metric-value">${(r.current_price * 100).toFixed(1)}%</div>
      </div>
      <div class="metric">
        <div class="metric-label">LLaMA</div>
        <div class="metric-value">${(r.estimated_prob * 100).toFixed(1)}%</div>
      </div>
      <div class="metric">
        <div class="metric-label">Edge</div>
        <div class="metric-value ${edgeClass}">${edgeStr}</div>
      </div>
      <div class="metric">
        <div class="metric-label">Confiança</div>
        <span class="confidence-badge ${confClass}">${r.confidence}</span>
      </div>
      <div class="metric">
        <div class="metric-label">Ação</div>
        <span class="action-badge action-${r.recommendation}">${r.recommendation}</span>
      </div>
    </div>`;
  }).join('');
}

function updateStats(results) {
  document.getElementById('stat-total').textContent = results.length;
  document.getElementById('stat-buy').textContent = results.filter(r => r.recommendation === 'BUY').length;
  document.getElementById('stat-sell').textContent = results.filter(r => r.recommendation === 'SELL').length;
  const avgEdge = results.reduce((s, r) => s + Math.abs(r.edge), 0) / results.length;
  document.getElementById('stat-edge').textContent = (avgEdge * 100).toFixed(1) + '%';
}
</script>
</body>
</html>"""


@app.route("/")
def index():
    return render_template_string(HTML)


@app.route("/api/analyze")
def api_analyze():
    global is_running
    if is_running:
        return jsonify({"error": "Análise já em andamento, aguarde..."}), 429

    is_running = True
    try:
        validate()
        client = get_client()
        markets = get_markets(client, limit=10)
        results = []

        for market in markets:
            question = market.get("question", "?")
            token_id = get_yes_token_id(market)
            if not token_id:
                continue
            try:
                price = get_market_price(client, market)
                market["end_date_iso"] = market.get("endDateIso", "?")
                analysis = analyze_market(market, price)
                results.append(analysis)
            except Exception as e:
                results.append({
                    "question": question,
                    "current_price": 0,
                    "estimated_prob": 0,
                    "edge": 0,
                    "confidence": "low",
                    "recommendation": "SKIP",
                    "reasoning": f"Erro: {str(e)}",
                    "key_factors": [],
                })

        return jsonify({"results": results, "dry_run": DRY_RUN})

    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        is_running = False


if __name__ == "__main__":
    print("🚀 Dashboard rodando em http://localhost:5000")
    app.run(debug=False, port=5000)