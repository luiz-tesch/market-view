"""
src/feed/polymarket.py  v2.3
Correções v2.3:
- fetch_gamma_markets: logging detalhado de cada filtro descartado
- Filtros muito menos restritivos (min_liquidity padrão: 10 → era 50)
- Aceita campo 'liquidity' além de 'liquidityNum' (API pode ter renomeado)
- Aceita campo 'volume' além de 'volumeNum'
- endDate fallback mais robusto — aceita formatos variados da API
- Adiciona keyword "pol" e "polygon" ao MATIC, "xrp" já estava ok
- is_short_term: aumenta tolerância para mercados de "hoje" (até 240min)
- fetch_short_term_markets: usa max_days=7 em vez de 1
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

SYMBOL_STREAMS: dict[str, str] = {
    "BTC":   "btcusdt",
    "ETH":   "ethusdt",
    "SOL":   "solusdt",
    "BNB":   "bnbusdt",
    "MATIC": "maticusdt",
    "DOGE":  "dogeusdt",
    "XRP":   "xrpusdt",
}
STREAM_TO_SYM: dict[str, str] = {v: k for k, v in SYMBOL_STREAMS.items()}

CRYPTO_KEYWORDS: dict[str, list[str]] = {
    "BTC":   ["bitcoin", "btc"],
    "ETH":   ["ethereum", "eth"],
    "SOL":   ["solana", "sol"],
    "BNB":   ["bnb", "binance coin", "binance"],
    "MATIC": ["matic", "polygon", "pol"],
    "DOGE":  ["dogecoin", "doge"],
    "XRP":   ["ripple", "xrp"],
}

POLYMARKET_FEE = 0.01   # 1% taxa maker/taker


# ── Dataclass ──────────────────────────────────────────────────────────────────

@dataclass
class PolyMarket:
    token_id:      str
    no_token_id:   str
    condition_id:  str
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

    def _horizon_from_title(self) -> float | None:
        import re
        q = self.question.lower()
        if "up or down" not in q:
            return None
        m = re.search(r"(\d{1,2}):(\d{2})(am|pm)-(\d{1,2}):(\d{2})(am|pm)", q)
        if m:
            h1, m1, p1, h2, m2, p2 = m.groups()
            h1, m1, h2, m2 = int(h1), int(m1), int(h2), int(m2)
            if p1 == "pm" and h1 != 12: h1 += 12
            if p2 == "pm" and h2 != 12: h2 += 12
            diff = (h2 * 60 + m2) - (h1 * 60 + m1)
            return float(diff) if diff > 0 else float(diff + 1440)
        if "5 min"  in q: return 5.0
        if "15 min" in q: return 15.0
        if "30 min" in q: return 30.0
        if "1 hour" in q or "60 min" in q: return 60.0
        return None

    def is_short_term(self) -> bool:
        horizon = self._horizon_from_title()
        if horizon is not None:
            return SHORT_TERM_MIN_MINUTES <= horizon <= SHORT_TERM_MAX_MINUTES
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


def _parse_end_date(m: dict) -> str | None:
    """
    Tenta extrair end_date do mercado em vários formatos que a API pode usar.
    Retorna ISO string ou None se não encontrar.
    """
    # Tenta campos em ordem de preferência
    for field in ("endDateIso", "endDate", "end_date_iso", "end_date", "expirationDate"):
        v = m.get(field)
        if v and isinstance(v, str) and len(v) >= 8:
            # Normaliza para ISO
            if "T" not in v:
                v = v + "T23:59:00Z"
            if not v.endswith("Z") and "+" not in v:
                v = v + "Z"
            return v
    return None


def _parse_liquidity(m: dict) -> float:
    """Tenta vários campos de liquidity que a API pode usar."""
    for field in ("liquidityNum", "liquidity", "liquidityClob", "liq"):
        v = m.get(field)
        if v is not None:
            try:
                return float(v)
            except (ValueError, TypeError):
                pass
    return 0.0


def _parse_volume(m: dict) -> float:
    """Tenta vários campos de volume que a API pode usar."""
    for field in ("volumeNum", "volume", "volume24h", "vol"):
        v = m.get(field)
        if v is not None:
            try:
                return float(v)
            except (ValueError, TypeError):
                pass
    return 0.0


def fetch_gamma_markets(
    limit: int = 300,
    min_liquidity: float = 10.0,   # era 50 — reduzido para não filtrar tudo
    max_days: float = 90.0,
    _debug: bool = True,           # loga quantos caem em cada filtro
) -> list[PolyMarket]:
    """Busca mercados cripto ativos. Retorna lista ordenada por volume."""

    # Contadores para debug
    counts = {
        "total_raw":     0,
        "no_symbol":     0,
        "no_token_ids":  0,
        "no_end_date":   0,
        "expired":       0,
        "too_far":       0,
        "low_liquidity": 0,
        "accepted":      0,
    }

    try:
        r = requests.get(
            GAMMA_URL,
            params={
                "active": "true", "closed": "false",
                "limit": limit, "order": "startDate", "ascending": "false",
            },
            timeout=15,
        )
        r.raise_for_status()
        raw   = r.json()
        items = raw if isinstance(raw, list) else raw.get("data", [])
    except Exception as e:
        print(f"[Gamma] Fetch error: {e}")
        return []

    counts["total_raw"] = len(items)
    result: list[PolyMarket] = []

    for m in items:
        q   = m.get("question", "")
        sym = _detect_symbol(q)
        if not sym:
            counts["no_symbol"] += 1
            continue

        # Token IDs
        try:
            tids_raw = m.get("clobTokenIds", "[]")
            tids = json.loads(tids_raw) if isinstance(tids_raw, str) else tids_raw
            if not isinstance(tids, list) or len(tids) < 2:
                counts["no_token_ids"] += 1
                continue
        except Exception:
            counts["no_token_ids"] += 1
            continue

        # End date
        end_iso = _parse_end_date(m)
        if not end_iso:
            counts["no_end_date"] += 1
            continue

        # Validar end date e filtrar por max_days
        try:
            from datetime import datetime, timezone
            end_dt    = datetime.fromisoformat(end_iso.replace("Z", "+00:00"))
            mins_left = (end_dt - datetime.now(timezone.utc)).total_seconds() / 60
            if mins_left < -60:   # mercados expirados há mais de 1h
                counts["expired"] += 1
                continue
            if mins_left > max_days * 1440:
                counts["too_far"] += 1
                continue
        except Exception:
            pass  # se falhou em parsear, inclui mesmo assim

        # Liquidity
        liq = _parse_liquidity(m)
        if liq < min_liquidity:
            counts["low_liquidity"] += 1
            continue

        # Prices
        try:
            prices = json.loads(m.get("outcomePrices", "[0.5,0.5]"))
            yes_p  = float(prices[0])
            no_p   = float(prices[1]) if len(prices) > 1 else 1 - yes_p
        except Exception:
            yes_p, no_p = 0.5, 0.5

        vol          = _parse_volume(m)
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
        counts["accepted"] += 1

    if _debug:
        total = counts["total_raw"]
        print(
            f"[Gamma] {total} raw → "
            f"{counts['no_symbol']} no_sym · "
            f"{counts['no_token_ids']} no_tids · "
            f"{counts['no_end_date']} no_date · "
            f"{counts['expired']} expired · "
            f"{counts['too_far']} too_far · "
            f"{counts['low_liquidity']} low_liq → "
            f"{counts['accepted']} accepted"
        )

    # Se zero aceitados, loga amostra para diagnóstico
    if counts["accepted"] == 0 and items:
        print("[Gamma] DEBUG — primeiros 3 itens da API:")
        for i, m in enumerate(items[:3]):
            print(f"  [{i}] question={m.get('question','')[:60]}")
            print(f"       liquidityNum={m.get('liquidityNum')} liquidity={m.get('liquidity')}")
            print(f"       endDateIso={m.get('endDateIso')} endDate={m.get('endDate')}")
            print(f"       clobTokenIds={str(m.get('clobTokenIds',''))[:60]}")
            print(f"       active={m.get('active')} closed={m.get('closed')}")

    return result


def fetch_short_term_markets(min_liquidity: float = 10.0) -> list[PolyMarket]:
    """Busca APENAS mercados de curto prazo (3–120 min)."""
    # max_days=7 em vez de 1 para não perder mercados que vencem nos próximos dias
    # mas cujo endDate está dentro do range short-term
    all_markets  = fetch_gamma_markets(limit=500, min_liquidity=min_liquidity, max_days=7)
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
    Busca se um mercado já resolveu e qual foi o resultado.
    Retorna True (YES venceu), False (NO venceu), ou None (não resolvido / erro).
    """
    if not condition_id:
        return None

    # ── FONTE 1: Gamma API ─────────────────────────────────────────────────────
    try:
        r = requests.get(
            GAMMA_URL,
            params={"conditionId": condition_id},
            timeout=8,
        )
        if r.status_code == 200:
            items = r.json()
            items = items if isinstance(items, list) else items.get("data", [])
            for m in items:
                if m.get("conditionId") != condition_id:
                    continue
                if m.get("active") and not m.get("closed"):
                    return None
                try:
                    prices = json.loads(m.get("outcomePrices", "[0.5,0.5]"))
                    yes_p  = float(prices[0])
                    no_p   = float(prices[1]) if len(prices) > 1 else 1 - yes_p
                    if yes_p >= 0.99:
                        return True
                    if no_p >= 0.99:
                        return False
                except Exception:
                    pass
                outcome = m.get("outcome", "")
                if isinstance(outcome, str) and outcome:
                    up = outcome.strip().upper()
                    if up in ("YES", "1", "TRUE", "WIN"):
                        return True
                    if up in ("NO", "0", "FALSE", "LOSS"):
                        return False
    except Exception as e:
        print(f"[Resolution] Gamma source failed for {condition_id[:12]}: {e}")

    # ── FONTE 2: CLOB /markets/{conditionId} ──────────────────────────────────
    try:
        r = requests.get(f"{CLOB_URL}/markets/{condition_id}", timeout=8)
        if r.status_code == 200:
            data = r.json()
            if data.get("resolved"):
                outcome = data.get("outcome", "")
                if isinstance(outcome, str) and outcome:
                    up = outcome.strip().upper()
                    if up in ("YES", "1", "TRUE"):
                        return True
                    if up in ("NO", "0", "FALSE"):
                        return False
            tokens = data.get("tokens", [])
            for token in tokens:
                if token.get("winner") is True:
                    outcome_name = token.get("outcome", "").upper()
                    return outcome_name in ("YES", "1")
    except Exception as e:
        print(f"[Resolution] CLOB source failed for {condition_id[:12]}: {e}")

    # ── FONTE 3: prices-history ────────────────────────────────────────────────
    try:
        r = requests.get(GAMMA_URL, params={"conditionId": condition_id}, timeout=5)
        if r.status_code == 200:
            items = r.json()
            items = items if isinstance(items, list) else items.get("data", [])
            for m in items:
                if m.get("conditionId") != condition_id:
                    continue
                tids_raw = m.get("clobTokenIds", "[]")
                tids = json.loads(tids_raw) if isinstance(tids_raw, str) else tids_raw
                if not tids:
                    break
                yes_token = tids[0]
                rh = requests.get(
                    f"{CLOB_URL}/prices-history",
                    params={"market": yes_token, "interval": "1m", "fidelity": 1},
                    timeout=6,
                )
                if rh.status_code == 200:
                    history = rh.json().get("history", [])
                    if history:
                        last_p = float(history[-1].get("p", 0.5))
                        if last_p >= 0.97:
                            return True
                        if last_p <= 0.03:
                            return False
                break
    except Exception:
        pass

    return None


