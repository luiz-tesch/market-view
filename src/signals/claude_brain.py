"""
src/signals/claude_brain.py — LLM como cérebro de decisão de trades

Usa Gemini Flash (grátis e rápido) ou Groq como decisor inteligente.
O modelo recebe dados de mercado, preços e contexto e decide:
  - Direção (YES/NO/SKIP)
  - Confiança (low/medium/high)
  - Razões da decisão

Substitui o motor de indicadores técnicos que tinha 3% win rate.
"""
from __future__ import annotations

import json
import os
import time
import threading
from dataclasses import dataclass

from dotenv import load_dotenv
load_dotenv()

from src.signals.engine import PriceBuffer, polymarket_dynamic_fee

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GEMINI_MODEL = "gemini-2.0-flash"
GROQ_MODEL = "llama-3.1-8b-instant"  # 8B é ~5x mais leve, limite diário maior no free tier

# Qual provider usar (prioriza Groq por ser mais confiável no free tier)
LLM_PROVIDER = "groq" if GROQ_API_KEY else ("gemini" if GEMINI_API_KEY else "")
LLM_AVAILABLE = bool(LLM_PROVIDER)

# Rate limiting adaptativo
_last_call_ts = 0.0
_call_lock = threading.Lock()
MIN_CALL_INTERVAL = 3.0  # segundos entre chamadas
_rate_limited_until = 0.0  # timestamp até quando estamos bloqueados por 429
_consecutive_429 = 0

# Cache de decisões
_decision_cache: dict[str, tuple[float, "LLMSignal"]] = {}
CACHE_TTL = 60  # segundos (aumentado para economizar tokens)


@dataclass
class LLMSignal:
    direction: str       # "YES", "NO", "SKIP"
    confidence: str      # "low", "medium", "high"
    edge: float
    fee_adjusted_edge: float
    strength: float
    momentum_1m: float
    momentum_5m: float
    rsi: float
    vwap_dev: float
    bb_position: float
    reasons: list[str]
    market_type: str
    symbol: str
    raw_reasoning: str


def _simple_rsi(closes: list[float], period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50.0
    deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    recent = deltas[-period:]
    gains = [d for d in recent if d > 0]
    losses = [-d for d in recent if d < 0]
    avg_gain = sum(gains) / period if gains else 0
    avg_loss = sum(losses) / period if losses else 0
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def _build_prompt(
    symbol: str,
    buf: PriceBuffer,
    market_price_yes: float,
    horizon_minutes: int,
    question: str,
    seconds_to_expiry: float | None,
) -> str:
    latest = buf.latest_price()
    ticks = list(buf.ticks)
    candles = list(buf.candles_1m)

    recent_candles = []
    for c in candles[-10:]:
        recent_candles.append({
            "o": round(c.open, 2),
            "h": round(c.high, 2),
            "l": round(c.low, 2),
            "c": round(c.close, 2),
            "v": round(c.volume, 1),
        })

    closes = buf.closes(60)
    rsi = _simple_rsi(closes) if len(closes) >= 14 else 50.0
    vwap = buf.vwap(30) or latest or 0.0

    mom_1m = mom_5m = 0.0
    if latest and len(ticks) > 60:
        p_1m = ticks[-60][1]
        mom_1m = (latest / p_1m - 1) * 100 if p_1m > 0 else 0
    if latest and len(ticks) > 300:
        p_5m = ticks[-300][1]
        mom_5m = (latest / p_5m - 1) * 100 if p_5m > 0 else 0

    fee = polymarket_dynamic_fee(market_price_yes)

    # Trend analysis from candles
    trend = "FLAT"
    if len(recent_candles) >= 3:
        last3_closes = [c["c"] for c in recent_candles[-3:]]
        if all(last3_closes[i] > last3_closes[i-1] for i in range(1, len(last3_closes))):
            trend = "UP"
        elif all(last3_closes[i] < last3_closes[i-1] for i in range(1, len(last3_closes))):
            trend = "DOWN"

    # Price change over candle window
    if recent_candles:
        pct_change = (recent_candles[-1]["c"] / recent_candles[0]["o"] - 1) * 100
    else:
        pct_change = 0.0

    # Prompt compacto para economizar tokens
    candles_str = json.dumps(recent_candles[-5:])  # últimos 5 candles apenas
    prompt = f"""Crypto binary option trader. Respond ONLY valid JSON.

Q: {question}
YES={market_price_yes:.3f} NO={1-market_price_yes:.3f} T-{seconds_to_expiry:.0f}s Fee={fee*100:.1f}%
{symbol} ${latest:.2f} RSI={rsi:.1f} VWAP=${vwap:.2f} Mom1m={mom_1m:+.3f}% Mom5m={mom_5m:+.3f}% Trend={trend} Chg={pct_change:+.3f}%
Candles: {candles_str}

Rules: YES=price UP, NO=price DOWN. Edge must beat fee. SKIP if unsure.
{{"direction":"YES"|"NO"|"SKIP","confidence":"low"|"medium"|"high","edge":0.05,"reason":"brief"}}"""

    return prompt


def _call_gemini(prompt: str) -> str | None:
    try:
        from google import genai

        client = genai.Client(api_key=GEMINI_API_KEY)
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
            config={
                "temperature": 0.2,
                "max_output_tokens": 200,
            },
        )
        return response.text.strip()
    except Exception as e:
        print(f"[LLM-Gemini] Error: {e}")
        return None


