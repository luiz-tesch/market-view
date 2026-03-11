"""
src/feed/polymarket.py  v2
Correções:
- BinanceFeed: fix detecção de símbolo (aggTrade stream case-insensitive)
- msg_count duplicado removido do ClobFeed
- fetch_gamma_markets: filtro SHORT_TERM (3-120 min) separado
- enrich_prices: agora em background thread (não bloqueia startup)
- CLOB resolve: função nova para buscar resolução real de mercados
"""
from __future__ import annotations
import asyncio
import json
import math
import random
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

import requests

from src.config import SHORT_TERM_MAX_MINUTES, SHORT_TERM_MIN_MINUTES

# ── URLs ───────────────────────────────────────────────────────────────────────
GAMMA_URL  = "https://gamma-api.polymarket.com/markets"
CLOB_URL   = "https://clob.polymarket.com"
CLOB_WS    = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
BIN_WS     = "wss://stream.binance.com:9443/stream"

# FIX: mapa completo em lowercase para match com data["s"].lower()
SYMBOL_STREAMS: dict[str, str] = {
    "BTC":   "btcusdt",
    "ETH":   "ethusdt",
    "SOL":   "solusdt",
    "BNB":   "bnbusdt",
    "MATIC": "maticusdt",
    "DOGE":  "dogeusdt",
}
# Inverso: "btcusdt" → "BTC"  (usado no handler do Binance)
STREAM_TO_SYM: dict[str, str] = {v: k for k, v in SYMBOL_STREAMS.items()}

CRYPTO_KEYWORDS: dict[str, list[str]] = {
    "BTC":   ["bitcoin", "btc"],
    "ETH":   ["ethereum", "eth"],
    "SOL":   ["solana", "sol"],
    "BNB":   ["bnb", "binance coin"],
    "MATIC": ["matic", "polygon"],
    "DOGE":  ["dogecoin", "doge"],
    "XRP":   ["ripple", "xrp"],
}

POLYMARKET_FEE = 0.01   # 1% taxa maker/taker


# ── Dataclass ──────────────────────────────────────────────────────────────────

@dataclass
class PolyMarket:
    token_id:      str
    no_token_id:   str
    condition_id:  str           # para buscar resolução real
    question:      str
    slug:          str
    symbol:        str
    end_date_iso:  str
    yes_price:     float
    no_price:      float
    volume:        float
    liquidity:     float
    image:         str = ""
    description:   str = ""

    def mins_left(self) -> float:
        try:
            from datetime import datetime, timezone
            end = datetime.fromisoformat(self.end_date_iso.replace("Z", "+00:00"))
            return (end - datetime.now(timezone.utc)).total_seconds() / 60
        except Exception:
            return 9999.0

    def is_short_term(self) -> bool:
        ml = self.mins_left()
        return SHORT_TERM_MIN_MINUTES <= ml <= SHORT_TERM_MAX_MINUTES

    def to_dict(self) -> dict:
        ml = self.mins_left()
        return {
            "token_id":     self.token_id,
            "no_token_id":  self.no_token_id,
            "condition_id": self.condition_id,
            "question":     self.question,
            "slug":         self.slug,
            "symbol":       self.symbol,
            "end_date_iso": self.end_date_iso,
            "mins_left":    round(ml, 1),
            "is_short":     self.is_short_term(),
            "yes_price":    round(self.yes_price, 4),
            "no_price":     round(self.no_price, 4),
            "volume":       round(self.volume, 2),
            "liquidity":    round(self.liquidity, 2),
            "image":        self.image,
        }


# ── Gamma API ──────────────────────────────────────────────────────────────────

def _detect_symbol(question: str) -> str | None:
    q = question.lower()
    for sym, kws in CRYPTO_KEYWORDS.items():
        if any(kw in q for kw in kws):
            return sym
    return None


