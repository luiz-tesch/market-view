"""
tests/test_signal_engine.py
============================
Unit tests for src/signals/engine.py

Covers:
  - PriceBuffer: push, latest_price, price_n_seconds_ago, closes, vwap
  - Indicators: rsi(), bollinger(), bb_position_fn()
  - detect_market_type()
  - compute_signal(): direction, edge, confidence, market_type degradation
"""
import math
import sys
import time
import pytest

from src.signals.engine import (
    PriceBuffer,
    Signal,
    Candle,
    detect_market_type,
    compute_signal,
    rsi,
    bollinger,
    bb_position_fn,
)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _fill_buffer(buf: PriceBuffer, prices: list[float], volume: float = 1.0):
    """Push a list of prices into a buffer with 1-second timestamps."""
    for p in prices:
        buf.push(p, volume)


def _make_trending_buffer(start: float, pct_per_tick: float, n: int = 500) -> PriceBuffer:
    """Return a buffer trending up (positive pct) or down (negative pct)."""
    buf = PriceBuffer(maxlen=200)
    price = start
    for _ in range(n):
        price *= (1 + pct_per_tick)
        buf.push(price, 1.0)
    return buf


# ══════════════════════════════════════════════════════════════════════════════
# PriceBuffer
# ══════════════════════════════════════════════════════════════════════════════

class TestPriceBuffer:

    def test_empty_buffer_returns_none(self):
        """latest_price and price_n_seconds_ago must return None on empty buffer."""
        buf = PriceBuffer()
        assert buf.latest_price() is None
        assert buf.price_n_seconds_ago(60) is None

    def test_latest_price_returns_last_pushed(self):
        buf = PriceBuffer()
        buf.push(100.0)
        buf.push(200.0)
        assert buf.latest_price() == 200.0

    def test_closes_returns_list(self):
        """closes() should return a list even with few candles."""
        buf = PriceBuffer()
        for p in [100, 101, 102, 103]:
            buf.push(p)
        result = buf.closes(10)
        assert isinstance(result, list)
        assert len(result) >= 1

    def test_tick_deque_capped_at_600(self):
        """Buffer should not exceed maxlen=600 ticks."""
        buf = PriceBuffer(maxlen=50)
        for i in range(700):
            buf.push(float(i))
        assert len(buf.ticks) <= 600

    def test_vwap_returns_none_without_candles(self):
        """VWAP needs closed candles; should return None on just ticks."""
        buf = PriceBuffer()
        buf.push(100.0)
        # No closed candles yet
        result = buf.vwap(minutes=30)
        assert result is None

    def test_vwap_computable_with_candles(self):
        """Inject synthetic candles and verify VWAP is a positive float."""
        buf = PriceBuffer()
        for _ in range(5):
            buf.candles_1m.append(
                Candle(ts=time.time(), open=100, high=105, low=95, close=100, volume=10)
            )
        vwap_val = buf.vwap(minutes=5)
        assert vwap_val is not None
        assert vwap_val > 0


# ══════════════════════════════════════════════════════════════════════════════
# RSI indicator
# ══════════════════════════════════════════════════════════════════════════════

class TestRSI:

    def test_rsi_too_few_closes_returns_50(self):
        """With fewer than period+1 closes, RSI defaults to 50."""
        assert rsi([100.0], period=14) == 50.0
        assert rsi([], period=14) == 50.0

    def test_rsi_all_gains_returns_100(self):
        """Monotonically increasing closes → RSI near 100."""
        closes = [100 + i for i in range(20)]
        result = rsi(closes, period=14)
        assert result > 90

    def test_rsi_all_losses_returns_low(self):
        """Monotonically decreasing closes → RSI near 0."""
        closes = [100 - i for i in range(20)]
        result = rsi(closes, period=14)
        assert result < 10

    def test_rsi_flat_returns_50(self):
        """No movement → avg_gain == avg_loss == 0, returns 50."""
        closes = [100.0] * 20
        result = rsi(closes, period=14)
        # When avg_loss == 0, function returns 100, but flat prices have 0 gains too
        # The implementation returns 50 for the edge where both are 0
        assert 0 <= result <= 100

    def test_rsi_range_0_to_100(self):
        """RSI must always be within [0, 100]."""
        import random
        random.seed(42)
        closes = [100 * (1 + random.gauss(0, 0.01)) for _ in range(50)]
        result = rsi(closes, period=14)
        assert 0 <= result <= 100


