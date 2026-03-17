"""
src/mm/market_engine.py — Motor de Market Making para Polymarket CLOB.

Em DRY_RUN, o FillSimulator replica o comportamento real:
  - Chegada de takers via processo de Poisson
  - Drift direcional por mercado (simula momentum de curto prazo)
  - Bursts de atividade (períodos de alta negociação)
  - Fills parciais ocasionais
  - Adverse selection: ordens muito próximas do mid têm mais risco
"""
from __future__ import annotations

import logging
import math
import random
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from threading import RLock
from typing import Callable

logger = logging.getLogger("mm.engine")

MIN_VOL_TICKS     = 10
DEFAULT_SPREAD    = 0.025
MAX_QUOTE_AGE     = 30.0


@dataclass
class ActiveOrder:
    order_id:  str
    token_id:  str
    side:      str
    price:     float
    size:      float
    placed_ts: float = field(default_factory=time.time)


@dataclass
class BookSnapshot:
    bid:    float
    ask:    float
    mid:    float
    spread: float
    ts:     float = field(default_factory=time.time)


# ── Fill Simulator ─────────────────────────────────────────────────────────────

class FillSimulator:
    """
    Simula chegada de takers e fills em DRY_RUN.

    Modelo:
      - Cada mercado tem um "sentiment" que faz random walk (-1 a +1).
        sentiment > 0 → mais takers comprando → ask preenche mais.
        sentiment < 0 → mais takers vendendo → bid preenche mais.

      - Chegada de takers: processo de Poisson com λ base ajustado por:
          * atividade do mercado (burst mode)
          * distância da ordem ao mid (mais longe = menos provável)
          * sentiment direcional

      - Bursts: a cada ~3min um burst de 30-90s com λ × 4

      - Fills parciais: 20% dos fills preenchem 40-80% do tamanho
    """

    # λ base: ~20-30 fills/hora = agressivo para testes visíveis no dashboard
    BASE_LAMBDA   = 0.006    # fills/segundo por ordem competitiva
    CHECK_INTERVAL = 5.0     # segundos entre verificações

    def __init__(self, engine: "MarketEngine") -> None:
        self._engine     = engine
        self._stop       = threading.Event()

        # Estado por token_id
        self._sentiment:   dict[str, float] = {}   # -1 a +1
        self._burst_until: dict[str, float] = {}   # timestamp fim do burst
        self._next_burst:  dict[str, float] = {}   # timestamp próximo burst

        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="FillSim",
        )

    def start(self) -> None:
        self._thread.start()
        logger.info("[FillSim] Simulador de fills iniciado (base_lambda=%.4f/s)", self.BASE_LAMBDA)

    def stop(self) -> None:
        self._stop.set()

    # ── Loop principal ─────────────────────────────────────────────────────

    def _loop(self) -> None:
        while not self._stop.wait(self.CHECK_INTERVAL):
            try:
                self._tick()
            except Exception as e:
                logger.debug("FillSim tick erro: %s", e)

    def _tick(self) -> None:
        now = time.time()

        with self._engine._lock:
            orders = list(self._engine._active_orders.values())

        if not orders:
            return

        # Agrupa por token para processar sentiment por mercado
        by_token: dict[str, list[ActiveOrder]] = {}
        for o in orders:
            by_token.setdefault(o.token_id, []).append(o)

        for token_id, token_orders in by_token.items():
            self._update_sentiment(token_id, now)
            burst = self._is_burst(token_id, now)

            book = self._engine._current_book.get(token_id)
            if not book:
                # Sem dados de WebSocket ainda — cria book sintético a partir
                # dos preços das ordens ativas (bid máximo / ask mínimo)
                bids = [o.price for o in token_orders if o.side == "buy"]
                asks = [o.price for o in token_orders if o.side == "sell"]
                if not bids or not asks:
                    continue
                b = max(bids); a = min(asks)
                if b >= a:
                    continue
                book = BookSnapshot(bid=b, ask=a, mid=(b+a)/2, spread=a-b)

            for order in token_orders:
                prob = self._fill_probability(order, book, burst)
                if random.random() < prob:
                    fill_size = self._pick_fill_size(order.size)
                    self._process_fill(order, fill_size, token_id)

    # ── Sentiment & Burst ──────────────────────────────────────────────────

    def _update_sentiment(self, token_id: str, now: float) -> None:
        """
        Random walk do sentiment com mean-reversion suave para 0.
        Simula o fluxo direcional de curto prazo de um mercado real.
        """
        s = self._sentiment.get(token_id, 0.0)
        # Ruído + mean-reversion
        noise      = random.gauss(0, 0.08)
        reversion  = -s * 0.05
        s = max(-1.0, min(1.0, s + noise + reversion))
        self._sentiment[token_id] = s

    def _is_burst(self, token_id: str, now: float) -> bool:
        """
        Burst = período de atividade elevada (~4× o normal).
        Ocorre aleatoriamente a cada 2-5 minutos, dura 30-90 segundos.
        Simula notícias, expiração próxima, arbitrageurs entrando.
        """
        if now < self._burst_until.get(token_id, 0):
            return True

        next_b = self._next_burst.get(token_id, 0)
        if next_b == 0:
            self._next_burst[token_id] = now + random.uniform(60, 180)
            return False

        if now >= next_b:
            duration = random.uniform(30, 90)
            self._burst_until[token_id] = now + duration
            self._next_burst[token_id]  = now + duration + random.uniform(120, 300)
            logger.debug("FillSim burst em %s por %.0fs", token_id[:10], duration)
            return True

        return False

    # ── Fill Probability ───────────────────────────────────────────────────

    def _fill_probability(
        self,
        order: ActiveOrder,
        book:  BookSnapshot,
        burst: bool,
    ) -> float:
        """
        P(fill em CHECK_INTERVAL segundos) via processo de Poisson.

        Fatores:
          1. Distância do preço ao mid (mais longe = menos provável)
          2. Sentiment direcional (favorece um lado)
          3. Burst mode (λ × 4)
        """
        mid      = book.mid
        spread   = max(book.spread, 0.01)
        sentiment = self._sentiment.get(order.token_id, 0.0)

        # Distância normalizada: 0 = no mid, 1 = na borda do spread
        dist_from_mid = abs(order.price - mid)
        rel_dist      = min(dist_from_mid / (spread * 1.5), 1.0)

        # Fator de distância: 1.0 no mid, 0.15 na borda
        dist_factor = 1.0 - rel_dist * 0.85

        # Fator de sentiment: bid fill mais provável quando sentiment < 0 (vendedores)
        if order.side == "buy":
            # sentiment -1 → fator 1.8,  sentiment +1 → fator 0.3
            sent_factor = 1.0 - sentiment * 0.75
        else:
            # sentiment +1 → fator 1.8,  sentiment -1 → fator 0.3
            sent_factor = 1.0 + sentiment * 0.75

        sent_factor = max(0.1, sent_factor)

        # λ efetivo
        lam = self.BASE_LAMBDA * dist_factor * sent_factor
        if burst:
            lam *= 4.0

        # P(pelo menos 1 chegada em dt) = 1 - e^(-λ·dt)
        return 1.0 - math.exp(-lam * self.CHECK_INTERVAL)

    # ── Fill Processing ────────────────────────────────────────────────────

    def _pick_fill_size(self, order_size: float) -> float:
        """80% fill total, 20% fill parcial (40-85% do tamanho)."""
        if random.random() < 0.20:
            pct = random.uniform(0.40, 0.85)
            return round(max(0.5, order_size * pct), 1)
        return order_size

    def _process_fill(self, order: ActiveOrder, fill_size: float, token_id: str) -> None:
        """Remove a ordem do tracking e dispara os callbacks de fill."""
        engine = self._engine

        with engine._lock:
            # Se a ordem já foi cancelada/expirada, ignora
            if order.order_id not in engine._active_orders:
                return
            engine._active_orders.pop(order.order_id)
            ids = engine._market_orders.get(token_id, [])
            if order.order_id in ids:
                ids.remove(order.order_id)

        # Atualiza inventário no RiskManager
        meta = {}
        if hasattr(engine, '_markets_meta_ref'):
            meta = engine._markets_meta_ref.get(token_id, {})

        engine.risk.on_fill(
            token_id=token_id,
            side=order.side,
            size=fill_size,
            price=order.price,
            question=meta.get("question", ""),
            symbol=meta.get("symbol", ""),
        )

        # Callback externo (bot_mm._on_fill → log + dashboard)
        if engine.on_fill_cb:
            engine.on_fill_cb(
                order_id=order.order_id,
                token_id=token_id,
                side=order.side,
                size=fill_size,
                price=order.price,
                symbol=meta.get("symbol", "?"),
            )

        partial_tag = f" (parcial {fill_size:.1f}/{order.size:.1f})" if fill_size < order.size else ""
        logger.info(
            "SIM-FILL %s %s %.2f @ %.3f%s | sent=%.2f",
            order.side.upper(), token_id[:10], fill_size, order.price,
            partial_tag, self._sentiment.get(token_id, 0),
        )


