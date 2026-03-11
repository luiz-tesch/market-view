"""
Polymarket CLOB WebSocket Client
Substitui polling REST (200-600ms delay) por stream em tempo real (~5-50ms)

Docs: https://docs.polymarket.com/#websocket-channels
Sem autenticação para leitura de preços públicos.
"""
from __future__ import annotations
import asyncio
import json
import threading
import time
from typing import Callable


CLOB_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/"

# Channels disponíveis sem auth
CHANNEL_MARKET = "market"       # order book updates, best bid/ask
CHANNEL_LIVE_ACTIVITY = "live_activity"  # trades em tempo real


class ClobPriceUpdate:
    __slots__ = ("token_id", "best_bid", "best_ask", "mid", "ts", "market_slug")

    def __init__(self, token_id: str, best_bid: float, best_ask: float, market_slug: str = ""):
        self.token_id = token_id
        self.best_bid = best_bid
        self.best_ask = best_ask
        self.mid = (best_bid + best_ask) / 2 if best_bid and best_ask else best_bid or best_ask
        self.ts = time.time()
        self.market_slug = market_slug


class ClobWebSocket:
    """
    Real-time Polymarket CLOB feed.
    Subscribes to YES token IDs and fires on_update(ClobPriceUpdate) on each change.

    Latency: ~5-50ms vs 200-600ms do REST polling.
    """

    def __init__(
        self,
        token_ids: list[str],
        on_update: Callable[[ClobPriceUpdate], None],
        token_slug_map: dict[str, str] | None = None,
    ):
        self.token_ids = token_ids
        self.on_update = on_update
        self.token_slug_map = token_slug_map or {}
        self._running = False
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self.connected = False
        self.last_prices: dict[str, ClobPriceUpdate] = {}
        self.message_count = 0
        self.last_message_ts = 0.0
        self.latencies: list[float] = []  # rolling last 100

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="ClobWS")
        self._thread.start()

    def stop(self):
        self._running = False
        if self._loop:
            self._loop.call_soon_threadsafe(self._loop.stop)

    def avg_latency_ms(self) -> float:
        if not self.latencies:
            return 0.0
        return sum(self.latencies) / len(self.latencies)

    def _run_loop(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._stream())

    async def _stream(self):
        while self._running:
            try:
                import websockets
                async with websockets.connect(
                    CLOB_WS + CHANNEL_MARKET,
                    ping_interval=20,
                    ping_timeout=15,
                    open_timeout=10,
                ) as ws:
                    # Subscribe to YES token IDs
                    sub_msg = json.dumps({
                        "auth": {},
                        "markets": [],
                        "assets_ids": self.token_ids,
                        "type": CHANNEL_MARKET,
                    })
                    await ws.send(sub_msg)
                    self.connected = True
                    print(f"[ClobWS] Connected — subscribed to {len(self.token_ids)} tokens")

                    async for raw in ws:
                        if not self._running:
                            break
                        recv_ts = time.time()
                        try:
                            self._handle_message(raw, recv_ts)
                        except Exception as e:
                            print(f"[ClobWS] Parse error: {e}")

            except Exception as e:
                self.connected = False
                print(f"[ClobWS] Disconnected: {e} — reconnecting in 2s")
                await asyncio.sleep(2)

    def _handle_message(self, raw: str, recv_ts: float):
        """
        CLOB market messages contain order book snapshots/diffs.
        Format varies — handle both snapshot and delta.
        """
        data = json.loads(raw)
        if not isinstance(data, list):
            data = [data]

        for msg in data:
            event_type = msg.get("event_type") or msg.get("type", "")
            asset_id = msg.get("asset_id") or msg.get("market", "")

            if not asset_id:
                continue

            # Extract best bid/ask from different message formats
            best_bid = 0.0
            best_ask = 0.0

            if "buys" in msg or "sells" in msg:
                buys = msg.get("buys", []) or []
                sells = msg.get("sells", []) or []
                if buys:
                    best_bid = max(float(b.get("price", 0)) for b in buys)
                if sells:
                    best_ask = min(float(s.get("price", 1)) for s in sells)

            elif "best_bid" in msg:
                best_bid = float(msg.get("best_bid", 0) or 0)
                best_ask = float(msg.get("best_ask", 1) or 1)

            elif "price" in msg:
                # Trade message — use as mid
                p = float(msg.get("price", 0.5))
                best_bid = p - 0.005
                best_ask = p + 0.005

            if best_ask == 0 and best_bid == 0:
                continue

            # Measure latency if timestamp available
            msg_ts = msg.get("timestamp") or msg.get("ts")
            if msg_ts:
                try:
                    sent_ts = float(msg_ts) / 1000 if float(msg_ts) > 1e10 else float(msg_ts)
                    lat = (recv_ts - sent_ts) * 1000
                    if 0 < lat < 5000:
                        self.latencies.append(lat)
                        if len(self.latencies) > 100:
                            self.latencies.pop(0)
                except Exception:
                    pass

            slug = self.token_slug_map.get(asset_id, asset_id[:16])
            update = ClobPriceUpdate(asset_id, best_bid, best_ask, slug)
            self.last_prices[asset_id] = update
            self.message_count += 1
            self.last_message_ts = recv_ts
            self.on_update(update)