def _call_groq(prompt: str) -> str | None:
    global _rate_limited_until, _consecutive_429

    # Se estamos em cooldown por 429, tenta Gemini direto
    now = time.time()
    if now < _rate_limited_until:
        if GEMINI_API_KEY:
            return _call_gemini(prompt)
        return None

    try:
        from groq import Groq

        client = Groq(api_key=GROQ_API_KEY)
        response = client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
            max_tokens=150,
        )
        _consecutive_429 = 0
        return response.choices[0].message.content.strip()
    except Exception as e:
        err_str = str(e)
        if "429" in err_str or "rate_limit" in err_str:
            _consecutive_429 += 1
            # Backoff exponencial: 60s, 120s, 240s, max 600s
            cooldown = min(60 * (2 ** (_consecutive_429 - 1)), 600)
            _rate_limited_until = time.time() + cooldown
            print(f"[LLM-Groq] Rate limited — cooldown {cooldown}s, fallback Gemini={'yes' if GEMINI_API_KEY else 'no'}")
            if GEMINI_API_KEY:
                return _call_gemini(prompt)
        else:
            print(f"[LLM-Groq] Error: {e}")
        return None


def llm_decide(
    symbol: str,
    buf: PriceBuffer,
    market_price_yes: float,
    horizon_minutes: int,
    question: str = "",
    seconds_to_expiry: float | None = None,
) -> LLMSignal | None:
    """
    Chama o LLM (Gemini ou Groq) para decidir se deve operar.
    Retorna LLMSignal ou None se não conseguir.
    """
    global _last_call_ts

    if not LLM_AVAILABLE:
        return None

    # Cache check
    cache_key = f"{symbol}:{question[:30]}:{round(market_price_yes, 2)}"
    now = time.time()
    if cache_key in _decision_cache:
        cached_ts, cached_result = _decision_cache[cache_key]
        if now - cached_ts < CACHE_TTL:
            return cached_result

    # Rate limiting
    with _call_lock:
        elapsed = now - _last_call_ts
        if elapsed < MIN_CALL_INTERVAL:
            return None
        _last_call_ts = now

    latest = buf.latest_price()
    if not latest:
        return None

    ticks = list(buf.ticks)
    mom_1m = mom_5m = 0.0
    if len(ticks) > 60:
        p_1m = ticks[-60][1]
        mom_1m = (latest / p_1m - 1) * 100 if p_1m > 0 else 0
    if len(ticks) > 300:
        p_5m = ticks[-300][1]
        mom_5m = (latest / p_5m - 1) * 100 if p_5m > 0 else 0

    rsi = _simple_rsi(buf.closes(60))
    vwap = buf.vwap(30) or latest
    fee = polymarket_dynamic_fee(market_price_yes)

    prompt = _build_prompt(
        symbol, buf, market_price_yes, horizon_minutes,
        question, seconds_to_expiry,
    )

    # Call LLM
    if LLM_PROVIDER == "gemini":
        text = _call_gemini(prompt)
    else:
        text = _call_groq(prompt)

    if not text:
        return None

    try:
        # Clean markdown wrapping
        clean = text
        if clean.startswith("```"):
            clean = clean.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        if clean.startswith("`"):
            clean = clean.strip("`")

        result = json.loads(clean)

        direction = result.get("direction", "SKIP").upper()
        confidence = result.get("confidence", "low").lower()
        edge_est = float(result.get("edge", 0.0))
        reasoning = result.get("reason", "") or result.get("reasoning", "")

        if direction not in ("YES", "NO", "SKIP"):
            direction = "SKIP"
        if confidence not in ("low", "medium", "high"):
            confidence = "low"

        fee_adj = max(0, abs(edge_est) - fee)
        edge = edge_est if direction == "YES" else -abs(edge_est)

        signal = LLMSignal(
            direction=direction,
            confidence=confidence,
            edge=edge,
            fee_adjusted_edge=fee_adj,
            strength=abs(edge_est),
            momentum_1m=round(mom_1m, 3),
            momentum_5m=round(mom_5m, 3),
            rsi=round(rsi, 1),
            vwap_dev=round((latest - vwap) / vwap * 100 if vwap else 0, 3),
            bb_position=0.5,
            reasons=[reasoning],
            market_type="price",
            symbol=symbol,
            raw_reasoning=reasoning,
        )

        _decision_cache[cache_key] = (now, signal)

        # Clean expired cache
        expired = [k for k, (ts, _) in _decision_cache.items() if now - ts > CACHE_TTL * 2]
        for k in expired:
            del _decision_cache[k]

        tag = "Gemini" if LLM_PROVIDER == "gemini" else "Groq"
        print(f"[{tag}] {symbol} {direction} conf={confidence} edge={edge_est:.3f} fee_adj={fee_adj:.3f} | {reasoning[:60]}")
        return signal

    except json.JSONDecodeError as e:
        print(f"[LLM] JSON parse error: {e} | raw: {text[:120]}")
        return None
    except Exception as e:
        print(f"[LLM] Error parsing: {e}")
        return None


# Aliases para compatibilidade
claude_decide = llm_decide
ANTHROPIC_API_KEY = GEMINI_API_KEY or GROQ_API_KEY  # truthy se algum LLM disponível