# ── Market Engine ──────────────────────────────────────────────────────────────

class MarketEngine:
    """
    Motor principal de Market Making.

    Fluxo por mercado:
      1. Recebe atualização de book via WebSocket (on_book_update)
      2. Calcula spread dinâmico baseado na volatilidade dos mids recentes
      3. Consulta IntelligenceService → SpreadMode (aggressive/defensive)
      4. Aplica ajuste de inventário via RiskManager
      5. Cancela ordens velhas; coloca novas bid/ask post-only
      6. Em DRY_RUN: FillSimulator processa fills realistas
         Em LIVE:    poll_fills() detecta fills via REST
    """

    def __init__(
        self,
        clob_client,
        risk_manager,
        intelligence_service,
        dry_run:              bool = True,
        on_fill_cb:           Callable | None = None,
        on_quote_cb:          Callable | None = None,
        on_allowance_needed:  Callable | None = None,
    ) -> None:
        self.clob                = clob_client
        self.risk                = risk_manager
        self.intel               = intelligence_service
        self.dry_run             = dry_run
        self.on_fill_cb          = on_fill_cb
        self.on_quote_cb         = on_quote_cb
        self._on_allowance_needed = on_allowance_needed

        self._active_orders: dict[str, ActiveOrder] = {}
        self._market_orders: dict[str, list[str]]   = {}
        self._book_history:  dict[str, deque]        = {}
        self._current_book:  dict[str, BookSnapshot] = {}
        self._last_quote_ts: dict[str, float]        = {}

        self._lock           = RLock()
        self._total_cancelled = 0
        self._total_placed    = 0
        # Contador de falhas consecutivas de SELL por allowance por token
        self._sell_allowance_fails: dict[str, int] = {}

        # Referência para meta de mercados (injetada pelo bot)
        self._markets_meta_ref: dict = {}

        # Inicia simulador de fills apenas em DRY_RUN
        self._fill_sim: FillSimulator | None = None
        if dry_run:
            self._fill_sim = FillSimulator(self)
            self._fill_sim.start()

    def set_markets_meta(self, meta: dict) -> None:
        """Injeta referência ao markets_meta para o FillSimulator ter acesso ao symbol."""
        self._markets_meta_ref = meta

    # ── Book Feed ──────────────────────────────────────────────────────────

    def on_book_update(self, token_id: str, bid: float, ask: float) -> None:
        if bid <= 0 or ask <= 0 or bid >= ask:
            return
        mid  = (bid + ask) / 2
        snap = BookSnapshot(bid=bid, ask=ask, mid=mid, spread=ask - bid)
        with self._lock:
            if token_id not in self._book_history:
                self._book_history[token_id] = deque(maxlen=60)
            self._book_history[token_id].append(snap)
            self._current_book[token_id] = snap

    # ── Spread Dinâmico ────────────────────────────────────────────────────

    def calculate_dynamic_spread(self, token_id: str, mode: str = "defensive") -> float:
        with self._lock:
            history = list(self._book_history.get(token_id, []))

        if len(history) < MIN_VOL_TICKS:
            base = DEFAULT_SPREAD
            return round(base * (1.2 if mode == "defensive" else 0.9), 4)

        mids      = [s.mid for s in history[-30:]]
        mean_mid  = sum(mids) / len(mids)
        sigma     = math.sqrt(sum((m - mean_mid) ** 2 for m in mids) / len(mids))

        k         = 3.5 if mode == "defensive" else 2.0
        vol_spr   = k * sigma

        book_sprs     = [s.spread for s in history[-10:]]
        avg_book_spr  = sum(book_sprs) / len(book_sprs)

        spread = max(vol_spr, avg_book_spr * 0.8)
        spread = max(0.010, min(0.060, spread))
        return round(spread, 4)

    # ── Quoting ────────────────────────────────────────────────────────────

    def should_requote(self, token_id: str) -> bool:
        with self._lock:
            order_ids = self._market_orders.get(token_id, [])
            # Sem ordens ativas → sempre precisa cotar
            if not order_ids:
                return True
            # Com ordens ativas mas sem book → não mexa
            if not self._current_book.get(token_id):
                return False
            for oid in order_ids:
                order = self._active_orders.get(oid)
                if order and (time.time() - order.placed_ts) > MAX_QUOTE_AGE:
                    return True
        return False

    def quote_market(
        self,
        token_id:   str,
        question:   str,
        symbol:     str,
        market_mid: float | None = None,
    ) -> list[str]:
        ok, reason = self.risk.can_quote(token_id)
        if not ok:
            logger.debug("Skip quote %s: %s", token_id[:10], reason)
            return []

        with self._lock:
            snap = self._current_book.get(token_id)

        if snap is None and market_mid is None:
            return []

        mid         = snap.mid if snap else market_mid
        vol         = self._get_recent_vol(token_id)
        book_spread = snap.spread if snap else DEFAULT_SPREAD
        decision    = self.intel.get_spread_mode(question, symbol, mid, vol, book_spread)
        spread      = self.calculate_dynamic_spread(token_id, mode=decision.mode)

        bid_price, ask_price = self.risk.adjust_quotes(mid, spread, token_id)
        bid_size  = self.risk.order_size(bid_price, token_id, "buy")
        ask_size  = self.risk.order_size(ask_price, token_id, "sell")

        placed = []
        if bid_size > 0 and self.risk.can_buy(token_id):
            oid = self._place_order(token_id, "buy",  bid_price, bid_size)
            if oid:
                placed.append(oid)
        if ask_size > 0:
            oid = self._place_order(token_id, "sell", ask_price, ask_size)
            if oid:
                placed.append(oid)

        if placed and self.on_quote_cb:
            self.on_quote_cb(
                token_id=token_id, symbol=symbol,
                bid=bid_price, ask=ask_price,
                spread=spread, mode=decision.mode,
            )

        logger.info(
            "[%s] %s | mid=%.3f bid=%.3f ask=%.3f spr=%.4f [%s]",
            symbol, token_id[:10], mid, bid_price, ask_price, spread, decision.mode,
        )
        return placed

    def cancel_market_orders(self, token_id: str) -> int:
        with self._lock:
            order_ids = list(self._market_orders.get(token_id, []))
        return sum(1 for oid in order_ids if self._cancel_order(oid))

    def cancel_all(self) -> int:
        logger.warning("CANCEL ALL — graceful shutdown")

        if self._fill_sim:
            self._fill_sim.stop()

        try:
            if not self.dry_run:
                self.clob.cancel_all()
            with self._lock:
                total = len(self._active_orders)
                self._active_orders.clear()
                self._market_orders.clear()
            self._total_cancelled += total
            logger.warning("cancel_all() OK — %d ordens", total)
            return total
        except Exception as e:
            logger.error("cancel_all() falhou: %s", e)

        with self._lock:
            order_ids = list(self._active_orders.keys())
        cancelled = sum(1 for oid in order_ids if self._cancel_order(oid))
        logger.warning("Cancelamento individual: %d/%d", cancelled, len(order_ids))
        return cancelled

    # ── Fill Detection (LIVE) ──────────────────────────────────────────────

    def poll_fills(self, markets_meta: dict[str, dict]) -> list[dict]:
        """Detecta fills via REST (apenas em modo LIVE).

        Consulta o status de cada ordem individualmente para distinguir:
        - matched/mailed: fill real → registra no P&L
        - cancelled/expired: cancelada → libera reserva sem registrar fill
        """
        if self.dry_run:
            return []   # FillSimulator cuida disso em DRY_RUN

        with self._lock:
            expected_ids = set(self._active_orders.keys())
        if not expected_ids:
            return []

        try:
            open_orders = self._get_open_orders()
            open_ids    = {o.get("id") or o.get("orderID") for o in open_orders}
            fills       = []

            for oid in list(expected_ids):
                if oid in open_ids:
                    continue  # ainda aberta

                # Ordem sumiu — verifica status real para distinguir fill de cancelamento
                status, filled_size = self._get_order_status(oid)

                # Se status desconhecido (API falhou), mantém a ordem no tracking e tenta novamente no próximo poll
                if status == "unknown":
                    logger.warning("Status desconhecido para ordem %s — mantendo no tracking", oid[-8:])
                    continue

                with self._lock:
                    order = self._active_orders.pop(oid, None)
                    if order:
                        ids = self._market_orders.get(order.token_id, [])
                        if oid in ids:
                            ids.remove(oid)

                if not order:
                    continue

                meta = markets_meta.get(order.token_id, {})

                if status in ("matched",) or (filled_size and filled_size > 0):
                    # Fill real — registra P&L
                    size = filled_size if filled_size else order.size
                    self.risk.on_fill(
                        token_id=order.token_id, side=order.side,
                        size=size, price=order.price,
                        question=meta.get("question", ""),
                        symbol=meta.get("symbol", ""),
                    )
                    fill = {
                        "order_id": oid, "token_id": order.token_id,
                        "side": order.side, "size": size,
                        "price": order.price, "symbol": meta.get("symbol", ""),
                    }
                    fills.append(fill)
                    if self.on_fill_cb:
                        self.on_fill_cb(**fill)
                else:
                    # Cancelada/rejeitada/expirada — libera reserva sem registrar fill
                    self.risk.on_order_cancelled(order.token_id, order.side, order.size, order.price)
                    logger.info("Ordem %s...%s cancelada/expirada (status=%s)", order.side, oid[-8:], status)

            return fills
        except Exception as e:
            logger.error("poll_fills erro: %s", e)
            return []

    def _get_order_status(self, order_id: str) -> tuple[str, float | None]:
        """Consulta status e tamanho preenchido de uma ordem específica."""
        try:
            resp = self.clob.get_order(order_id)
            if isinstance(resp, dict):
                status = (resp.get("status") or resp.get("orderStatus") or "unknown").lower()
                filled = resp.get("makerAmount") or resp.get("sizeMatched") or resp.get("filledSize")
                filled_size = float(filled) if filled else None
                return status, filled_size
        except Exception as e:
            logger.debug("get_order_status %s: %s", order_id[-8:], e)
        return "unknown", None

    # ── Internal Helpers ───────────────────────────────────────────────────

    POLYMARKET_MIN_SHARES = 5.0   # mínimo exigido pela exchange

    def _place_order(self, token_id: str, side: str, price: float, size: float) -> str | None:
        # Garante mínimo de shares exigido pela Polymarket (evita 400 desnecessário)
        if not self.dry_run and size < self.POLYMARKET_MIN_SHARES:
            logger.info("Ordem ignorada (min shares) — size %.1f < %d", size, self.POLYMARKET_MIN_SHARES)
            return None
        try:
            if self.dry_run:
                import uuid
                order_id = f"dry_{uuid.uuid4().hex[:12]}"
            else:
                from py_clob_client.clob_types import OrderArgs
                from py_clob_client.order_builder.constants import BUY, SELL
                clob_side = BUY if side == "buy" else SELL
                order    = self.clob.create_order(OrderArgs(
                    token_id=token_id, price=price, size=size, side=clob_side,
                ))
                resp     = self.clob.post_order(order)
                order_id = resp.get("orderID") or resp.get("id") or resp.get("order_id")
                if not order_id:
                    logger.warning("post_order sem order_id: %s", resp)
                    return None

            active = ActiveOrder(order_id=order_id, token_id=token_id,
                                 side=side, price=price, size=size)
            with self._lock:
                self._active_orders[order_id] = active
                self._market_orders.setdefault(token_id, []).append(order_id)

            # Reserva capital para ordens BUY abertas
            self.risk.on_order_placed(token_id, side, size, price)
            self._total_placed += 1
            return order_id

        except Exception as e:
            err = str(e).lower()
            if "post_only" in err or "would cross" in err:
                logger.debug("Rejeitada (cruzaria book) %s @ %.3f", side, price)
            elif "allowance" in err and side == "sell":
                fails = self._sell_allowance_fails.get(token_id, 0) + 1
                self._sell_allowance_fails[token_id] = fails
                if fails <= 3:
                    logger.warning("SELL allowance fail %d/3 ...%s — %s", fails, token_id[-12:], e)
                    if self._on_allowance_needed:
                        self._on_allowance_needed(token_id)
                else:
                    # Desiste após 3 falhas — posição provavelmente zerada on-chain
                    logger.error("SELL ...%s desistido apos %d falhas — removendo posicao da memoria", token_id[-12:], fails)
                    self.risk.clear_position(token_id)
            else:
                logger.error("Erro ao colocar ordem %s @ %.3f: %s", side, price, e)
            return None

    def _cancel_order(self, order_id: str) -> bool:
        try:
            if not self.dry_run:
                self.clob.cancel(order_id)
            with self._lock:
                order = self._active_orders.pop(order_id, None)
                if order:
                    ids = self._market_orders.get(order.token_id, [])
                    if order_id in ids:
                        ids.remove(order_id)
            # Libera reserva de capital da ordem cancelada
            if order:
                self.risk.on_order_cancelled(order.token_id, order.side, order.size, order.price)
            self._total_cancelled += 1
            return True
        except Exception as e:
            logger.warning("Erro ao cancelar %s: %s", order_id[:16], e)
            return False

    def _get_open_orders(self) -> list[dict]:
        try:
            resp = self.clob.get_orders()
            return resp.get("data", []) if isinstance(resp, dict) else (resp or [])
        except Exception as e:
            logger.error("get_orders erro: %s", e)
            return []

    def _get_recent_vol(self, token_id: str) -> float:
        with self._lock:
            history = list(self._book_history.get(token_id, []))
        if len(history) < 5:
            return 0.0
        mids = [s.mid for s in history[-20:]]
        mean = sum(mids) / len(mids)
        return math.sqrt(sum((m - mean) ** 2 for m in mids) / len(mids))

    # ── Stats ──────────────────────────────────────────────────────────────

    def stats(self) -> dict:
        with self._lock:
            return {
                "active_orders":   len(self._active_orders),
                "markets_quoted":  len(self._market_orders),
                "total_placed":    self._total_placed,
                "total_cancelled": self._total_cancelled,
                "dry_run":         self.dry_run,
            }