def fetch_gamma_markets(
    limit: int = 300,
    min_liquidity: float = 50.0,
    max_days: float = 90.0,
) -> list[PolyMarket]:
    """Busca mercados cripto ativos. Retorna lista ordenada por volume."""
    try:
        r = requests.get(
            GAMMA_URL,
            params={
                "active": "true", "closed": "false",
                "limit": limit, "order": "volumeNum", "ascending": "false",
            },
            timeout=15,
        )
        r.raise_for_status()
        raw   = r.json()
        items = raw if isinstance(raw, list) else raw.get("data", [])
    except Exception as e:
        print(f"[Gamma] Fetch error: {e}")
        return []

    result: list[PolyMarket] = []
    for m in items:
        q   = m.get("question", "")
        sym = _detect_symbol(q)
        if not sym:
            continue

        try:
            tids = json.loads(m.get("clobTokenIds", "[]"))
            if len(tids) < 2:
                continue
        except Exception:
            continue

        try:
            prices = json.loads(m.get("outcomePrices", "[0.5,0.5]"))
            yes_p  = float(prices[0])
            no_p   = float(prices[1]) if len(prices) > 1 else 1 - yes_p
        except Exception:
            yes_p, no_p = 0.5, 0.5

        end_iso = m.get("endDateIso") or m.get("endDate", "")
        if end_iso and "T" not in end_iso:
            end_iso += "T23:59:00Z"

        try:
            from datetime import datetime, timezone
            end_dt    = datetime.fromisoformat(end_iso.replace("Z", "+00:00"))
            mins_left = (end_dt - datetime.now(timezone.utc)).total_seconds() / 60
            if mins_left < 0 or mins_left > max_days * 1440:
                continue
        except Exception:
            pass

        liq = float(m.get("liquidityNum") or m.get("liquidity") or 0)
        if liq < min_liquidity:
            continue

        vol          = float(m.get("volumeNum") or m.get("volume") or 0)
        condition_id = m.get("conditionId") or m.get("condition_id") or ""

        result.append(PolyMarket(
            token_id     = tids[0],
            no_token_id  = tids[1],
            condition_id = condition_id,
            question     = q,
            slug         = m.get("slug", ""),
            symbol       = sym,
            end_date_iso = end_iso,
            yes_price    = yes_p,
            no_price     = no_p,
            volume       = vol,
            liquidity    = liq,
            image        = m.get("image", "") or m.get("icon", "") or "",
            description  = (m.get("description", "") or "")[:200],
        ))

    print(f"[Gamma] {len(items)} total → {len(result)} crypto markets")
    return result


def fetch_short_term_markets(min_liquidity: float = 100.0) -> list[PolyMarket]:
    """
    Busca APENAS mercados de curto prazo (3–120 min).
    Liquidez mínima maior para garantir spreads decentes.
    """
    all_markets  = fetch_gamma_markets(limit=500, min_liquidity=min_liquidity, max_days=1)
    short        = [m for m in all_markets if m.is_short_term()]
    print(f"[Gamma] Short-term ({SHORT_TERM_MIN_MINUTES}-{SHORT_TERM_MAX_MINUTES}min): {len(short)} mercados")
    return short


# ── CLOB REST ──────────────────────────────────────────────────────────────────

def clob_midpoint(token_id: str) -> float | None:
    try:
        r = requests.get(f"{CLOB_URL}/midpoint", params={"token_id": token_id}, timeout=5)
        if r.status_code == 200:
            mid = r.json().get("mid")
            if mid is not None:
                return float(mid)
    except Exception:
        pass
    return None


def clob_book(token_id: str) -> dict | None:
    try:
        r = requests.get(f"{CLOB_URL}/book", params={"token_id": token_id}, timeout=5)
        if r.status_code == 200:
            d    = r.json()
            bids = d.get("bids", [])
            asks = d.get("asks", [])
            bid  = max((float(b["price"]) for b in bids), default=0.0) if bids else 0.0
            ask  = min((float(a["price"]) for a in asks), default=1.0) if asks else 1.0
            return {"bid": bid, "ask": ask, "mid": (bid + ask) / 2, "spread": ask - bid}
    except Exception:
        pass
    return None