class MockClobFeed:
    """
    Mock CLOB feed que simula order book realista com spread dinâmico.
    Usado quando sem chave Polymarket ou fora do ar.
    """

    MARKET_SCENARIOS = [
        {"slug": "btc-above-84k-dec10", "question": "BTC above $84,000 on Dec 10?", "symbol": "BTC",
         "base_prob": 0.48, "horizon_min": 8},
        {"slug": "eth-above-3200-dec10", "question": "ETH above $3,200 on Dec 10?", "symbol": "ETH",
         "base_prob": 0.52, "horizon_min": 12},
        {"slug": "btc-above-85k-dec10", "question": "BTC above $85,000 on Dec 10?", "symbol": "BTC",
         "base_prob": 0.35, "horizon_min": 6},
        {"slug": "sol-above-150-dec10", "question": "SOL above $150 on Dec 10?", "symbol": "SOL",
         "base_prob": 0.44, "horizon_min": 10},
        {"slug": "eth-below-3100-dec10", "question": "ETH below $3,100 on Dec 10?", "symbol": "ETH",
         "base_prob": 0.38, "horizon_min": 15},
        {"slug": "btc-high-83500-dec10", "question": "BTC reaches $83,500 in next 10min?", "symbol": "BTC",
         "base_prob": 0.55, "horizon_min": 10},
        {"slug": "sol-above-148-dec10", "question": "SOL above $148 in next 7min?", "symbol": "SOL",
         "base_prob": 0.61, "horizon_min": 7},
    ]

    def __init__(
        self,
        on_update: Callable[[ClobPriceUpdate], None],
        price_buffers: dict | None = None,
    ):
        self.on_update = on_update
        self.price_buffers = price_buffers or {}
        self._running = False
        self._thread: threading.Thread | None = None
        self.connected = False
        self.message_count = 0
        self.last_message_ts = 0.0
        self.latencies: list[float] = []
        self.markets = self._init_markets()
        self.last_prices: dict[str, ClobPriceUpdate] = {}

    def _init_markets(self) -> list[dict]:
        import random
        markets = []
        now = time.time()
        for i, m in enumerate(self.MARKET_SCENARIOS):
            token_id = f"mock_token_{i:04d}_{m['slug'][:8]}"
            markets.append({
                **m,
                "token_id": token_id,
                "current_prob": m["base_prob"] + random.uniform(-0.05, 0.05),
                "created_ts": now,
                "expire_ts": now + m["horizon_min"] * 60,
            })
        return markets

    def get_active_markets(self) -> list[dict]:
        """Returns markets list with token_id, question, symbol, horizon_min, etc."""
        now = time.time()
        active = []
        for m in self.markets:
            mins_left = (m["expire_ts"] - now) / 60
            if mins_left > 0.5:
                active.append({
                    **m,
                    "mins_left": round(mins_left, 1),
                    "yes_price": m["current_prob"],
                })
            else:
                # Expire and recycle
                m["expire_ts"] = now + m["horizon_min"] * 60
                m["current_prob"] = m["base_prob"] + __import__("random").uniform(-0.08, 0.08)
        return active

    def start(self):
        self._running = True
        self.connected = True
        self._thread = threading.Thread(target=self._run, daemon=True, name="MockClob")
        self._thread.start()
        print(f"[MockClob] Started {len(self.markets)} simulated markets")

    def stop(self):
        self._running = False

    def avg_latency_ms(self) -> float:
        return 2.5  # mock

    def _run(self):
        import random, math
        t = 0
        while self._running:
            for m in self.markets:
                # Price walk influenced by Binance price buffer if available
                sym = m["symbol"]
                buf = self.price_buffers.get(sym)
                influence = 0.0
                if buf:
                    p_now = buf.latest_price()
                    p_ago = buf.price_n_seconds_ago(30)
                    if p_now and p_ago and p_ago != 0:
                        pct = (p_now / p_ago - 1) * 100
                        influence = pct * 0.08  # market moves 8% as much as underlying

                # Random walk + influence
                drift = influence + math.sin(t / 60 + hash(m["slug"]) % 10) * 0.002
                shock = random.gauss(0, 0.008)
                new_prob = m["current_prob"] + drift * 0.1 + shock
                new_prob = max(0.03, min(0.97, new_prob))
                m["current_prob"] = new_prob

                spread = random.uniform(0.015, 0.04)
                bid = max(0.01, new_prob - spread / 2)
                ask = min(0.99, new_prob + spread / 2)

                update = ClobPriceUpdate(m["token_id"], bid, ask, m["slug"])
                self.last_prices[m["token_id"]] = update
                self.message_count += 1
                self.last_message_ts = time.time()
                sim_lat = random.uniform(2, 15)
                self.latencies.append(sim_lat)
                if len(self.latencies) > 100:
                    self.latencies.pop(0)
                self.on_update(update)

            time.sleep(0.8)
            t += 1