"""
Signal Engine — multi-factor scoring for 5-15min crypto options
Combines: momentum, RSI, VWAP deviation, Bollinger squeeze
"""
from __future__ import annotations
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Literal
import math


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
    symbol: str
    direction: Literal["YES", "NO", "SKIP"]
    strength: float          # 0-1
    confidence: Literal["low", "medium", "high"]
    edge: float              # estimated edge vs market price
    reasons: list[str]
    momentum_1m: float
    momentum_5m: float
    rsi: float
    vwap_dev: float          # % deviation from VWAP
    bb_position: float       # 0=lower band, 1=upper band
    ts: float = field(default_factory=time.time)


class PriceBuffer:
    """Rolling buffer of candle data per symbol."""

    def __init__(self, maxlen: int = 120):
        self.ticks: deque[tuple[float, float]] = deque(maxlen=500)  # (ts, price)
        self.candles_1m: deque[Candle] = deque(maxlen=maxlen)
        self._candle_open: float | None = None
        self._candle_ts: float | None = None
        self._candle_high = 0.0
        self._candle_low = 0.0
        self._candle_vol = 0.0

    def push(self, price: float, volume: float = 1.0):
        now = time.time()
        self.ticks.append((now, price))

        # Build 1-min candles
        bucket = int(now // 60) * 60
        if self._candle_ts is None:
            self._candle_ts = bucket
            self._candle_open = price
            self._candle_high = price
            self._candle_low = price
            self._candle_vol = 0.0
        elif bucket != self._candle_ts:
            # Close previous candle
            self.candles_1m.append(Candle(
                ts=self._candle_ts,
                open=self._candle_open,
                high=self._candle_high,
                low=self._candle_low,
                close=price,
                volume=self._candle_vol,
            ))
            self._candle_ts = bucket
            self._candle_open = price
            self._candle_high = price
            self._candle_low = price
            self._candle_vol = 0.0

        self._candle_high = max(self._candle_high, price)
        self._candle_low = min(self._candle_low, price)
        self._candle_vol += volume

    def latest_price(self) -> float | None:
        return self.ticks[-1][1] if self.ticks else None

    def price_n_seconds_ago(self, seconds: int) -> float | None:
        now = time.time()
        cutoff = now - seconds
        for ts, price in reversed(self.ticks):
            if ts <= cutoff:
                return price
        return self.ticks[0][1] if self.ticks else None

    def closes(self, n: int) -> list[float]:
        candles = list(self.candles_1m)[-n:]
        closes = [c.close for c in candles]
        if self._candle_open is not None:
            closes.append(self._candle_open)  # current open as last
        return closes

    def vwap(self, minutes: int = 30) -> float | None:
        candles = list(self.candles_1m)[-minutes:]
        if not candles:
            return None
        total_vol = sum(c.volume for c in candles)
        if total_vol == 0:
            return None
        vwap = sum(((c.high + c.low + c.close) / 3) * c.volume for c in candles) / total_vol
        return vwap


# ─── Indicators ───────────────────────────────────────────────────────────────

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
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def bollinger(closes: list[float], period: int = 20, std_mult: float = 2.0) -> tuple[float, float, float]:
    """Returns (lower, mid, upper)."""
    if len(closes) < period:
        mid = closes[-1] if closes else 0
        return mid, mid, mid
    window = closes[-period:]
    mid = sum(window) / period
    variance = sum((x - mid) ** 2 for x in window) / period
    std = math.sqrt(variance)
    return mid - std_mult * std, mid, mid + std_mult * std


def bb_position(price: float, lower: float, upper: float) -> float:
    """0 = at lower band, 1 = at upper band."""
    if upper == lower:
        return 0.5
    return max(0.0, min(1.0, (price - lower) / (upper - lower)))


# ─── Main Signal Function ──────────────────────────────────────────────────────

def compute_signal(symbol: str, buf: PriceBuffer, market_price_yes: float, horizon_minutes: int = 10) -> Signal:
    """
    Compute a directional signal for a YES/NO crypto options market.

    market_price_yes: current Polymarket implied probability for YES (0-1)
    horizon_minutes: option expiry horizon
    """
    price_now = buf.latest_price()
    if price_now is None:
        return Signal(symbol=symbol, direction="SKIP", strength=0, confidence="low",
                      edge=0, reasons=["No price data"], momentum_1m=0,
                      momentum_5m=0, rsi=50, vwap_dev=0, bb_position=0.5)

    reasons: list[str] = []
    score = 0.0  # positive = bullish (YES), negative = bearish (NO)
    weight_total = 0.0

    # ── 1. Momentum 1m ──────────────────────────────────────
    p1m = buf.price_n_seconds_ago(60)
    mom_1m = ((price_now / p1m) - 1) * 100 if p1m else 0.0
    mom_weight = 2.0
    score += mom_1m * mom_weight
    weight_total += mom_weight
    if abs(mom_1m) > 0.15:
        reasons.append(f"{'↑' if mom_1m > 0 else '↓'} 1m mom {mom_1m:+.2f}%")

    # ── 2. Momentum 5m ──────────────────────────────────────
    p5m = buf.price_n_seconds_ago(300)
    mom_5m = ((price_now / p5m) - 1) * 100 if p5m else 0.0
    mom5_weight = 1.5
    score += mom_5m * mom5_weight
    weight_total += mom5_weight
    if abs(mom_5m) > 0.3:
        reasons.append(f"{'↑' if mom_5m > 0 else '↓'} 5m mom {mom_5m:+.2f}%")

    # ── 3. RSI ───────────────────────────────────────────────
    closes = buf.closes(30)
    r = rsi(closes, period=min(14, max(3, len(closes) - 1)))
    rsi_weight = 1.0
    if r < 35:
        rsi_score = (35 - r) / 35  # bullish oversold
        score += rsi_score * 2 * rsi_weight
        reasons.append(f"RSI oversold {r:.0f}")
    elif r > 65:
        rsi_score = (r - 65) / 35  # bearish overbought
        score -= rsi_score * 2 * rsi_weight
        reasons.append(f"RSI overbought {r:.0f}")
    weight_total += rsi_weight

    # ── 4. Bollinger Band position ───────────────────────────
    lower, mid_bb, upper = bollinger(closes, period=min(20, len(closes)))
    bb_pos = bb_position(price_now, lower, upper)
    bb_weight = 1.2

    # For short-horizon options: near upper band → expect mean reversion → NO
    # near lower band → expect bounce → YES
    # But if momentum is strong, follow momentum instead
    momentum_strong = abs(mom_1m) > 0.25 or abs(mom_5m) > 0.5
    if not momentum_strong:
        if bb_pos > 0.85:
            score -= 1.5 * bb_weight
            reasons.append(f"BB upper {bb_pos:.2f} → reversion risk")
        elif bb_pos < 0.15:
            score += 1.5 * bb_weight
            reasons.append(f"BB lower {bb_pos:.2f} → bounce zone")
    weight_total += bb_weight

    # ── 5. VWAP deviation ────────────────────────────────────
    vwap = buf.vwap(minutes=30)
    vwap_dev = 0.0
    if vwap:
        vwap_dev = (price_now / vwap - 1) * 100
        vwap_weight = 1.0
        if abs(vwap_dev) > 0.3:
            score -= math.copysign(1.0, vwap_dev) * min(abs(vwap_dev) / 0.5, 2.0) * vwap_weight
            reasons.append(f"VWAP dev {vwap_dev:+.2f}%")
        weight_total += vwap_weight

    # ── Normalize score → estimated_prob ────────────────────
    if weight_total > 0:
        norm = score / weight_total
    else:
        norm = 0.0

    # Sigmoid squash → probability shift from 0.5 baseline
    prob_shift = (1 / (1 + math.exp(-norm * 3))) - 0.5  # -0.5 to +0.5
    estimated_prob_yes = max(0.05, min(0.95, 0.5 + prob_shift))

    # ── Edge vs Polymarket price ─────────────────────────────
    edge = estimated_prob_yes - market_price_yes

    # ── Confidence based on data quality ────────────────────
    n_ticks = len(buf.ticks)
    n_candles = len(buf.candles_1m)
    if n_ticks > 200 and n_candles > 15:
        confidence: Literal["low", "medium", "high"] = "high"
    elif n_ticks > 80 and n_candles > 5:
        confidence = "medium"
    else:
        confidence = "low"

    # ── Minimum edge thresholds by confidence ───────────────
    min_edges = {"high": 0.05, "medium": 0.08, "low": 0.12}
    min_edge = min_edges[confidence]

    if edge >= min_edge:
        direction: Literal["YES", "NO", "SKIP"] = "YES"
    elif edge <= -min_edge:
        direction = "NO"
    else:
        direction = "SKIP"

    strength = min(1.0, abs(edge) / 0.20)

    if not reasons:
        reasons.append("No strong signal")

    return Signal(
        symbol=symbol,
        direction=direction,
        strength=strength,
        confidence=confidence,
        edge=edge,
        reasons=reasons,
        momentum_1m=mom_1m,
        momentum_5m=mom_5m,
        rsi=r,
        vwap_dev=vwap_dev,
        bb_position=bb_pos,
    )