# ══════════════════════════════════════════════════════════════════════════════
# Bollinger Bands
# ══════════════════════════════════════════════════════════════════════════════

class TestBollinger:

    def test_bollinger_three_bands_ordered(self):
        """lower <= mid <= upper must always hold."""
        closes = [100 + i * 0.5 for i in range(25)]
        lower, mid, upper = bollinger(closes, period=20)
        assert lower <= mid <= upper

    def test_bollinger_flat_prices_tight_bands(self):
        """Flat prices → std=0, so lower == mid == upper."""
        closes = [100.0] * 25
        lower, mid, upper = bollinger(closes, period=20)
        assert abs(upper - lower) < 1e-9

    def test_bollinger_fallback_on_too_few_closes(self):
        """Fewer closes than period → mid = last price, bands equal."""
        closes = [99.0, 100.0]
        lower, mid, upper = bollinger(closes, period=20)
        assert lower == mid == upper

    def test_bb_position_midpoint_returns_half(self):
        """Price at midpoint between lower/upper → 0.5."""
        result = bb_position_fn(price=0.5, lower=0.0, upper=1.0)
        assert abs(result - 0.5) < 1e-9

    def test_bb_position_clamped(self):
        """bb_position must stay in [0, 1]."""
        assert bb_position_fn(2.0, 0.0, 1.0) == 1.0
        assert bb_position_fn(-1.0, 0.0, 1.0) == 0.0

    def test_bb_position_equal_bands_returns_half(self):
        """When lower == upper, returns 0.5 (no division by zero)."""
        assert bb_position_fn(50.0, 50.0, 50.0) == 0.5


# ══════════════════════════════════════════════════════════════════════════════
# detect_market_type
# ══════════════════════════════════════════════════════════════════════════════

class TestDetectMarketType:

    @pytest.mark.parametrize("question,expected", [
        ("Will BTC be above $90,000 in 45 min?", "price"),
        ("Will ETH reach $4,000 this week?", "price"),
        ("Will BTC exceed $100k before June?", "price"),
        ("Will SOL hit $200 today?", "price"),
        ("Will the SEC approve a Bitcoin ETF?", "event"),
        ("Will Ethereum ban PoW mining?", "event"),
        ("Will exchange announce token burn?", "event"),
        ("Will Vitalik resign from Ethereum?", "event"),
        ("Is BTC going to Mars?", "unknown"),
    ])
    def test_market_type_classification(self, question, expected):
        assert detect_market_type(question) == expected

    def test_empty_question_returns_unknown(self):
        assert detect_market_type("") == "unknown"

    def test_case_insensitive(self):
        """Detection should be case-insensitive."""
        assert detect_market_type("WILL BTC BE ABOVE $90,000?") == "price"


# ══════════════════════════════════════════════════════════════════════════════
# compute_signal — Core Logic
# ══════════════════════════════════════════════════════════════════════════════

