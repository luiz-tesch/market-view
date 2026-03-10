# 🤖 Polymarket Bot

Bot de análise e trading automático no [Polymarket](https://polymarket.com), usando **Groq + LLaMA 3.3 70B** (gratuito) como motor de análise e **NewsAPI** (opcional, gratuito) para contexto de notícias.

---

## Como funciona

```
Polymarket Gamma API
        ↓
   Lista mercados ativos
        ↓
   NewsAPI (opcional)
   Busca notícias recentes
        ↓
   Groq / LLaMA 3.3 70B
   Estima probabilidade real
        ↓
   Risk Manager
   Kelly Criterion + limites diários
        ↓
   Execução (ou simulação em DRY_RUN)
```

O bot compara a **probabilidade estimada pelo LLaMA** com o **preço atual do mercado**. Se a diferença (edge) for maior que o mínimo configurado, ele recomenda BUY ou SELL.

---

## Setup

### 1. Clone e crie o ambiente virtual
```bash
git clone <seu-repo>
cd polymarket-bot
python -m venv venv
source venv/bin/activate        # Linux/Mac
venv\Scripts\activate           # Windows
```

### 2. Instale as dependências
```bash
pip install -r requirements.txt
```

### 3. Configure as credenciais
```bash
cp .env.example .env
# Edite o .env com suas chaves
```

### 4. Obtenha as credenciais

| Credencial | Onde obter | Obrigatório |
|---|---|---|
| `GROQ_API_KEY` | [console.groq.com](https://console.groq.com) → API Keys | ✅ Sim |
| `NEWS_API_KEY` | [newsapi.org](https://newsapi.org) → Get API Key | ❌ Opcional |
| `POLYGON_PRIVATE_KEY` | MetaMask → Export Private Key | Só para apostas reais |

> ⚠️ **NUNCA** commite o arquivo `.env` no git. Ele já está no `.gitignore`.

### 5. Rode em modo simulação
```bash
python main.py
```

O bot vai listar mercados, analisar cada um com o LLaMA e mostrar uma tabela com recomendações — **sem fazer nenhuma aposta real**.

---

## Ativando apostas reais

Quando estiver satisfeito com as análises, edite o `.env`:

```env
DRY_RUN=false
POLYGON_PRIVATE_KEY=sua_private_key_aqui
MAX_BET_USDC=5        # comece pequeno!
MAX_DAILY_USDC=20
```

Você também precisará de **USDC na rede Polygon** na sua wallet.

---

## Estrutura do projeto

```
polymarket-bot/
├── main.py                      # Entry point
├── requirements.txt
├── .env.example                 # Template — copie para .env
├── src/
│   ├── config.py                # Carrega e valida o .env
│   ├── market/
│   │   └── client.py            # Gamma API do Polymarket
│   ├── analysis/
│   │   ├── analyzer.py          # Groq/LLaMA analisa cada mercado
│   │   └── news.py              # Busca notícias via NewsAPI
│   └── execution/
│       └── risk.py              # Kelly Criterion + limites diários
└── logs/
    └── daily_ledger.json        # Registro de apostas por dia
```

---

## Parâmetros de risco (.env)

| Parâmetro | Descrição | Padrão |
|---|---|---|
| `DRY_RUN` | Simula sem apostar de verdade | `true` |
| `MAX_BET_USDC` | Valor máximo por aposta | `10` |
| `MAX_DAILY_USDC` | Limite de gasto diário total | `50` |
| `MIN_EDGE` | Edge mínimo para recomendar aposta | `0.05` (5%) |

O tamanho de cada aposta é calculado via **Kelly Criterion simplificado**, ajustado pelo nível de confiança do modelo (low / medium / high).

---

## Custo

| Serviço | Custo |
|---|---|
| Groq (LLaMA 3.3 70B) | **Gratuito** — 14.400 req/dia |
| NewsAPI | **Gratuito** — 100 req/dia |
| Polymarket API | **Gratuito** |
| USDC para apostas | Você define o valor |

---

## ⚠️ Aviso

Este projeto é para fins educacionais. Trading em mercados de previsão envolve **risco real de perda de capital**. Sempre comece com `DRY_RUN=true`, avalie as análises por alguns dias antes de ativar apostas reais, e nunca arrisque mais do que pode perder.