def fetch_market_resolution(condition_id: str) -> bool | None:
    """
    Busca se um mercado já resolveu e qual foi o resultado (YES=True / NO=False).
    Retorna None se ainda não resolveu ou não encontrou.
    
    Usa o endpoint /markets do CLOB filtrado por condition_id.
    """
    if not condition_id:
        return None
    try:
        r = requests.get(
            f"{CLOB_URL}/markets/{condition_id}",
            timeout=8,
        )
        if r.status_code != 200:
            return None
        data = r.json()
        # Campo "resolved" + "outcome" no CLOB response
        if data.get("resolved") or data.get("closed"):
            outcome = data.get("outcome", "")
            if isinstance(outcome, str):
                return outcome.upper() in ("YES", "1", "TRUE", "WIN")
            if isinstance(outcome, (int, float)):
                return float(outcome) > 0.5
        return None
    except Exception:
        return None


def enrich_prices_background(markets: list[PolyMarket]) -> None:
    """
    Atualiza yes_price dos mercados via CLOB REST em background thread.
    NÃO bloqueia o startup do servidor.
    """
    def _worker():
        for m in markets[:30]:
            try:
                mid = clob_midpoint(m.token_id)
                if mid is not None:
                    m.yes_price = mid
                    m.no_price  = 1 - mid
                time.sleep(0.05)
            except Exception:
                pass
        print("[CLOB] Preços enriquecidos em background")

    threading.Thread(target=_worker, daemon=True, name="EnrichPrices").start()


# ── CLOB WebSocket ─────────────────────────────────────────────────────────────

class ClobFeed:
    """
    WebSocket CLOB do Polymarket — updates de preço em tempo real.
    FIX v2: msg_count não duplicado; subscriptions em batch de 50.
    """

    def __init__(
        self,
        token_ids: list[str],
        on_update: Callable[[str, float, float], None],
    ):
        self.token_ids = token_ids
        self.on_update = on_update
        self._running  = False
        self._thread:  threading.Thread | None = None
        self._loop:    asyncio.AbstractEventLoop | None = None
        self.connected = False
        self.msg_count = 0          # FIX: incrementado apenas 1x por mensagem
        self.last_ts   = 0.0
        self.latencies: list[float] = []
        self._book:    dict[str, dict] = {}

    def start(self):
        self._running = True
        self._thread  = threading.Thread(target=self._run, daemon=True, name="ClobWS")
        self._thread.start()

    def stop(self):
        self._running = False

    def avg_latency_ms(self) -> float:
        s = self.latencies[-100:]
        return sum(s) / len(s) if s else 0.0

    def _run(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._stream())

    async def _stream(self):
        while self._running:
            try:
                import websockets
                async with websockets.connect(
                    CLOB_WS, ping_interval=20, ping_timeout=15, open_timeout=10,
                ) as ws:
                    for i in range(0, len(self.token_ids), 50):
                        batch = self.token_ids[i:i + 50]
                        await ws.send(json.dumps({
                            "auth": {}, "markets": [],
                            "assets_ids": batch, "type": "market",
                        }))
                    self.connected = True
                    print(f"[ClobWS] Connected — {len(self.token_ids)} tokens")

                    async for raw in ws:
                        if not self._running:
                            break
                        recv_ts = time.time()
                        try:
                            self._handle(raw, recv_ts)
                        except Exception:
                            pass
            except Exception as e:
                self.connected = False
                print(f"[ClobWS] {e} — retry 5s")
                await asyncio.sleep(5)

    def _handle(self, raw: str, recv_ts: float):
        msgs = json.loads(raw)
        if not isinstance(msgs, list):
            msgs = [msgs]

        for msg in msgs:
            asset_id = msg.get("asset_id", "")
            if not asset_id or asset_id not in self.token_ids:
                continue

            mtype = msg.get("event_type") or msg.get("type", "")
            bid = ask = None

            if mtype == "book":
                bids = msg.get("bids", [])
                asks = msg.get("asks", [])
                if bids:
                    bid = max(float(b["price"]) for b in bids)
                if asks:
                    ask = min(float(a["price"]) for a in asks)
            elif mtype in ("price_change", "last_trade_price"):
                p   = float(msg.get("price", 0))
                bid = p - 0.005
                ask = p + 0.005
            else:
                if "bids" in msg and "asks" in msg:
                    bids = msg["bids"]
                    asks = msg["asks"]
                    if bids:
                        bid = max(float(b["price"]) for b in bids)
                    if asks:
                        ask = min(float(a["price"]) for a in asks)

            prev = self._book.get(asset_id, {})
            if bid is None:
                bid = prev.get("bid")
            if ask is None:
                ask = prev.get("ask")
            if bid is None or ask is None:
                continue

            bid = max(0.01, min(0.99, bid))
            ask = max(0.01, min(0.99, ask))

            self._book[asset_id] = {"bid": bid, "ask": ask}
            self.msg_count += 1      # FIX: incremento único
            self.last_ts    = recv_ts

            ts_raw = msg.get("timestamp") or msg.get("ts")
            if ts_raw:
                try:
                    s    = float(ts_raw)
                    sent = s / 1000 if s > 1e10 else s
                    lat  = (recv_ts - sent) * 1000
                    if 0 < lat < 5000:
                        self.latencies.append(lat)
                        if len(self.latencies) > 200:
                            self.latencies.pop(0)
                except Exception:
                    pass

            self.on_update(asset_id, bid, ask)


