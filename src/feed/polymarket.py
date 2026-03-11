"""
src/feed/polymarket.py
Feeds 100% reais do Polymarket:
  - Gamma API: busca mercados cripto ativos (sem auth)
  - CLOB REST:  preço midpoint + order book por token_id (sem auth para leitura)
  - CLOB WS:   updates de preço em tempo real por asset_id (sem auth para leitura)
  - Binance WS: preço spot BTC/ETH/SOL em tempo real
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

# ── URLs ───────────────────────────────────────────────────────────────────────
GAMMA_URL   = "https://gamma-api.polymarket.com/markets"
CLOB_URL    = "https://clob.polymarket.com"
CLOB_WS     = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
BIN_WS      = "wss://stream.binance.com:9443/stream"

SYMBOL_STREAMS = {
    "BTC":  "btcusdt@aggTrade",
    "ETH":  "ethusdt@aggTrade",
    "SOL":  "solusdt@aggTrade",
    "BNB":  "bnbusdt@aggTrade",
    "MATIC":"maticusdt@aggTrade",
    "DOGE": "dogeusdt@aggTrade",
}

CRYPTO_KEYWORDS = {
    "BTC":   ["bitcoin","btc"],
    "ETH":   ["ethereum","eth"],
    "SOL":   ["solana","sol"],
    "BNB":   ["bnb","binance coin"],
    "MATIC": ["matic","polygon"],
    "DOGE":  ["dogecoin","doge"],
    "XRP":   ["ripple","xrp"],
}

# ── Dataclass ─────────────────────────────────────────────────────────────────

@dataclass
class PolyMarket:
    token_id:     str         # YES token id (usado no CLOB)
    no_token_id:  str         # NO  token id
    question:     str
    slug:         str
    symbol:       str         # BTC | ETH | SOL | ...
    end_date_iso: str         # "2025-03-15T20:00:00Z"
    yes_price:    float       # 0–1
    no_price:     float
    volume:       float
    liquidity:    float
    image:        str = ""
    description:  str = ""

    def mins_left(self) -> float:
        try:
            from datetime import datetime, timezone
            end = datetime.fromisoformat(self.end_date_iso.replace("Z", "+00:00"))
            return (end - datetime.now(timezone.utc)).total_seconds() / 60
        except Exception:
            return 9999.0

    def to_dict(self) -> dict:
        ml = self.mins_left()
        return {
            "token_id":     self.token_id,
            "no_token_id":  self.no_token_id,
            "question":     self.question,
            "slug":         self.slug,
            "symbol":       self.symbol,
            "end_date_iso": self.end_date_iso,
            "mins_left":    round(ml, 1),
            "yes_price":    round(self.yes_price, 4),
            "no_price":     round(self.no_price, 4),
            "volume":       round(self.volume, 2),
            "liquidity":    round(self.liquidity, 2),
            "image":        self.image,
        }


# ── Gamma API ─────────────────────────────────────────────────────────────────

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
    """
    Busca mercados cripto ativos via Gamma API.
    Retorna lista ordenada por volume decrescente.
    """
    try:
        r = requests.get(
            GAMMA_URL,
            params={"active":"true","closed":"false","limit":limit,"order":"volumeNum","ascending":"false"},
            timeout=15,
        )
        r.raise_for_status()
        raw = r.json()
        items = raw if isinstance(raw, list) else raw.get("data", [])
    except Exception as e:
        print(f"[Gamma] Fetch error: {e}")
        return []

    result: list[PolyMarket] = []
    for m in items:
        q   = m.get("question","")
        sym = _detect_symbol(q)
        if not sym:
            continue

        # clobTokenIds vem como JSON string
        try:
            tids = json.loads(m.get("clobTokenIds","[]"))
            if len(tids) < 2:
                continue
        except Exception:
            continue

        # outcomePrices também é JSON string
        try:
            prices = json.loads(m.get("outcomePrices","[0.5,0.5]"))
            yes_p  = float(prices[0])
            no_p   = float(prices[1]) if len(prices)>1 else 1-yes_p
        except Exception:
            yes_p, no_p = 0.5, 0.5

        # Data de fim
        end_iso = m.get("endDateIso") or m.get("endDate","")
        # endDateIso pode ser só "2025-03-15" sem horário
        if end_iso and "T" not in end_iso:
            end_iso = end_iso + "T23:59:00Z"

        # Filtro: não encerrado, liquidez mínima, horizonte máximo
        try:
            from datetime import datetime, timezone
            end_dt    = datetime.fromisoformat(end_iso.replace("Z","+00:00"))
            mins_left = (end_dt - datetime.now(timezone.utc)).total_seconds()/60
            if mins_left < 0 or mins_left > max_days*1440:
                continue
        except Exception:
            pass

        liq = float(m.get("liquidityNum") or m.get("liquidity") or 0)
        if liq < min_liquidity:
            continue

        vol = float(m.get("volumeNum") or m.get("volume") or 0)

        result.append(PolyMarket(
            token_id    = tids[0],
            no_token_id = tids[1],
            question    = q,
            slug        = m.get("slug",""),
            symbol      = sym,
            end_date_iso= end_iso,
            yes_price   = yes_p,
            no_price    = no_p,
            volume      = vol,
            liquidity   = liq,
            image       = m.get("image","") or m.get("icon","") or "",
            description = m.get("description","")[:200] if m.get("description") else "",
        ))

    print(f"[Gamma] {len(items)} total → {len(result)} crypto markets")
    return result


# ── CLOB REST ─────────────────────────────────────────────────────────────────

def clob_midpoint(token_id: str) -> float | None:
    """Preço midpoint via CLOB REST. Sem auth."""
    try:
        r = requests.get(f"{CLOB_URL}/midpoint", params={"token_id":token_id}, timeout=5)
        if r.status_code == 200:
            mid = r.json().get("mid")
            if mid is not None:
                return float(mid)
    except Exception:
        pass
    return None


def clob_book(token_id: str) -> dict | None:
    """Melhor bid/ask via CLOB REST. Sem auth."""
    try:
        r = requests.get(f"{CLOB_URL}/book", params={"token_id":token_id}, timeout=5)
        if r.status_code == 200:
            d    = r.json()
            bids = d.get("bids",[])
            asks = d.get("asks",[])
            bid  = max((float(b["price"]) for b in bids), default=0.0) if bids else 0.0
            ask  = min((float(a["price"]) for a in asks), default=1.0) if asks else 1.0
            return {"bid":bid,"ask":ask,"mid":(bid+ask)/2,"spread":ask-bid}
    except Exception:
        pass
    return None


def enrich_prices(markets: list[PolyMarket]) -> None:
    """Atualiza yes_price/no_price dos mercados via CLOB REST (em batch)."""
    for m in markets[:30]:   # só os 30 mais relevantes para não sobrecarregar
        mid = clob_midpoint(m.token_id)
        if mid is not None:
            m.yes_price = mid
            m.no_price  = 1 - mid
        time.sleep(0.05)      # 50ms entre chamadas — gentil com a API


# ── CLOB WebSocket ────────────────────────────────────────────────────────────

class ClobFeed:
    """
    WebSocket CLOB do Polymarket.
    Recebe book events (bids/asks) e last_trade_price para os token_ids inscritos.
    Sem autenticação para leitura pública de mercado.

    Formato de subscrição:
      {"auth": {}, "markets": [], "assets_ids": [...], "type": "market"}

    Mensagens recebidas (type="book"):
      {"asset_id":"...", "bids":[{"price":"0.45","size":"100"},...], "asks":[...], "timestamp":"..."}
    Mensagens recebidas (type="last_trade_price"):
      {"asset_id":"...", "price":"0.47", "size":"50", "side":"BUY", "timestamp":"..."}
    """

    def __init__(
        self,
        token_ids: list[str],
        on_update: Callable[[str, float, float], None],  # token_id, bid, ask
    ):
        self.token_ids   = token_ids
        self.on_update   = on_update
        self._running    = False
        self._thread:    threading.Thread | None = None
        self._loop:      asyncio.AbstractEventLoop | None = None
        self.connected   = False
        self.msg_count   = 0
        self.last_ts     = 0.0
        self.latencies:  list[float] = []
        self._book:      dict[str, dict] = {}  # token_id → {bid, ask}

    def start(self):
        self._running = True
        self._thread  = threading.Thread(target=self._run, daemon=True, name="ClobWS")
        self._thread.start()

    def stop(self):
        self._running = False

    def avg_latency_ms(self) -> float:
        s = self.latencies[-100:]
        return sum(s)/len(s) if s else 0.0

    def _run(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._stream())

    async def _stream(self):
        while self._running:
            try:
                import websockets
                async with websockets.connect(
                    CLOB_WS,
                    ping_interval=20,
                    ping_timeout=15,
                    open_timeout=10,
                ) as ws:
                    # Subscreve em batches de 50
                    for i in range(0, len(self.token_ids), 50):
                        batch = self.token_ids[i:i+50]
                        await ws.send(json.dumps({
                            "auth":      {},
                            "markets":   [],
                            "assets_ids":batch,
                            "type":      "market",
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
            asset_id = msg.get("asset_id","")
            if not asset_id or asset_id not in self.token_ids:
                continue

            mtype = msg.get("event_type") or msg.get("type","")
            bid = ask = None

            if mtype == "book":
                # Snapshot completo de bids/asks
                bids = msg.get("bids",[])
                asks = msg.get("asks",[])
                if bids:
                    bid = max(float(b["price"]) for b in bids)
                if asks:
                    ask = min(float(a["price"]) for a in asks)

            elif mtype == "price_change":
                # Delta update: price field é o novo midpoint
                p   = float(msg.get("price", 0))
                bid = p - 0.005
                ask = p + 0.005

            elif mtype == "last_trade_price":
                p   = float(msg.get("price", 0))
                bid = p - 0.003
                ask = p + 0.003

            else:
                # Tenta extrair qualquer campo de preço
                if "bids" in msg and "asks" in msg:
                    bids = msg["bids"]
                    asks = msg["asks"]
                    if bids: bid = max(float(b["price"]) for b in bids)
                    if asks: ask = min(float(a["price"]) for a in asks)

            if bid is None or ask is None:
                # Usa estado anterior + delta
                prev = self._book.get(asset_id)
                if prev and bid is None: bid = prev["bid"]
                if prev and ask is None: ask = prev["ask"]

            if bid is None or ask is None:
                continue

            bid = max(0.01, min(0.99, bid))
            ask = max(0.01, min(0.99, ask))

            self._book[asset_id] = {"bid":bid,"ask":ask}
            self.msg_count += 1
            self.last_ts    = recv_ts
            self.msg_count  += 1

            # Latência
            ts_raw = msg.get("timestamp") or msg.get("ts")
            if ts_raw:
                try:
                    s = float(ts_raw)
                    sent = s/1000 if s > 1e10 else s
                    lat  = (recv_ts - sent)*1000
                    if 0 < lat < 5000:
                        self.latencies.append(lat)
                        if len(self.latencies) > 200:
                            self.latencies.pop(0)
                except Exception:
                    pass

            self.on_update(asset_id, bid, ask)


# ── Binance WebSocket ─────────────────────────────────────────────────────────

class BinanceFeed:
    """aggTrade stream do Binance — preço spot em tempo real."""

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
        streams = "/".join(SYMBOL_STREAMS[s] for s in self.symbols)
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
                        data = json.loads(raw).get("data",{})
                        if data.get("e") == "aggTrade":
                            stream = data["s"].lower()  # "btcusdt"
                            sym = next((k for k,v in SYMBOL_STREAMS.items() if v.startswith(stream.replace("@aggtrade",""))),None)
                            if sym:
                                p = float(data["p"])
                                q = float(data["q"])
                                self.last_prices[sym] = p
                                self.tick_count += 1
                                self.on_tick(sym, p, q)
            except Exception as e:
                self.connected = False
                print(f"[BinanceWS] {e} — retry 3s")
                await asyncio.sleep(3)


class MockBinanceFeed:
    """Mock Binance — random walk baseado em preços reais como seed."""
    BASE = {"BTC":83000.,"ETH":3200.,"SOL":145.,"BNB":580.,"MATIC":0.85,"DOGE":0.13}
    VOL  = {"BTC":0.0005,"ETH":0.0009,"SOL":0.0014,"BNB":0.0008,"MATIC":0.002,"DOGE":0.002}

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
                    self._prices[s] *= 1 + random.gauss(0, v) + math.sin(t/180)*v*0.2
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


class MockClobFeed:
    """
    Mock CLOB — usa os mercados REAIS buscados da Gamma API,
    mas simula o movimento de preço.
    O preço de cada mercado é influenciado pelo preço spot Binance.
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
        # Seed: usa preços reais da Gamma como ponto de partida
        self._probs: dict[str, float] = {m.token_id: m.yes_price for m in markets}

    def start(self):
        self._running  = True
        self.connected = True
        threading.Thread(target=self._loop, daemon=True, name="MockClob").start()
        print(f"[MockClob] Simulating {len(self.markets)} real Polymarket markets")

    def stop(self):
        self._running = False

    def avg_latency_ms(self) -> float:
        s = self.latencies[-50:]
        return sum(s)/len(s) if s else 0.0

    def _loop(self):
        t = 0
        while self._running:
            for m in self.markets:
                # Influência spot (mercados de preço BTC/ETH/SOL)
                influence = 0.0
                buf = self.price_buffers.get(m.symbol)
                if buf:
                    p_now = buf.latest_price()
                    p_ago = buf.price_n_seconds_ago(60)
                    if p_now and p_ago and p_ago != 0:
                        pct = (p_now/p_ago - 1)*100
                        influence = pct * 0.08  # 8% de transmissão do spot para o mercado

                noise = random.gauss(0, 0.004)
                drift = math.sin(t/100 + hash(m.slug[:8])%30) * 0.001
                new_p = self._probs[m.token_id] + influence*0.1 + drift + noise
                new_p = max(0.02, min(0.98, new_p))
                self._probs[m.token_id] = new_p

                spread = random.uniform(0.01, 0.03)
                bid    = max(0.01, new_p - spread/2)
                ask    = min(0.99, new_p + spread/2)

                self.msg_count += 1
                self.last_ts    = time.time()
                lat = random.uniform(3, 25)
                self.latencies.append(lat)
                if len(self.latencies) > 100:
                    self.latencies.pop(0)

                self.on_update(m.token_id, bid, ask)

            time.sleep(0.8)
            t += 1