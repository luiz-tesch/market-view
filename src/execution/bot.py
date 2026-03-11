"""
Autonomous Bot Engine
- Polls Polymarket for active 5-15min crypto markets
- Scores each with the signal engine
- Paper-trades autonomously every 30s
- Tracks full P&L + history
"""
from __future__ import annotations
import json
import math
import random
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Literal

import requests

from src.signals.engine import PriceBuffer, Signal, compute_signal

GAMMA_URL = "https://gamma-api.polymarket.com/markets"
CRYPTO_KEYWORDS = ["BTC", "ETH", "SOL", "bitcoin", "ethereum", "solana",
                   "crypto", "MATIC", "BNB", "DOGE", "XRP"]

# Target option horizons (minutes)
MIN_HORIZON = 3
MAX_HORIZON = 20


# ─── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class Position:
    id: str
    symbol: str
    question: str
    market_slug: str
    side: Literal["YES", "NO"]
    amount: float
    entry_price: float          # Polymarket implied prob
    entry_ts: float
    horizon_minutes: int
    signal_edge: float
    signal_reasons: list[str]
    resolved: bool = False
    outcome: str | None = None
    pnl: float | None = None
    close_ts: float | None = None
    close_price: float | None = None

    def to_dict(self):
        return asdict(self)


@dataclass
class BotStats:
    total_bets: int = 0
    wins: int = 0
    losses: int = 0
    total_wagered: float = 0.0
    total_pnl: float = 0.0
    peak_balance: float = 100.0
    max_drawdown: float = 0.0
    start_time: float = field(default_factory=time.time)
    last_scan_ts: float = 0.0
    last_bet_ts: float = 0.0
    markets_scanned: int = 0
    signals_fired: int = 0
    signals_skipped: int = 0


# ─── Bot ───────────────────────────────────────────────────────────────────────

