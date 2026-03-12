"""
Signal Engine v2.1 — multi-factor scoring para opções cripto de curto prazo.

Melhorias v2.1:
- Fix: relevance agora escala TAMBÉM o weight_total (efeito real de redução)
- Diferencia mercados de PREÇO (BTC>$X) vs EVENTO (aprovação, eleição etc.)
- Momentum com 3 janelas (1m, 5m, 15m)
- Detecção de tipo de mercado
- Confiança degradada para mercados de evento
"""
from __future__ import annotations
import math
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Literal


# ── Tipos de mercado ───────────────────────────────────────────────────────────

PRICE_KEYWORDS = [
    "above", "below", "over", "under", "reach", "hit", "exceed",
    "higher than", "lower than", "at least", "at most", "more than", "less than",
    "up or down"
]
EVENT_KEYWORDS = [
    "approve", "ban", "launch", "release", "win", "lose",
    "pass", "fail", "confirm", "resign", "happen", "occur", "announce",
]


def detect_market_type(question: str) -> Literal["price", "event", "unknown"]:
    q = question.lower()
    if any(k in q for k in PRICE_KEYWORDS):
        return "price"
    if any(k in q for k in EVENT_KEYWORDS):
        return "event"
    return "unknown"


# ── Data classes ───────────────────────────────────────────────────────────────

@dataclass
class Candle:
    ts: float
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class Signal:
    symbol:      str
    direction:   Literal["YES", "NO", "SKIP"]
    strength:    float
    confidence:  Literal["low", "medium", "high"]
    edge:        float
    reasons:     list[str]
    momentum_1m: float
    momentum_5m: float
    rsi:         float
    vwap_dev:    float
    bb_position: float
    market_type: str = "unknown"
    ts:          float = field(default_factory=time.time)


# ── PriceBuffer ────────────────────────────────────────────────────────────────

