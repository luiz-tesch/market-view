"""
Signal Engine v3.0 — multi-factor scoring para opções cripto de curto prazo.

Melhorias v3.0:
- CVD (Cumulative Volume Delta): pressão compradora vs vendedora
- Regime detection: Bollinger Bandwidth → chaveia momentum vs mean-reversion
- EMA 9/21 crossover como confirmação de tendência
- Fee-aware edge: desconta taxa dinâmica da Polymarket (até 3.15% em 50/50)
- Filtro near-50%: evita mercados com fee máxima
- End-of-cycle sniping: boost de confiança nos últimos 30s do mercado
"""
from __future__ import annotations
import math
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Literal


def polymarket_dynamic_fee(market_price: float) -> float:
    """Taxa dinâmica da Polymarket: máximo 3.15% em 50/50, cai para 0% nas extremidades.
    Fórmula: fee = 4 * 0.0315 * p * (1 - p)
    """
    p = max(0.01, min(0.99, market_price))
    return 4 * 0.0315 * p * (1 - p)


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
    fee_adjusted_edge: float
    reasons:     list[str]
    momentum_1m: float
    momentum_5m: float
    rsi:         float
    vwap_dev:    float
    bb_position: float
    cvd_signal:  float = 0.0
    regime:      str = "neutral"   # "trending" | "ranging" | "neutral"
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
        # CVD: acumula delta de volume (buy pressure - sell pressure)
        self._cvd:         float = 0.0
        self._prev_price:  float | None = None

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

        # CVD: heurística — se preço subiu, volume é buy; se caiu, é sell
        if self._prev_price is not None:
            if price > self._prev_price:
                self._cvd += volume
            elif price < self._prev_price:
                self._cvd -= volume
        self._prev_price = price

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

    def cvd_normalized(self, window: int = 50) -> float:
        """CVD normalizado em [-1, 1]: positivo = buy pressure dominante."""
        if not self.ticks or len(self.ticks) < 5:
            return 0.0
        # Normaliza pelo número de ticks para manter escala consistente
        total_ticks = max(len(self.ticks), 1)
        return max(-1.0, min(1.0, self._cvd / total_ticks))

    def bollinger_bandwidth(self, period: int = 20) -> float:
        """Bollinger Bandwidth: (upper - lower) / mid. Alto = trending, baixo = ranging."""
        closes = self.closes(period + 5)
        if len(closes) < period:
            return 0.0
        lower, mid, upper = bollinger(closes, period=period)
        if mid == 0:
            return 0.0
        return (upper - lower) / mid

    def ema(self, period: int, closes: list[float] | None = None) -> float | None:
        """EMA de `period` períodos sobre os closes dos candles."""
        c = closes if closes is not None else self.closes(period * 3)
        if len(c) < period:
            return None
        k = 2 / (period + 1)
        ema_val = sum(c[:period]) / period
        for price in c[period:]:
            ema_val = price * k + ema_val * (1 - k)
        return ema_val

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
    seconds_to_expiry: float | None = None,
) -> Signal:
    """
    Calcula sinal direcional para um mercado YES/NO de cripto.

    v3.0: Fee-aware edge, CVD, regime detection, EMA crossover, end-of-cycle sniping.
    """
    price_now   = buf.latest_price()
    market_type = detect_market_type(question) if question else "price"

    if price_now is None:
        return Signal(
            symbol=symbol, direction="SKIP", strength=0, confidence="low",
            edge=0, fee_adjusted_edge=0, reasons=["No price data"], momentum_1m=0,
            momentum_5m=0, rsi=50, vwap_dev=0, bb_position=0.5,
            market_type=market_type,
        )

    # Calcula taxa dinâmica da Polymarket para este mercado
    taker_fee = polymarket_dynamic_fee(market_price_yes)

    # Evita mercados near-50% com fees altíssimas — mas só bloqueia se MUITO perto
    # Range reduzido: só bloqueia 0.47-0.53 (antes era 0.42-0.58 que bloqueava quase tudo)
    near_fifty = abs(market_price_yes - 0.5) < 0.03
    if near_fifty and taker_fee > 0.030:
        reasons_skip = [f"Near-50% fee={taker_fee:.1%} muito alta para edge"]
        return Signal(
            symbol=symbol, direction="SKIP", strength=0, confidence="low",
            edge=0, fee_adjusted_edge=0, reasons=reasons_skip, momentum_1m=0,
            momentum_5m=0, rsi=50, vwap_dev=0, bb_position=0.5,
            market_type=market_type,
        )

    relevance = 1.0 if market_type == "price" else 0.40

    # Regime detection: Bollinger Bandwidth
    bb_bw = buf.bollinger_bandwidth(period=20)
    is_trending = bb_bw > 0.015   # expansão = trending
    regime = "trending" if is_trending else ("ranging" if bb_bw < 0.008 else "neutral")

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
    # Em regime trending, BB contribui menos (momentum domina)
    momentum_strong = abs(mom_1m) > 0.25 or abs(mom_5m) > 0.50
    if not momentum_strong and not is_trending:
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

    # ── 7. CVD — Cumulative Volume Delta ─────────────────────
    cvd_sig = buf.cvd_normalized()
    cvd_w = 1.5
    if abs(cvd_sig) > 0.15:
        raw_score  += cvd_sig * 2.0 * cvd_w
        raw_weight += cvd_w
        direction_str = "↑ buy" if cvd_sig > 0 else "↓ sell"
        reasons.append(f"CVD {direction_str} pressure {cvd_sig:+.2f}")
    else:
        raw_weight += cvd_w

    # ── 8. EMA 9/21 Crossover ────────────────────────────────
    ema_closes = buf.closes(60)
    ema9  = buf.ema(9, ema_closes)
    ema21 = buf.ema(21, ema_closes)
    ema_w = 1.3
    if ema9 is not None and ema21 is not None:
        ema_diff = (ema9 / ema21 - 1) * 100
        raw_weight += ema_w
        if abs(ema_diff) > 0.05:
            raw_score += math.copysign(1.0, ema_diff) * min(abs(ema_diff) / 0.1, 2.0) * ema_w
            if abs(ema_diff) > 0.15:
                reasons.append(f"EMA9/21 {'bull' if ema_diff > 0 else 'bear'} {ema_diff:+.2f}%")
    else:
        raw_weight += ema_w

    norm = (raw_score / raw_weight) if raw_weight > 0 else 0.0

    # Sigmoid → shift de probabilidade a partir de 0.5
    prob_shift = (1 / (1 + math.exp(-norm * 3))) - 0.5

    # Aplica relevance APÓS o sigmoid
    prob_shift_scaled = prob_shift * relevance
    estimated_prob    = max(0.05, min(0.95, 0.5 + prob_shift_scaled))
    edge              = estimated_prob - market_price_yes

    # ── Fee-adjusted edge ─────────────────────────────────────
    # Fee é paga pelo taker; descontamos do edge bruto
    fee_adjusted_edge = abs(edge) - taker_fee
    # Se fee_adjusted_edge negativo, não há vantagem real
    if fee_adjusted_edge < 0:
        return Signal(
            symbol=symbol, direction="SKIP", strength=0, confidence="low",
            edge=edge, fee_adjusted_edge=fee_adjusted_edge,
            reasons=[f"Edge {edge:.1%} < fee {taker_fee:.1%}"],
            momentum_1m=mom_1m, momentum_5m=mom_5m, rsi=r,
            vwap_dev=vwap_dev, bb_position=bb_pos,
            cvd_signal=cvd_sig, regime=regime, market_type=market_type,
        )

    # ── Confiança baseada em qualidade do buffer ──────────────
    n_ticks   = len(buf.ticks)
    n_candles = len(buf.candles_1m)

    if n_ticks > 300 and n_candles > 20:
        confidence: Literal["low", "medium", "high"] = "high"
    elif n_ticks > 100 and n_candles > 8:
        confidence = "medium"
    else:
        confidence = "low"

    # End-of-cycle sniping: boost confiança nos últimos 30s
    # Quando mercado está prestes a resolver, momentum já "locked in"
    end_of_cycle = False
    if seconds_to_expiry is not None and 0 < seconds_to_expiry <= 30:
        end_of_cycle = True
        # Sobe confiança se momentum está claro
        if abs(mom_1m) > 0.08 and confidence == "medium":
            confidence = "high"
            reasons.append(f"End-of-cycle snipe T-{seconds_to_expiry:.0f}s")
        elif abs(mom_1m) > 0.08 and confidence == "low":
            confidence = "medium"
            reasons.append(f"End-of-cycle T-{seconds_to_expiry:.0f}s")

    # Degrada confiança para mercados de evento (spot menos preditivo)
    if market_type == "event":
        if confidence == "high":
            confidence = "medium"
        elif confidence == "medium":
            confidence = "low"

    # ── Thresholds de edge por confiança (fee-adjusted) ───────
    # Thresholds são sobre o fee_adjusted_edge, não o edge bruto
    min_edges = {"high": 0.035, "medium": 0.07, "low": 0.12}
    min_edge  = min_edges[confidence]

    if fee_adjusted_edge >= min_edge and edge > 0:
        direction: Literal["YES", "NO", "SKIP"] = "YES"
    elif fee_adjusted_edge >= min_edge and edge < 0:
        direction = "NO"
    else:
        direction = "SKIP"

    strength = min(1.0, fee_adjusted_edge / 0.15)

    if not reasons:
        reasons.append("Sem sinal forte")

    return Signal(
        symbol=symbol, direction=direction, strength=strength,
        confidence=confidence, edge=edge, fee_adjusted_edge=fee_adjusted_edge,
        reasons=reasons, momentum_1m=mom_1m, momentum_5m=mom_5m, rsi=r,
        vwap_dev=vwap_dev, bb_position=bb_pos,
        cvd_signal=cvd_sig, regime=regime, market_type=market_type,
    )