def enrich_prices_background(markets: list[PolyMarket]) -> None:
    """
    Atualiza yes_price dos mercados via CLOB REST em background thread.
    Prioriza short-term markets.
    """
    def _worker():
        short_term = [m for m in markets if m.is_short_term()]
        others     = [m for m in markets if not m.is_short_term()]
        ordered    = short_term + others

        enriched = 0
        for m in ordered:
            try:
                mid = clob_midpoint(m.token_id)
                if mid is not None:
                    m.yes_price = mid
                    m.no_price  = 1 - mid
                    enriched += 1
                time.sleep(0.05)
            except Exception:
                pass

        print(f"[CLOB] {enriched}/{len(ordered)} preços enriquecidos em background "
              f"({len(short_term)} short-term prioritários)")

    threading.Thread(target=_worker, daemon=True, name="EnrichPrices").start()


# ── CLOB WebSocket ─────────────────────────────────────────────────────────────

class ClobFeed:
    def __init__(self, token_ids: list[str], on_update: Callable[[str, float, float], None]):
        self.token_ids = token_ids
        self.on_update = on_update
        self._running  = False
        self._thread:  threading.Thread | None = None
        self._loop:    asyncio.AbstractEventLoop | None = None
        self.connected = False
        self.msg_count = 0
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
            self.msg_count += 1
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
        streams = "/".join(f"{SYMBOL_STREAMS[s]}@aggTrade" for s in self.symbols)
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
                            bin_sym = data["s"].lower()
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
    BASE = {"BTC": 83000., "ETH": 3200., "SOL": 145., "BNB": 580.,
            "MATIC": 0.85, "DOGE": 0.13, "XRP": 0.60}
    VOL  = {"BTC": 0.0005, "ETH": 0.0009, "SOL": 0.0014, "BNB": 0.0008,
            "MATIC": 0.002, "DOGE": 0.002, "XRP": 0.0015}

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
    Simula preços do CLOB com realismo.

    Ao invés de random walk a partir de 0.50 (que nunca gera edge suficiente),
    calcula um preço justo baseado no preço spot do Binance vs o strike implícito
    do mercado, depois aplica ruído de mercado em cima.

    Resultado: preços variam entre 0.10–0.90 dependendo do contexto,
    gerando edges reais (≥ 0.09) com frequência suficiente para trades.
    """

    # Vol implícita por símbolo para calcular p_fair
    # Calibrada para horizonte de ~60 min
    IMPL_VOL: dict[str, float] = {
        "BTC":   0.012,   # BTC ~1.2%/h
        "ETH":   0.018,
        "SOL":   0.025,
        "BNB":   0.016,
        "MATIC": 0.030,
        "DOGE":  0.028,
        "XRP":   0.020,
    }

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

        # Preço simulado por token_id — inicializado de forma realista
        self._probs: dict[str, float] = {}
        for m in markets:
            self._probs[m.token_id] = self._initial_price(m)

    def _initial_price(self, m: PolyMarket) -> float:
        """Calcula preço inicial realista baseado no preço spot vs strike implícito."""
        buf = self.price_buffers.get(m.symbol)
        spot = buf.latest_price() if buf else None
        return self._fair_price(m, spot)

    def _fair_price(self, m: PolyMarket, spot: float | None) -> float:
        """
        Estima probabilidade fair do YES baseado no preço spot atual.
        Se não tem spot, usa yes_price da Gamma API como fallback.
        """
        if spot is None or spot <= 0:
            return m.yes_price if 0.05 <= m.yes_price <= 0.95 else 0.50

        # Extrai strike implícito da question (ex: "BTC above $85,000")
        strike = self._extract_strike(m.question, m.symbol, spot)
        if strike is None or strike <= 0:
            return m.yes_price if 0.05 <= m.yes_price <= 0.95 else 0.50

        # Horizonte em horas (mínimo 5 min)
        horizon_h = max(m.mins_left(), 5) / 60.0

        # Vol ajustada para o horizonte: vol_h = daily_vol * sqrt(horizon_h / 24)
        daily_vol = self.IMPL_VOL.get(m.symbol, 0.020) * math.sqrt(24)
        vol_h     = daily_vol * math.sqrt(horizon_h / 24)

        # z-score: quão longe está o spot do strike em desvios padrão
        z = (math.log(spot / strike)) / vol_h

        # YES = P(spot > strike em T) ≈ N(z) para movimento log-normal
        p = 0.5 * (1 + math.erf(z / math.sqrt(2)))
        return max(0.05, min(0.95, p))

    @staticmethod
    def _extract_strike(question: str, symbol: str, spot: float) -> float | None:
        """Extrai o strike price da pergunta. Ex: 'BTC above $85,000' → 85000.0"""
        import re
        q = question.lower()

        # Padrões: "$85,000", "$85k", "85000", "85,000"
        patterns = [
            r'\$\s*([\d,]+(?:\.\d+)?)\s*k\b',   # $85k
            r'\$\s*([\d,]+(?:\.\d+)?)',            # $85,000 ou $85000
            r'\b([\d,]{4,}(?:\.\d+)?)\b',         # 85000 ou 85,000 sem $
        ]

        for pat in patterns:
            m = re.search(pat, q)
            if m:
                raw = m.group(1).replace(",", "")
                try:
                    val = float(raw)
                    # Se usou "k" multiplica por 1000
                    if 'k' in pat:
                        val *= 1000
                    # Sanity check: strike deve ser razoável vs spot
                    if 0.1 * spot <= val <= 10 * spot:
                        return val
                except ValueError:
                    continue
        return None

    def start(self):
        self._running  = True
        self.connected = True
        threading.Thread(target=self._loop, daemon=True, name="MockClob").start()
        print(f"[MockClob] Simulating {len(self.markets)} markets with realistic pricing")

    def stop(self):
        self._running = False

    def avg_latency_ms(self) -> float:
        s = self.latencies[-50:]
        return sum(s) / len(s) if s else 0.0

    def _loop(self):
        t = 0
        while self._running:
            active = [m for m in self.markets if m.mins_left() > 0.5]
            for m in active:
                buf  = self.price_buffers.get(m.symbol)
                spot = buf.latest_price() if buf else None

                # Preço justo baseado no spot atual
                p_fair = self._fair_price(m, spot)

                # Ruído de mercado em torno do fair value (market maker spread + noise)
                vol_noise = self.IMPL_VOL.get(m.symbol, 0.020) * 0.3
                noise     = random.gauss(0, vol_noise)

                # Mean-reversion suave para o fair price
                current   = self._probs.get(m.token_id, p_fair)
                reversion = (p_fair - current) * 0.15   # puxa 15% em direção ao fair
                new_p     = current + reversion + noise
                new_p     = max(0.03, min(0.97, new_p))
                self._probs[m.token_id] = new_p

                spread = random.uniform(0.008, 0.025)
                bid    = max(0.02, new_p - spread / 2)
                ask    = min(0.98, new_p + spread / 2)

                self.msg_count += 1
                self.last_ts    = time.time()
                lat = random.uniform(3, 25)
                self.latencies.append(lat)
                if len(self.latencies) > 100:
                    self.latencies.pop(0)

                self.on_update(m.token_id, bid, ask)

            time.sleep(0.8)
            t += 1