# 🤖 Polymarket Bot

Bot de análise e trading no [Polymarket](https://polymarket.com) usando Claude (Anthropic) como motor de análise.

## Como funciona

```
Polymarket API → lista mercados
       ↓
NewsAPI → busca notícias relevantes
       ↓
Claude → estima probabilidade real
       ↓
Risk Manager → calcula tamanho da aposta (Kelly Criterion)
       ↓
Execução → coloca ordem (ou simula em DRY_RUN)
```

## Setup

### 1. Clone e instale dependências
```bash
git clone <seu-repo>
cd polymarket-bot
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Configure as credenciais
```bash
cp .env.example .env
# Edite .env com suas chaves
```

### 3. Obtenha as credenciais

| Credencial | Onde obter |
|---|---|
| `POLYGON_PRIVATE_KEY` | Sua wallet MetaMask → Export Private Key |
| `ANTHROPIC_API_KEY` | [console.anthropic.com](https://console.anthropic.com) |
| `NEWS_API_KEY` | [newsapi.org](https://newsapi.org) (gratuito) |
| USDC na Polygon | Compre em exchange → bridge para Polygon |

> ⚠️ **NUNCA** compartilhe ou commite sua `POLYGON_PRIVATE_KEY`

### 4. Execute em modo simulação (DRY_RUN=true)
```bash
python main.py
```

### 5. Quando estiver confiante, ative apostas reais
```bash
# No .env:
DRY_RUN=false
MAX_BET_USDC=5      # comece pequeno!
MAX_DAILY_USDC=20
```

## Estrutura

```
polymarket-bot/
├── main.py                    # Entry point
├── requirements.txt
├── .env.example               # Template de configuração
├── src/
│   ├── config.py              # Carrega .env e valida
│   ├── market/
│   │   └── client.py          # API do Polymarket
│   ├── analysis/
│   │   ├── analyzer.py        # Claude analisa o mercado
│   │   └── news.py            # Busca notícias (NewsAPI)
│   └── execution/
│       └── risk.py            # Kelly Criterion + limites diários
└── logs/
    └── daily_ledger.json      # Registro de apostas por dia
```

## Parâmetros de risco

| Parâmetro | Descrição | Padrão |
|---|---|---|
| `MAX_BET_USDC` | Máximo por aposta | $10 |
| `MAX_DAILY_USDC` | Limite diário total | $50 |
| `MIN_EDGE` | Edge mínimo para apostar | 5% |
| `DRY_RUN` | Simular sem apostar | `true` |

## ⚠️ Aviso

Este bot é para fins educacionais. Trading em mercados de previsão envolve risco real de perda de capital. Comece sempre com `DRY_RUN=true` e valores pequenos.