class CryptoBot:
    STARTING_BALANCE = 100.0
    MAX_BET_PCT = 0.05        # 5% of balance per bet
    MAX_OPEN_POSITIONS = 5
    SCAN_INTERVAL = 30        # seconds between market scans
    KELLY_FRACTION = 0.25     # fractional Kelly

    def __init__(self, buffers: dict[str, PriceBuffer]):
        self.buffers = buffers  # symbol → PriceBuffer
        self.balance = self.STARTING_BALANCE
        self.positions: list[Position] = []
        self.closed_positions: list[Position] = []
        self.stats = BotStats(peak_balance=self.STARTING_BALANCE)
        self._lock = threading.Lock()
        self._running = False
        self._thread: threading.Thread | None = None
        self.log: list[dict] = []          # event log
        self._markets_cache: list[dict] = []
        self._markets_cache_ts: float = 0

    # ── Public API ─────────────────────────────────────────────────────────────

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        self._log("BOT_START", "Autonomous bot started")

    def stop(self):
        self._running = False
        self._log("BOT_STOP", "Bot stopped")

    def state_snapshot(self) -> dict:
        with self._lock:
            open_pos = [p.to_dict() for p in self.positions if not p.resolved]
            closed_pos = [p.to_dict() for p in self.closed_positions[-50:]]
            return {
                "balance": self.balance,
                "stats": asdict(self.stats),
                "open_positions": open_pos,
                "closed_positions": closed_pos,
                "log": self.log[-100:],
                "buffers": {
                    sym: {
                        "price": buf.latest_price(),
                        "ticks": len(buf.ticks),
                        "candles": len(buf.candles_1m),
                        "mom_1m": self._safe_mom(buf, 60),
                        "mom_5m": self._safe_mom(buf, 300),
                    }
                    for sym, buf in self.buffers.items()
                },
                "running": self._running,
            }

    # ── Internal loop ──────────────────────────────────────────────────────────

    def _loop(self):
        while self._running:
            try:
                self._resolve_expired()
                self._scan_and_trade()
                self._update_drawdown()
            except Exception as e:
                self._log("ERROR", str(e))
            time.sleep(self.SCAN_INTERVAL)

    def _scan_and_trade(self):
        markets = self._fetch_crypto_markets()
        self.stats.last_scan_ts = time.time()
        self.stats.markets_scanned += len(markets)

        open_count = sum(1 for p in self.positions if not p.resolved)
        if open_count >= self.MAX_OPEN_POSITIONS:
            self._log("SKIP_SCAN", f"Max open positions ({open_count}) reached")
            return

        # Randomise order to avoid always betting the same markets
        random.shuffle(markets)
        for market in markets[:20]:
            if not self._running:
                break
            self._evaluate_market(market)

    def _evaluate_market(self, market: dict):
        question = market.get("question", "")
        slug = market.get("slug") or market.get("id", "")

        # Don't double-bet the same market
        if any(p.market_slug == slug and not p.resolved for p in self.positions):
            return

        # Detect which symbol this market is about
        symbol = self._detect_symbol(question)
        if not symbol:
            return

        buf = self.buffers.get(symbol)
        if not buf or len(buf.ticks) < 20:
            return

        # Get current Polymarket price
        try:
            import json as _json
            prices = _json.loads(market.get("outcomePrices", "[0.5,0.5]"))
            market_price_yes = float(prices[0])
        except Exception:
            market_price_yes = 0.5

        # Estimate horizon
        horizon = self._estimate_horizon(market)
        if horizon is None:
            return

        # Compute signal
        sig = compute_signal(symbol, buf, market_price_yes, horizon)
        self.stats.last_scan_ts = time.time()

        if sig.direction == "SKIP":
            self.stats.signals_skipped += 1
            return

        self.stats.signals_fired += 1
        self._place_bet(market, symbol, sig, horizon)

    def _place_bet(self, market: dict, symbol: str, sig: Signal, horizon: int):
        with self._lock:
            # Kelly sizing
            edge = abs(sig.edge)
            p = sig.edge + (1 - sig.edge) if sig.direction == "YES" else (1 - abs(sig.edge))
            kelly = (edge * p - (1 - edge) * (1 - p)) / (1 - p) if (1 - p) > 0 else 0
            kelly = max(0, min(kelly, self.KELLY_FRACTION))

            conf_mult = {"high": 1.0, "medium": 0.6, "low": 0.3}[sig.confidence]
            max_bet = self.balance * self.MAX_BET_PCT
            amount = round(min(kelly * max_bet * conf_mult, max_bet, self.balance * 0.08), 2)
            amount = max(amount, 0.25)  # floor

            if amount > self.balance:
                return

            side: Literal["YES", "NO"] = sig.direction  # type: ignore
            entry_price = sig.edge + 0.5 if side == "YES" else 0.5 - abs(sig.edge)
            entry_price = max(0.05, min(0.95, entry_price))

            slug = market.get("slug") or market.get("id", "")
            pos = Position(
                id=f"pos_{int(time.time()*1000)}",
                symbol=symbol,
                question=market.get("question", "?")[:80],
                market_slug=slug,
                side=side,
                amount=amount,
                entry_price=entry_price,
                entry_ts=time.time(),
                horizon_minutes=horizon,
                signal_edge=sig.edge,
                signal_reasons=sig.reasons,
            )
            self.balance -= amount
            self.positions.append(pos)
            self.stats.total_bets += 1
            self.stats.total_wagered += amount
            self.stats.last_bet_ts = time.time()

            self._log("BET_PLACED", (
                f"{side} ${amount:.2f} on {symbol} | "
                f"edge {sig.edge:+.3f} | {sig.confidence} | "
                f"'{market.get('question','')[:50]}'"
            ), data={"pos_id": pos.id, "amount": amount, "side": side,
                     "edge": sig.edge, "symbol": symbol})

    def _resolve_expired(self):
        """Auto-resolve positions past their horizon using current price momentum as proxy."""
        now = time.time()
        for pos in list(self.positions):
            if pos.resolved:
                continue
            age_minutes = (now - pos.entry_ts) / 60
            if age_minutes < pos.horizon_minutes:
                continue

            # Simulate resolution: use momentum since entry as proxy for outcome
            buf = self.buffers.get(pos.symbol)
            won = self._simulate_resolution(pos, buf)
            self._close_position(pos, won)

    def _simulate_resolution(self, pos: Position, buf: PriceBuffer | None) -> bool:
        """
        Determine win/loss using price action since entry.
        If YES and price went up → win; if NO and price went down → win.
        Falls back to probabilistic resolution.
        """
        if buf is None:
            return random.random() < 0.48  # slight house edge

        price_now = buf.latest_price()
        if price_now is None:
            return random.random() < 0.48

        # Find price at entry time
        entry_ts = pos.entry_ts
        price_at_entry = None
        for ts, p in buf.ticks:
            if ts >= entry_ts:
                price_at_entry = p
                break

        if price_at_entry is None:
            price_at_entry = price_now  # fallback

        pct_move = (price_now / price_at_entry - 1) * 100

        if pos.side == "YES":
            # YES wins if price went up by threshold
            threshold = 0.05 + random.gauss(0, 0.03)
            return pct_move > threshold
        else:
            # NO wins if price went down
            threshold = 0.05 + random.gauss(0, 0.03)
            return pct_move < -threshold

    def _close_position(self, pos: Position, won: bool):
        with self._lock:
            pos.resolved = True
            pos.outcome = "YES" if won else "NO"
            pos.close_ts = time.time()

            if won:
                if pos.side == "YES":
                    payout = pos.amount / pos.entry_price
                else:
                    payout = pos.amount / (1 - pos.entry_price)
                pos.pnl = payout - pos.amount
                self.balance += payout
                self.stats.wins += 1
            else:
                pos.pnl = -pos.amount
                self.stats.losses += 1

            self.stats.total_pnl += pos.pnl
            self.positions = [p for p in self.positions if p.id != pos.id]
            self.closed_positions.append(pos)

            icon = "✓ WIN" if won else "✗ LOSS"
            self._log("BET_RESOLVED", (
                f"{icon} ${pos.pnl:+.2f} | {pos.symbol} {pos.side} | "
                f"balance now ${self.balance:.2f}"
            ), data={"won": won, "pnl": pos.pnl, "pos_id": pos.id})

    def _update_drawdown(self):
        total_value = self.balance + sum(
            p.amount for p in self.positions if not p.resolved
        )
        if total_value > self.stats.peak_balance:
            self.stats.peak_balance = total_value
        dd = (self.stats.peak_balance - total_value) / self.stats.peak_balance
        if dd > self.stats.max_drawdown:
            self.stats.max_drawdown = dd

    # ── Market fetching ────────────────────────────────────────────────────────

    def _fetch_crypto_markets(self) -> list[dict]:
        now = time.time()
        if now - self._markets_cache_ts < 120 and self._markets_cache:
            return self._markets_cache

        try:
            resp = requests.get(
                GAMMA_URL,
                params={"active": "true", "closed": "false", "limit": 100},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            markets = data if isinstance(data, list) else data.get("data", [])

            # Filter to crypto-related markets
            crypto_markets = [
                m for m in markets
                if any(kw.lower() in m.get("question", "").lower() for kw in CRYPTO_KEYWORDS)
            ]
            self._markets_cache = crypto_markets
            self._markets_cache_ts = now
            return crypto_markets
        except Exception as e:
            self._log("FETCH_ERROR", str(e))
            return self._markets_cache or []

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _detect_symbol(self, question: str) -> str | None:
        q = question.upper()
        if "BTC" in q or "BITCOIN" in q:
            return "BTC"
        if "ETH" in q or "ETHEREUM" in q:
            return "ETH"
        if "SOL" in q or "SOLANA" in q:
            return "SOL"
        if "MATIC" in q or "POLYGON" in q:
            return "MATIC"
        if "BNB" in q or "BINANCE" in q:
            return "BNB"
        if "DOGE" in q or "DOGECOIN" in q:
            return "DOGE"
        return None

    def _estimate_horizon(self, market: dict) -> int | None:
        """Return estimated minutes to resolution, or None if outside range."""
        end_iso = market.get("endDateIso") or market.get("end_date_iso")
        if not end_iso:
            return None
        try:
            from datetime import datetime, timezone
            end_dt = datetime.fromisoformat(end_iso.replace("Z", "+00:00"))
            now_dt = datetime.now(timezone.utc)
            minutes = (end_dt - now_dt).total_seconds() / 60
            if MIN_HORIZON <= minutes <= MAX_HORIZON:
                return int(minutes)
            return None
        except Exception:
            return None

    def _safe_mom(self, buf: PriceBuffer, seconds: int) -> float:
        p_now = buf.latest_price()
        p_then = buf.price_n_seconds_ago(seconds)
        if p_now and p_then and p_then != 0:
            return round((p_now / p_then - 1) * 100, 3)
        return 0.0

    def _log(self, event: str, message: str, data: dict | None = None):
        entry = {
            "ts": time.time(),
            "ts_str": time.strftime("%H:%M:%S"),
            "event": event,
            "message": message,
            "data": data or {},
        }
        self.log.append(entry)
        print(f"[Bot] [{entry['ts_str']}] {event}: {message}")