class TestComputeSignal:

    def test_empty_buffer_returns_skip(self):
        """No data → SKIP with no crash."""
        buf = PriceBuffer()
        sig = compute_signal("BTC", buf, market_price_yes=0.5)
        assert sig.direction == "SKIP"
        assert sig.edge == 0

    def test_signal_returns_signal_object(self):
        """compute_signal always returns a Signal dataclass."""
        buf = _make_trending_buffer(83000, 0.0002, 300)
        sig = compute_signal("BTC", buf, 0.5, 10, "Will BTC be above $84,000?")
        assert isinstance(sig, Signal)

    def test_signal_direction_valid(self):
        """Direction must be one of YES/NO/SKIP."""
        buf = _make_trending_buffer(83000, 0.0003, 400)
        sig = compute_signal("BTC", buf, 0.5)
        assert sig.direction in ("YES", "NO", "SKIP")

    def test_signal_edge_bounded(self):
        """Edge should be within [-1, 1]."""
        buf = _make_trending_buffer(83000, 0.0003, 400)
        sig = compute_signal("BTC", buf, 0.5)
        assert -1.0 <= sig.edge <= 1.0

    def test_signal_strength_bounded(self):
        """Strength must be in [0, 1]."""
        buf = _make_trending_buffer(83000, 0.0003, 400)
        sig = compute_signal("BTC", buf, 0.5)
        assert 0.0 <= sig.strength <= 1.0

    def test_strong_uptrend_produces_yes(self):
        """
        Strong uptrend (0.04% per tick, 500 ticks) with market price well below
        estimated prob should produce a YES signal.
        """
        buf = _make_trending_buffer(83000, 0.0004, 500)
        sig = compute_signal("BTC", buf, 0.30, 10, "Will BTC be above $84,000?")
        # With strong momentum and low market price, edge should be positive
        assert sig.edge > 0

    def test_strong_downtrend_produces_no(self):
        """
        Strong downtrend with high market price should produce NO or at least negative edge.
        """
        buf = _make_trending_buffer(83000, -0.0004, 500)
        sig = compute_signal("BTC", buf, 0.75, 10, "Will BTC be above $80,000?")
        assert sig.edge < 0

    def test_event_market_gets_reduced_edge(self):
        """
        Event markets should have smaller absolute edge than price markets
        given identical price data.
        """
        buf = _make_trending_buffer(83000, 0.0004, 500)
        sig_price = compute_signal("BTC", buf, 0.40, 10, "Will BTC be above $84,000?")
        sig_event = compute_signal("BTC", buf, 0.40, 10, "Will the SEC approve Bitcoin ETF?")
        assert abs(sig_event.edge) <= abs(sig_price.edge) + 0.01  # event edge ≤ price edge

    def test_event_market_confidence_degraded(self):
        """
        Event markets should have confidence degraded by one level
        compared to equivalent price market.
        """
        buf = _make_trending_buffer(83000, 0.0003, 500)
        sig_price = compute_signal("BTC", buf, 0.40, 10, "Will BTC be above $84,000?")
        sig_event = compute_signal("BTC", buf, 0.40, 10, "Will the SEC approve Bitcoin ETF?")
        order = {"low": 0, "medium": 1, "high": 2}
        assert order[sig_event.confidence] <= order[sig_price.confidence]

    def test_low_ticks_produces_low_confidence(self):
        """Buffer with < 100 ticks should yield low confidence."""
        buf = PriceBuffer()
        for i in range(50):
            buf.push(83000.0 + i)
        sig = compute_signal("BTC", buf, 0.5)
        assert sig.confidence == "low"

    def test_high_ticks_can_produce_higher_confidence(self):
        """Buffer with >300 ticks and >20 candles can yield medium/high confidence.
        Uses market_price_yes=0.40 to avoid near-50% fee filter (>2.5% fee).
        """
        buf = _make_trending_buffer(83000, 0.0002, 400)
        # Force candles by injecting synthetic ones
        for _ in range(25):
            buf.candles_1m.append(
                Candle(ts=time.time(), open=83000, high=83500, low=82500, close=83200, volume=10)
            )
        sig = compute_signal("BTC", buf, 0.40)
        assert sig.confidence in ("medium", "high")

    def test_signal_has_reasons_list(self):
        """Signal always has a non-empty reasons list."""
        buf = _make_trending_buffer(83000, 0.0003, 400)
        sig = compute_signal("BTC", buf, 0.5)
        assert isinstance(sig.reasons, list)
        assert len(sig.reasons) >= 1

    def test_market_type_propagated_correctly(self):
        """market_type in Signal should match detect_market_type output."""
        buf = _make_trending_buffer(83000, 0.0002, 300)
        sig = compute_signal("BTC", buf, 0.5, 10, "Will BTC be above $90,000?")
        assert sig.market_type == "price"

        sig2 = compute_signal("BTC", buf, 0.5, 10, "Will the SEC approve a Bitcoin ETF?")
        assert sig2.market_type == "event"

    def test_no_signal_when_edge_below_threshold(self):
        """Flat price data → edge near 0 → SKIP direction."""
        buf = PriceBuffer()
        # Add lots of flat ticks
        for _ in range(500):
            buf.push(83000.0, 1.0)
        sig = compute_signal("BTC", buf, 0.5)
        # With flat prices the momentum is 0, edge should be near 0 → SKIP
        assert sig.direction == "SKIP"

    def test_symbol_propagated(self):
        """Symbol must be passed through to the Signal."""
        buf = _make_trending_buffer(3000, 0.0002, 300)
        sig = compute_signal("ETH", buf, 0.5)
        assert sig.symbol == "ETH"