class PriceBuffer:
    def __init__(self, maxlen: int = 120):
        self.ticks:      deque[tuple[float, float]] = deque(maxlen=600)
        self.candles_1m: deque[Candle] = deque(maxlen=maxlen)
        self._candle_open: float | None = None
        self._candle_ts:   float | None = None
        self._candle_high  = 0.0
        self._candle_low   = 0.0
        self._candle_vol   = 0.0

    def push(self, price: float, volume: float = 1.0):
        now    = time.time()
        bucket = int(now // 60) * 60
        self.ticks.append((now, price))

        if self._candle_ts is None:
            self._candle_ts   = bucket
            self._candle_open = price
            self._candle_high = price
            self._candle_low  = price
            self._candle_vol  = 0.0
        elif bucket != self._candle_ts:
            self.candles_1m.append(Candle(
                ts=self._candle_ts, open=self._candle_open,
                high=self._candle_high, low=self._candle_low,
                close=price, volume=self._candle_vol,
            ))
            self._candle_ts   = bucket
            self._candle_open = price
            self._candle_high = price
            self._candle_low  = price
            self._candle_vol  = 0.0

        self._candle_high = max(self._candle_high, price)
        self._candle_low  = min(self._candle_low, price)
        self._candle_vol += volume

    def latest_price(self) -> float | None:
        return self.ticks[-1][1] if self.ticks else None

    def price_n_seconds_ago(self, seconds: int) -> float | None:
        now    = time.time()
        cutoff = now - seconds
        for ts, price in reversed(self.ticks):
            if ts <= cutoff:
                return price
        return self.ticks[0][1] if self.ticks else None

    def closes(self, n: int) -> list[float]:
        candles = list(self.candles_1m)[-n:]
        closes  = [c.close for c in candles]
        if self._candle_open is not None:
            closes.append(self._candle_open)
        return closes

    def vwap(self, minutes: int = 30) -> float | None:
        candles   = list(self.candles_1m)[-minutes:]
        if not candles:
            return None
        total_vol = sum(c.volume for c in candles)
        if total_vol == 0:
            return None
        return sum(((c.high + c.low + c.close) / 3) * c.volume for c in candles) / total_vol


# ── Indicadores ───────────────────────────────────────────────────────────────

def rsi(closes: list[float], period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100.0
    return 100 - (100 / (1 + avg_gain / avg_loss))


def bollinger(closes: list[float], period: int = 20, std_mult: float = 2.0) -> tuple[float, float, float]:
    if len(closes) < period:
        mid = closes[-1] if closes else 0
        return mid, mid, mid
    window   = closes[-period:]
    mid      = sum(window) / period
    variance = sum((x - mid) ** 2 for x in window) / period
    std      = math.sqrt(variance)
    return mid - std_mult * std, mid, mid + std_mult * std


def bb_position_fn(price: float, lower: float, upper: float) -> float:
    if upper == lower:
        return 0.5
    return max(0.0, min(1.0, (price - lower) / (upper - lower)))


# ── Signal principal ───────────────────────────────────────────────────────────

def compute_signal(
    symbol: str,
    buf: PriceBuffer,
    market_price_yes: float,
    horizon_minutes: int = 10,
    question: str = "",
) -> Signal:
    """
    Calcula sinal direcional para um mercado YES/NO de cripto.

    FIX v2.1: relevance escala tanto score quanto weight_total,
    garantindo que mercados de evento produzam edges menores de forma efetiva.
    """
    price_now   = buf.latest_price()
    market_type = detect_market_type(question) if question else "price"

    if price_now is None:
        return Signal(
            symbol=symbol, direction="SKIP", strength=0, confidence="low",
            edge=0, reasons=["No price data"], momentum_1m=0,
            momentum_5m=0, rsi=50, vwap_dev=0, bb_position=0.5,
            market_type=market_type,
        )

    # FIX: relevance escala AMBOS score E weight_total
    # Resultado: norm = (score*rel) / (weight*rel) = score/weight para price
    # Para event: norm = (score*0.4) / (weight*0.4) manteria o mesmo norm
    # Portanto o efeito correto é reduzir o score absoluto antes de normalizar
    # Fazemos: acumulamos raw_score e raw_weight, depois aplicamos relevance no final
    relevance = 1.0 if market_type == "price" else 0.40

    reasons:    list[str] = []
    raw_score   = 0.0
    raw_weight  = 0.0

    # ── 1. Momentum 1m ────────────────────────────────────────
    p1m    = buf.price_n_seconds_ago(60)
    mom_1m = ((price_now / p1m) - 1) * 100 if p1m else 0.0
    if abs(mom_1m) > 0.05:   # filtro noise
        w = 2.5
        raw_score  += mom_1m * w
        raw_weight += w
        if abs(mom_1m) > 0.15:
            reasons.append(f"{'↑' if mom_1m > 0 else '↓'} 1m {mom_1m:+.2f}%")
    else:
        raw_weight += 2.5

    # ── 2. Momentum 5m ────────────────────────────────────────
    p5m    = buf.price_n_seconds_ago(300)
    mom_5m = ((price_now / p5m) - 1) * 100 if p5m else 0.0
    if abs(mom_5m) > 0.10:
        w = 1.8
        raw_score  += mom_5m * w
        raw_weight += w
        if abs(mom_5m) > 0.30:
            reasons.append(f"{'↑' if mom_5m > 0 else '↓'} 5m {mom_5m:+.2f}%")
    else:
        raw_weight += 1.8

    # ── 3. Momentum 15m ───────────────────────────────────────
    p15m    = buf.price_n_seconds_ago(900)
    mom_15m = ((price_now / p15m) - 1) * 100 if p15m else 0.0
    if abs(mom_15m) > 0.20:
        w = 1.0
        raw_score  += mom_15m * w
        raw_weight += w
    else:
        raw_weight += 1.0

    # ── 4. RSI ────────────────────────────────────────────────
    closes     = buf.closes(30)
    r          = rsi(closes, period=min(14, max(3, len(closes) - 1)))
    rsi_weight = 1.2
    if r < 35:
        raw_score  += ((35 - r) / 35) * 2 * rsi_weight
        raw_weight += rsi_weight
        reasons.append(f"RSI oversold {r:.0f}")
    elif r > 65:
        raw_score  -= ((r - 65) / 35) * 2 * rsi_weight
        raw_weight += rsi_weight
        reasons.append(f"RSI overbought {r:.0f}")
    else:
        raw_weight += rsi_weight

    # ── 5. Bollinger Bands ────────────────────────────────────
    lower, mid_bb, upper = bollinger(closes, period=min(20, len(closes)))
    bb_pos    = bb_position_fn(price_now, lower, upper)
    bb_weight = 1.0
    momentum_strong = abs(mom_1m) > 0.25 or abs(mom_5m) > 0.50
    if not momentum_strong:
        if bb_pos > 0.85:
            raw_score  -= 1.5 * bb_weight
            raw_weight += bb_weight
            reasons.append(f"BB upper {bb_pos:.2f} → reversão")
        elif bb_pos < 0.15:
            raw_score  += 1.5 * bb_weight
            raw_weight += bb_weight
            reasons.append(f"BB lower {bb_pos:.2f} → bounce")
        else:
            raw_weight += bb_weight
    else:
        raw_weight += bb_weight

    # ── 6. VWAP deviation ────────────────────────────────────
    vwap_val = buf.vwap(minutes=30)
    vwap_dev = 0.0
    if vwap_val:
        vwap_dev   = (price_now / vwap_val - 1) * 100
        vw         = 1.1
        raw_weight += vw
        if abs(vwap_dev) > 0.3:
            raw_score -= math.copysign(1.0, vwap_dev) * min(abs(vwap_dev) / 0.5, 2.0) * vw
            reasons.append(f"VWAP {vwap_dev:+.2f}%")

    norm = (raw_score / raw_weight) if raw_weight > 0 else 0.0

    # Sigmoid → shift de probabilidade a partir de 0.5
    prob_shift = (1 / (1 + math.exp(-norm * 3))) - 0.5

    # FIX v2.1: aplica relevance APÓS o sigmoid (evita saturação)
    # Para eventos (relevance=0.40), o edge será sempre 40% do edge de preço
    # independente de quão saturado esteja o sigmoid
    prob_shift_scaled = prob_shift * relevance
    estimated_prob    = max(0.05, min(0.95, 0.5 + prob_shift_scaled))
    edge              = estimated_prob - market_price_yes

    # ── Confiança baseada em qualidade do buffer ──────────────
    n_ticks   = len(buf.ticks)
    n_candles = len(buf.candles_1m)

    if n_ticks > 300 and n_candles > 20:
        confidence: Literal["low", "medium", "high"] = "high"
    elif n_ticks > 100 and n_candles > 8:
        confidence = "medium"
    else:
        confidence = "low"

    # Degrada confiança para mercados de evento (spot menos preditivo)
    if market_type == "event":
        if confidence == "high":
            confidence = "medium"
        elif confidence == "medium":
            confidence = "low"

    # ── Thresholds de edge por confiança ──────────────────────
    min_edges = {"high": 0.05, "medium": 0.09, "low": 0.14}
    min_edge  = min_edges[confidence]

    if edge >= min_edge:
        direction: Literal["YES", "NO", "SKIP"] = "YES"
    elif edge <= -min_edge:
        direction = "NO"
    else:
        direction = "SKIP"

    strength = min(1.0, abs(edge) / 0.20)

    if not reasons:
        reasons.append("Sem sinal forte")

    return Signal(
        symbol=symbol, direction=direction, strength=strength,
        confidence=confidence, edge=edge, reasons=reasons,
        momentum_1m=mom_1m, momentum_5m=mom_5m, rsi=r,
        vwap_dev=vwap_dev, bb_position=bb_pos,
        market_type=market_type,
    )