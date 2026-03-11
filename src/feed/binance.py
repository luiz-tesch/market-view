"""
Binance WebSocket price feed — streams real-time tick data
Symbols: BTCUSDT, ETHUSDT, SOLUSDT, etc.
"""
from __future__ import annotations
import asyncio
import json
import threading
import time
from typing import Callable
import websockets


BINANCE_WS = "wss://stream.binance.com:9443/stream"

# Map from our symbol keys to Binance stream names
SYMBOL_MAP = {
    "BTC": "btcusdt",
    "ETH": "ethusdt",
    "SOL": "solusdt",
    "BNB": "bnbusdt",
    "MATIC": "maticusdt",
    "DOGE": "dogeusdt",
}


class BinanceFeed:
    """
    Async WebSocket feed. Calls on_tick(symbol, price, volume) on each trade.
    Runs in a background thread so Flask can stay synchronous.
    """

    def __init__(self, symbols: list[str], on_tick: Callable[[str, float, float], None]):
        self.symbols = symbols
        self.on_tick = on_tick
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._running = False
        self.connected = False
        self.last_prices: dict[str, float] = {}
        self.tick_counts: dict[str, int] = {s: 0 for s in symbols}

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        if self._loop:
            self._loop.call_soon_threadsafe(self._loop.stop)

    def _run_loop(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._stream())

    async def _stream(self):
        streams = "/".join(
            f"{SYMBOL_MAP[s]}@aggTrade"
            for s in self.symbols
            if s in SYMBOL_MAP
        )
        url = f"{BINANCE_WS}?streams={streams}"

        while self._running:
            try:
                async with websockets.connect(url, ping_interval=20, ping_timeout=10) as ws:
                    self.connected = True
                    print(f"[BinanceFeed] Connected — streaming {self.symbols}")
                    async for raw in ws:
                        if not self._running:
                            break
                        try:
                            msg = json.loads(raw)
                            data = msg.get("data", {})
                            if data.get("e") == "aggTrade":
                                # Extract symbol: "BTCUSDT" → "BTC"
                                bin_sym = data["s"]  # e.g. "BTCUSDT"
                                sym = next(
                                    (k for k, v in SYMBOL_MAP.items()
                                     if v == bin_sym.lower()),
                                    None,
                                )
                                if sym and sym in self.symbols:
                                    price = float(data["p"])
                                    qty = float(data["q"])
                                    self.last_prices[sym] = price
                                    self.tick_counts[sym] += 1
                                    self.on_tick(sym, price, qty)
                        except Exception:
                            pass
            except Exception as e:
                self.connected = False
                print(f"[BinanceFeed] Disconnected: {e} — reconnecting in 3s")
                await asyncio.sleep(3)


class MockFeed:
    """
    Fallback mock feed for testing without network access.
    Generates realistic BTC/ETH price walks.
    """

    BASE_PRICES = {"BTC": 83000.0, "ETH": 3200.0, "SOL": 145.0}
    VOLATILITY = {"BTC": 0.0008, "ETH": 0.0012, "SOL": 0.0018}

    def __init__(self, symbols: list[str], on_tick: Callable[[str, float, float], None]):
        self.symbols = symbols
        self.on_tick = on_tick
        self._prices = {s: self.BASE_PRICES.get(s, 100.0) for s in symbols}
        self._running = False
        self._thread: threading.Thread | None = None
        self.connected = False
        self.last_prices: dict[str, float] = {}
        self.tick_counts: dict[str, int] = {s: 0 for s in symbols}

    def start(self):
        import random
        import math
        self._running = True
        self.connected = True
        self.last_prices = dict(self._prices)

        def run():
            import random, math
            t = 0
            while self._running:
                for sym in self.symbols:
                    vol = self.VOLATILITY.get(sym, 0.001)
                    # Brownian motion + small sine wave trend
                    drift = math.sin(t / 300) * vol * 0.3
                    shock = random.gauss(0, vol)
                    self._prices[sym] *= (1 + drift + shock)
                    price = self._prices[sym]
                    qty = random.uniform(0.01, 2.0)
                    self.last_prices[sym] = price
                    self.tick_counts[sym] += 1
                    self.on_tick(sym, price, qty)
                time.sleep(0.5)
                t += 1

        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()
        print(f"[MockFeed] Started mock price feed for {self.symbols}")

    def stop(self):
        self._running = False