# ── Binance WebSocket ──────────────────────────────────────────────────────────

class BinanceFeed:
    """
    aggTrade stream do Binance.
    FIX v2: detecção de símbolo corrigida (case-insensitive, sem @aggTrade).
    """

    def __init__(self, symbols: list[str], on_tick: Callable[[str, float, float], None]):
        self.symbols   = [s for s in symbols if s in SYMBOL_STREAMS]
        self.on_tick   = on_tick
        self._running  = False
        self._thread:  threading.Thread | None = None
        self.connected = False
        self.last_prices: dict[str, float] = {}
        self.tick_count = 0

    def start(self):
        self._running = True
        self._thread  = threading.Thread(target=self._run, daemon=True, name="BinanceWS")
        self._thread.start()

    def stop(self):
        self._running = False

    def _run(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(self._stream())

    async def _stream(self):
        streams = "/".join(
            f"{SYMBOL_STREAMS[s]}@aggTrade" for s in self.symbols
        )
        url = f"{BIN_WS}?streams={streams}"
        while self._running:
            try:
                import websockets
                async with websockets.connect(url, ping_interval=20) as ws:
                    self.connected = True
                    print(f"[BinanceWS] Connected — {self.symbols}")
                    async for raw in ws:
                        if not self._running:
                            break
                        data = json.loads(raw).get("data", {})
                        if data.get("e") == "aggTrade":
                            # FIX: data["s"] = "BTCUSDT" → lowercase → "btcusdt" → lookup
                            bin_sym = data["s"].lower()  # "btcusdt"
                            sym = STREAM_TO_SYM.get(bin_sym)
                            if sym and sym in self.symbols:
                                p = float(data["p"])
                                q = float(data["q"])
                                self.last_prices[sym] = p
                                self.tick_count += 1
                                self.on_tick(sym, p, q)
            except Exception as e:
                self.connected = False
                print(f"[BinanceWS] {e} — retry 3s")
                await asyncio.sleep(3)


# ── Mock Binance ───────────────────────────────────────────────────────────────

class MockBinanceFeed:
    """Mock Binance — random walk com preços realistas."""
    BASE = {"BTC": 83000., "ETH": 3200., "SOL": 145., "BNB": 580., "MATIC": 0.85, "DOGE": 0.13}
    VOL  = {"BTC": 0.0005, "ETH": 0.0009, "SOL": 0.0014, "BNB": 0.0008, "MATIC": 0.002, "DOGE": 0.002}

    def __init__(self, symbols: list[str], on_tick: Callable[[str, float, float], None]):
        self.symbols    = symbols
        self.on_tick    = on_tick
        self._prices    = {s: self.BASE.get(s, 100.) for s in symbols}
        self._running   = False
        self.connected  = False
        self.last_prices: dict[str, float] = {}
        self.tick_count = 0

    def start(self):
        self._running  = True
        self.connected = True
        self.last_prices = dict(self._prices)

        def _loop():
            t = 0
            while self._running:
                for s in self.symbols:
                    v = self.VOL.get(s, 0.001)
                    self._prices[s] *= 1 + random.gauss(0, v) + math.sin(t / 180) * v * 0.2
                    p = self._prices[s]
                    self.last_prices[s] = p
                    self.tick_count += 1
                    self.on_tick(s, p, random.uniform(0.01, 2.0))
                time.sleep(0.35)
                t += 1

        threading.Thread(target=_loop, daemon=True, name="MockBinance").start()
        print(f"[MockBinance] Started for {self.symbols}")

    def stop(self):
        self._running = False


# ── Mock CLOB ─────────────────────────────────────────────────────────────────

class MockClobFeed:
    """
    Mock CLOB — usa mercados REAIS da Gamma API, simula movimento de preço.
    Influenciado pelo spot Binance para mercados de preço.
    """

    def __init__(
        self,
        markets: list[PolyMarket],
        on_update: Callable[[str, float, float], None],
        price_buffers: dict | None = None,
    ):
        self.markets       = markets
        self.on_update     = on_update
        self.price_buffers = price_buffers or {}
        self._running      = False
        self.connected     = False
        self.msg_count     = 0
        self.last_ts       = 0.0
        self.latencies:    list[float] = []
        self._probs: dict[str, float]  = {m.token_id: m.yes_price for m in markets}

    def start(self):
        self._running  = True
        self.connected = True
        threading.Thread(target=self._loop, daemon=True, name="MockClob").start()
        print(f"[MockClob] Simulating {len(self.markets)} real Polymarket markets")

    def stop(self):
        self._running = False

    def avg_latency_ms(self) -> float:
        s = self.latencies[-50:]
        return sum(s) / len(s) if s else 0.0

    def _loop(self):
        t = 0
        while self._running:
            # Só processa mercados ainda válidos (não expirados)
            active = [m for m in self.markets if m.mins_left() > 0.5]
            for m in active:
                influence = 0.0
                buf = self.price_buffers.get(m.symbol)
                if buf:
                    p_now = buf.latest_price()
                    p_ago = buf.price_n_seconds_ago(60)
                    if p_now and p_ago and p_ago != 0:
                        pct       = (p_now / p_ago - 1) * 100
                        influence = pct * 0.08

                noise = random.gauss(0, 0.004)
                drift = math.sin(t / 100 + hash(m.slug[:8]) % 30) * 0.001
                new_p = self._probs.get(m.token_id, m.yes_price) + influence * 0.1 + drift + noise
                new_p = max(0.02, min(0.98, new_p))
                self._probs[m.token_id] = new_p

                spread = random.uniform(0.01, 0.03)
                bid    = max(0.01, new_p - spread / 2)
                ask    = min(0.99, new_p + spread / 2)

                self.msg_count += 1
                self.last_ts    = time.time()
                lat = random.uniform(3, 25)
                self.latencies.append(lat)
                if len(self.latencies) > 100:
                    self.latencies.pop(0)

                self.on_update(m.token_id, bid, ask)

            time.sleep(0.8)
            t += 1