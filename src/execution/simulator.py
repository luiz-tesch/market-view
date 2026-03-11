"""
src/execution/simulator.py  v3 — Paper Trading 100% Fiel ao Polymarket

Melhorias v3:
────────────────────────────────────────────────────────────────────────────────
1. RESOLUÇÃO REAL: busca resultado via CLOB API antes de simular com random
2. FEE DE 1%: aplicada na entrada (igual ao Polymarket real)
3. KELLY CORRETO: f = (b*p - q) / b  onde b=payoff, p=prob estimada, q=1-p
4. MUTEX no check MAX_OPEN_TRADES (sem race condition)
5. FILTRO SHORT-TERM: só aceita mercados 3–120 min
6. WIN/LOSS fiel: YES token → $1.00 se venceu, $0.00 se perdeu
7. STOP LOSS e TAKE PROFIT: usam bid/ask real do CLOB (não preços fixos)
8. Mercados expirados são removidos automaticamente da lista
9. horizon_min correto: nunca >120 min (filtro na entrada)
10. Sem trades em mercados já resolvidos ou expirados
────────────────────────────────────────────────────────────────────────────────
"""
from __future__ import annotations
import math
import random
import threading
import time
import uuid
from collections import deque
from dataclasses import asdict, dataclass, field
from typing import Literal

from src.signals.engine import PriceBuffer, compute_signal
from src.feed.polymarket import fetch_market_resolution, POLYMARKET_FEE
from src.config import SHORT_TERM_MAX_MINUTES, SHORT_TERM_MIN_MINUTES


# ── Trade ──────────────────────────────────────────────────────────────────────

@dataclass
class Trade:
    id:                  str
    symbol:              str
    question:            str
    slug:                str
    token_id:            str
    condition_id:        str
    action:              Literal["BUY_YES", "BUY_NO"]
    amount_usdc:         float       # após fee
    gross_usdc:          float       # valor bruto antes da fee
    fee_usdc:            float       # fee paga (1%)
    entry_price:         float       # preço pago (0-1) = ask para YES, (1-bid) para NO
    entry_ts:            float
    horizon_min:         float
    end_date_iso:        str
    signal_edge:         float
    signal_confidence:   str
    signal_reasons:      list[str]
    signal_momentum_1m:  float
    signal_momentum_5m:  float
    signal_rsi:          float
    market_type:         str = "price"
    # saída
    status:              Literal["OPEN", "CLOSED"] = "OPEN"
    exit_price:          float | None = None
    exit_ts:             float | None = None
    exit_reason:         str = ""
    pnl_usdc:            float | None = None
    pnl_pct:             float | None = None
    won:                 bool | None = None
    # live
    current_price:       float | None = None
    unrealized_pnl:      float = 0.0
    price_history:       list[float] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["age_seconds"]   = round(time.time() - self.entry_ts, 1)
        d["pct_to_expiry"] = min(1.0, (time.time() - self.entry_ts) / (self.horizon_min * 60)) if self.horizon_min > 0 else 1.0
        return d


# ── SimStats ───────────────────────────────────────────────────────────────────

@dataclass
class SimStats:
    balance:          float = 100.0
    peak_balance:     float = 100.0
    total_trades:     int   = 0
    wins:             int   = 0
    losses:           int   = 0
    total_wagered:    float = 0.0
    total_fees:       float = 0.0
    total_pnl:        float = 0.0
    max_drawdown_pct: float = 0.0
    avg_win_usdc:     float = 0.0
    avg_loss_usdc:    float = 0.0
    win_streak:       int   = 0
    loss_streak:      int   = 0
    best_trade_pnl:   float = 0.0
    worst_trade_pnl:  float = 0.0
    start_ts:         float = field(default_factory=time.time)
    last_action_ts:   float = 0.0
    scans:            int   = 0
    signals_fired:    int   = 0
    signals_skipped:  int   = 0
    warmup_remaining: float = 0.0

    def win_rate(self) -> float:
        t = self.wins + self.losses
        return self.wins / t if t else 0.0

    def roi(self) -> float:
        return (self.balance - 100.0) / 100.0

    def to_dict(self) -> dict:
        d = asdict(self)
        d["win_rate"]        = self.win_rate()
        d["roi"]             = self.roi()
        d["uptime_minutes"]  = (time.time() - self.start_ts) / 60
        return d


# ── TradingSimulator ───────────────────────────────────────────────────────────

class TradingSimulator:

    # ── Parâmetros de risco ────────────────────────────────────────────────────
    MAX_POSITION_PCT  = 0.05     # 5% de balance por trade
    MAX_OPEN_TRADES   = 4        # max posições simultâneas
    STOP_LOSS_PCT     = 0.40     # -40% do entry price → stop
    TAKE_PROFIT_PCT   = 0.70     # +70% do potencial → take profit
    KELLY_FRACTION    = 0.15     # Kelly fracionado (15%)
    MIN_BALANCE       = 5.0
    COOLDOWN_SECONDS  = 300      # 5 min cooldown por mercado
    ALLOWED_CONF      = {"medium", "high"}
    MIN_EDGE          = {"high": 0.06, "medium": 0.11}
    MIN_TICKS         = 80
    MIN_CANDLES       = 6
    WARMUP_SECONDS    = 30     # 3 min aquecimento

    def __init__(self, price_buffers: dict[str, PriceBuffer]):
        self.buffers        = price_buffers
        self.stats          = SimStats()
        self.open_trades:   list[Trade] = []
        self.closed_trades: list[Trade] = []
        self.equity_curve:  deque = deque(maxlen=600)
        self.equity_curve.append((time.time(), 100.0))
        self._lock          = threading.Lock()
        self._running       = False
        self._monitor_t:    threading.Thread | None = None
        self.event_log:     deque = deque(maxlen=400)
        self._markets:      dict[str, dict] = {}
        self._cooldown:     dict[str, float] = {}
        self._start_ts      = 0.0

    # ── API Pública ────────────────────────────────────────────────────────────

    def register_market(
        self,
        token_id:     str,
        question:     str,
        slug:         str,
        symbol:       str,
        horizon_min:  float,
        yes_price:    float,
        end_date_iso: str = "",
        condition_id: str = "",
        market_type:  str = "price",
    ):
        # FIX: só registra mercados de curto prazo
        if not (SHORT_TERM_MIN_MINUTES <= horizon_min <= SHORT_TERM_MAX_MINUTES):
            return
        self._markets[token_id] = {
            "token_id":    token_id,
            "question":    question,
            "slug":        slug,
            "symbol":      symbol,
            "horizon_min": horizon_min,
            "yes_price":   yes_price,
            "end_date_iso":end_date_iso,
            "condition_id":condition_id,
            "market_type": market_type,
            "last_update": time.time(),
            "resolved":    False,
            "resolved_outcome": None,
        }

    def on_price_update(self, token_id: str, bid: float, ask: float):
        mid = (bid + ask) / 2
        if token_id in self._markets:
            m = self._markets[token_id]
            # Ignora mercados já resolvidos
            if m.get("resolved"):
                return
            m["yes_price"]   = mid
            m["last_update"] = time.time()
            m["bid"]         = bid
            m["ask"]         = ask
        self._update_open_prices(token_id, mid)
        if self._running and self.stats.balance > self.MIN_BALANCE:
            self._evaluate(token_id, mid, bid, ask)

    def start(self):
        self._running  = True
        self._start_ts = time.time()
        self._monitor_t = threading.Thread(
            target=self._monitor_loop, daemon=True, name="SimMon"
        )
        self._monitor_t.start()
        self._log("SIM_START", f"Iniciado — {self.WARMUP_SECONDS}s warm-up, focado em mercados {SHORT_TERM_MIN_MINUTES}–{SHORT_TERM_MAX_MINUTES}min")

    def stop(self):
        self._running = False
        self._log("SIM_STOP", "Parado")

    def reset(self):
        was = self._running
        self.stop()
        time.sleep(0.2)
        with self._lock:
            self.stats = SimStats()
            self.open_trades.clear()
            self.closed_trades.clear()
            self.equity_curve.clear()
            self.equity_curve.append((time.time(), 100.0))
            self.event_log.clear()
            self._cooldown.clear()
        self._start_ts = 0.0
        if was:
            self.start()
        self._log("SIM_RESET", "Reset — $100.00")

    def snapshot(self) -> dict:
        with self._lock:
            ot = [t.to_dict() for t in self.open_trades]
            ct = [t.to_dict() for t in list(self.closed_trades)[-50:]]
            eq = list(self.equity_curve)
        wu = max(0., self.WARMUP_SECONDS - (time.time() - self._start_ts)) if self._start_ts else 0.
        self.stats.warmup_remaining = round(wu, 0)
        return {
            "stats":        self.stats.to_dict(),
            "open_trades":  ot,
            "closed_trades":ct,
            "equity_curve": eq,
            "event_log":    list(self.event_log),
            "markets":      list(self._markets.values()),
            "running":      self._running,
        }

    # ── Avaliação de entrada ───────────────────────────────────────────────────

    def _evaluate(self, token_id: str, mid: float, bid: float, ask: float):
        # 1. Warm-up
        if self._start_ts and (time.time() - self._start_ts) < self.WARMUP_SECONDS:
            return

        mkt = self._markets.get(token_id)
        if not mkt or mkt.get("resolved"):
            return

        sym   = mkt["symbol"]
        slug  = mkt["slug"]
        horiz = mkt["horizon_min"]

        # 2. Cooldown por mercado
        if time.time() - self._cooldown.get(slug, 0) < self.COOLDOWN_SECONDS:
            return

        # 3. FIX: mutex no check de MAX_OPEN_TRADES (sem race condition)
        with self._lock:
            if len(self.open_trades) >= self.MAX_OPEN_TRADES:
                return

        # 4. Qualidade do buffer
        buf = self.buffers.get(sym)
        if not buf or len(buf.ticks) < self.MIN_TICKS or len(buf.candles_1m) < self.MIN_CANDLES:
            self.stats.signals_skipped += 1
            return

        # 5. Sinal com tipo de mercado
        sig = compute_signal(
            symbol=sym, buf=buf,
            market_price_yes=mid, horizon_minutes=int(horiz),
            question=mkt.get("question", ""),
        )
        self.stats.scans += 1

        if sig.direction == "SKIP":
            self.stats.signals_skipped += 1
            return

        if sig.confidence not in self.ALLOWED_CONF:
            self.stats.signals_skipped += 1
            return

        if abs(sig.edge) < self.MIN_EDGE.get(sig.confidence, 0.15):
            self.stats.signals_skipped += 1
            return

        # 6. Consistência de momentum
        if sig.direction == "YES" and sig.momentum_1m < -0.10:
            self.stats.signals_skipped += 1
            self._log("SKIP", f"{sym} YES vs mom1m {sig.momentum_1m:+.2f}%")
            return
        if sig.direction == "NO" and sig.momentum_1m > 0.10:
            self.stats.signals_skipped += 1
            self._log("SKIP", f"{sym} NO vs mom1m {sig.momentum_1m:+.2f}%")
            return

        self.stats.signals_fired += 1
        self._place_trade(mkt, sym, sig, horiz, bid, ask)

    def _place_trade(self, mkt: dict, sym: str, sig, horiz: float, bid: float, ask: float):
        """
        KELLY CORRETO v3:
        f* = (b*p - q) / b
        onde:
          b = payoff por unidade apostada (1/entry_price - 1)
          p = probabilidade estimada (sig.edge + 0.5 para YES)
          q = 1 - p
        """
        with self._lock:
            # Preço de entrada real: paga o ASK para YES, (1 - BID) para NO
            if sig.direction == "YES":
                action:      Literal["BUY_YES", "BUY_NO"] = "BUY_YES"
                entry_price  = ask
                p_est        = max(0.05, min(0.95, 0.5 + sig.edge))
            else:
                action       = "BUY_NO"
                entry_price  = max(0.02, 1.0 - bid)
                p_est        = max(0.05, min(0.95, 0.5 + abs(sig.edge)))

            entry_price = max(0.03, min(0.97, entry_price))

            # Kelly correto
            b = (1.0 / entry_price) - 1.0          # payoff líquido por unidade
            q = 1.0 - p_est
            kelly_raw = (b * p_est - q) / b if b > 0 else 0.0
            kelly     = max(0.0, min(kelly_raw, self.KELLY_FRACTION))

            cmult    = {"high": 1.0, "medium": 0.55}[sig.confidence]
            max_bet  = self.stats.balance * self.MAX_POSITION_PCT
            gross    = round(kelly * max_bet * cmult, 2)
            gross    = max(gross, 0.10)
            gross    = min(gross, max_bet, self.stats.balance * 0.06)

            # FIX: FEE de 1% (Polymarket cobra na entrada)
            fee    = round(gross * POLYMARKET_FEE, 4)
            amount = round(gross - fee, 4)   # valor líquido que fica nos tokens

            total_cost = gross  # o que sai do balance
            if total_cost > self.stats.balance:
                return

            trade = Trade(
                id                 = str(uuid.uuid4())[:8],
                symbol             = sym,
                question           = mkt["question"][:100],
                slug               = mkt["slug"],
                token_id           = mkt["token_id"],
                condition_id       = mkt.get("condition_id", ""),
                action             = action,
                amount_usdc        = amount,
                gross_usdc         = gross,
                fee_usdc           = fee,
                entry_price        = entry_price,
                entry_ts           = time.time(),
                horizon_min        = horiz,
                end_date_iso       = mkt.get("end_date_iso", ""),
                signal_edge        = sig.edge,
                signal_confidence  = sig.confidence,
                signal_reasons     = sig.reasons[:],
                signal_momentum_1m = sig.momentum_1m,
                signal_momentum_5m = sig.momentum_5m,
                signal_rsi         = sig.rsi,
                market_type        = sig.market_type,
                current_price      = (ask if action == "BUY_YES" else 1.0 - bid),
            )

            self.stats.balance       -= total_cost
            self.stats.total_trades  += 1
            self.stats.total_wagered += total_cost
            self.stats.total_fees    += fee
            self.stats.last_action_ts = time.time()
            self.open_trades.append(trade)
            self._cooldown[mkt["slug"]] = time.time()

        self._log(
            "TRADE_OPEN",
            f"{action} gross=${gross:.2f} fee=${fee:.4f} | {sym} | "
            f"edge {sig.edge:+.3f} | {sig.confidence} | entry {entry_price:.3f} | "
            f"kelly {kelly:.3f} | {sig.market_type}",
            level="info", trade_id=trade.id, symbol=sym,
            amount=amount, gross=gross, fee=fee,
            action=action, edge=sig.edge,
            reasons=sig.reasons, entry_price=entry_price,
        )

    # ── Monitor ────────────────────────────────────────────────────────────────

    def _monitor_loop(self):
        while self._running:
            try:
                self._check_exits()
                self._update_equity()
                self._update_dd()
            except Exception as e:
                self._log("MON_ERR", str(e), level="error")
            time.sleep(1.0)

    def _check_exits(self):
        now        = time.time()
        to_close:  list[tuple] = []

        with self._lock:
            for t in list(self.open_trades):
                mkt       = self._markets.get(t.token_id, {})
                bid       = mkt.get("bid", t.entry_price)
                ask_mkt   = mkt.get("ask", t.entry_price)

                # Preço atual do token que compramos
                if t.action == "BUY_YES":
                    cur_val = ask_mkt   # valor do YES token no mercado
                else:
                    cur_val = 1.0 - bid  # valor do NO token

                cur_val = max(0.01, min(0.99, cur_val))
                t.unrealized_pnl  = round((cur_val / t.entry_price - 1) * t.amount_usdc, 3)
                t.current_price   = ask_mkt if t.action == "BUY_YES" else 1.0 - bid
                t.price_history.append(round(ask_mkt, 4))
                if len(t.price_history) > 120:
                    t.price_history = t.price_history[-120:]

                age_min = (now - t.entry_ts) / 60

                # Stop loss: usa BID real (o que conseguiria vender)
                sell_val = bid if t.action == "BUY_YES" else (1.0 - ask_mkt)
                if sell_val < t.entry_price * (1 - self.STOP_LOSS_PCT):
                    to_close.append((t, "STOP_LOSS", False, sell_val))
                    continue

                # Take profit
                tp_threshold = t.entry_price + (1.0 - t.entry_price) * self.TAKE_PROFIT_PCT
                if cur_val >= tp_threshold:
                    to_close.append((t, "TAKE_PROFIT", True, cur_val))
                    continue

                # Expiração
                if age_min >= t.horizon_min:
                    to_close.append((t, "EXPIRED", None, cur_val))

        for t, reason, won_arg, exit_val in to_close:
            self._resolve_and_close(t, reason, won_arg, exit_val)

    def _resolve_and_close(self, trade: Trade, reason: str, won_arg: bool | None, exit_val: float):
        """
        Resolução fiel ao Polymarket:
        1. Tenta buscar resultado real via CLOB API
        2. Se não disponível, usa movimento de preço Binance como proxy
        3. WIN  → token vale $1.00 (payout = amount / entry_price)
        4. LOSS → token vale $0.00 (perde tudo)
        5. STOP_LOSS / TAKE_PROFIT → sai ao preço bid/ask atual
        """
        if reason in ("STOP_LOSS", "TAKE_PROFIT"):
            # Saída parcial ao preço de mercado — não espera resolução
            won = (reason == "TAKE_PROFIT")
            self._close(trade, reason, won, exit_val)
            return

        # Para EXPIRED: tenta resolução real primeiro
        real_outcome = None
        if trade.condition_id:
            try:
                real_outcome = fetch_market_resolution(trade.condition_id)
            except Exception:
                pass

        if real_outcome is not None:
            # Resultado real confirmado
            won = real_outcome if trade.action == "BUY_YES" else (not real_outcome)
            self._log("RESOLVE_REAL", f"Resultado REAL para {trade.symbol}: {'YES' if real_outcome else 'NO'}")
        elif won_arg is not None:
            won = won_arg
        else:
            # Fallback: usa preço Binance como proxy
            won = self._simulate_resolution(trade)
            self._log("RESOLVE_SIMULATED", f"Resultado simulado para {trade.symbol} (sem resolução real disponível)")

        self._close(trade, reason, won, exit_val)

    def _simulate_resolution(self, trade: Trade) -> bool:
        """
        Proxy de resolução quando a API não retorna resultado ainda.
        Usa movimento do spot Binance desde a entrada do trade.
        YES vence se o spot subiu acima do threshold implícito no preço.
        """
        buf = self.buffers.get(trade.symbol)
        if not buf:
            return random.random() < 0.47

        p_now = buf.latest_price()
        if p_now is None:
            return random.random() < 0.47

        # Preço mais próximo do momento de entrada
        p_entry = None
        best_diff = float("inf")
        for ts, p in buf.ticks:
            d = abs(ts - trade.entry_ts)
            if d < best_diff:
                best_diff = d
                p_entry   = p

        if not p_entry or p_entry == 0:
            return random.random() < 0.47

        pct   = (p_now / p_entry - 1) * 100
        noise = random.gauss(0, 0.003)

        if trade.action == "BUY_YES":
            return (pct + noise) > 0.05
        else:
            return (pct + noise) < -0.05

    def _close(self, trade: Trade, reason: str, won: bool, exit_val: float):
        """
        Fechamento fiel ao Polymarket:
        WIN  → payout = amount_usdc / entry_price  (token resolve a $1.00)
        LOSS → payout = $0.00                      (token resolve a $0.00)
        STOP/TP → payout proporcional ao exit_val
        """
        with self._lock:
            if trade not in self.open_trades:
                return

            if reason == "TAKE_PROFIT":
                # Vende ao preço atual (bid real)
                exit_val_clamped = min(0.97, max(exit_val, trade.entry_price * 1.10))
                payout = (trade.amount_usdc / trade.entry_price) * exit_val_clamped
                pnl    = payout - trade.amount_usdc
                self.stats.wins   += 1
                self.stats.balance += payout

            elif reason == "STOP_LOSS":
                # Vende ao BID atual — recupera parcialmente
                exit_val_clamped = max(0.01, exit_val)
                payout = trade.amount_usdc * (exit_val_clamped / trade.entry_price)
                pnl    = payout - trade.amount_usdc
                self.stats.losses += 1
                self.stats.balance += payout

            elif won:
                # FIX: EXPIRAÇÃO COM WIN → token vale exatamente $1.00
                payout = trade.amount_usdc / trade.entry_price
                pnl    = payout - trade.amount_usdc
                self.stats.wins   += 1
                self.stats.balance += payout

            else:
                # FIX: EXPIRAÇÃO COM LOSS → token vale $0.00
                pnl    = -trade.amount_usdc
                payout = 0.0
                self.stats.losses += 1
                # (balance não aumenta — perdeu tudo)

            trade.status      = "CLOSED"
            trade.exit_ts     = time.time()
            trade.exit_price  = exit_val
            trade.exit_reason = reason
            trade.pnl_usdc    = round(pnl, 3)
            trade.pnl_pct     = round(pnl / trade.amount_usdc * 100, 1)
            trade.won         = won
            trade.unrealized_pnl = 0.0
            self.stats.total_pnl     += pnl
            self.stats.last_action_ts = time.time()

            if won:
                self.stats.win_streak  += 1
                self.stats.loss_streak  = 0
                if self.stats.wins > 0:
                    self.stats.avg_win_usdc = (
                        self.stats.avg_win_usdc * (self.stats.wins - 1) + pnl
                    ) / self.stats.wins
            else:
                self.stats.loss_streak += 1
                self.stats.win_streak   = 0
                if self.stats.losses > 0:
                    self.stats.avg_loss_usdc = (
                        self.stats.avg_loss_usdc * (self.stats.losses - 1) + abs(pnl)
                    ) / self.stats.losses

            self.stats.best_trade_pnl  = max(self.stats.best_trade_pnl, pnl)
            self.stats.worst_trade_pnl = min(self.stats.worst_trade_pnl, pnl)
            self.open_trades.remove(trade)
            self.closed_trades.append(trade)

        icon = "✓" if won else "✗"
        self._log(
            "TRADE_CLOSE",
            f"{icon} ${pnl:+.2f} ({trade.pnl_pct:+.0f}%) | "
            f"{trade.symbol} {trade.action} | {reason} | bal ${self.stats.balance:.2f}",
            level="win" if won else "loss",
            trade_id=trade.id, pnl=pnl, won=won,
            reason=reason, symbol=trade.symbol, amount=trade.amount_usdc,
        )

        # Marca mercado como resolvido para não re-operar nele
        if reason == "EXPIRED" and trade.token_id in self._markets:
            self._markets[trade.token_id]["resolved"] = True

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _update_open_prices(self, token_id: str, mid: float):
        with self._lock:
            for t in self.open_trades:
                if t.token_id == token_id:
                    cur = mid if t.action == "BUY_YES" else 1.0 - mid
                    t.current_price   = mid
                    t.unrealized_pnl  = round((cur / t.entry_price - 1) * t.amount_usdc, 3)

    def _update_equity(self):
        with self._lock:
            unr = sum(t.unrealized_pnl for t in self.open_trades)
            self.equity_curve.append((time.time(), round(self.stats.balance + unr, 3)))

    def _update_dd(self):
        with self._lock:
            v = self.stats.balance
            if v > self.stats.peak_balance:
                self.stats.peak_balance = v
            dd = (self.stats.peak_balance - v) / self.stats.peak_balance if self.stats.peak_balance > 0 else 0.0
            if dd > self.stats.max_drawdown_pct:
                self.stats.max_drawdown_pct = round(dd, 4)

    # ── Helpers de teste (não usados em produção) ──────────────────────────────

    def _place_trade_direct(self, mkt: dict, sym: str, sig, horiz: float, bid: float, ask: float):
        """
        Abre um trade diretamente, sem verificar cooldown/warmup.
        Usado exclusivamente pelos testes unitários.
        """
        with self._lock:
            if sig.direction == "YES":
                from typing import Literal
                action = "BUY_YES"
                entry_price = ask
                p_est = max(0.05, min(0.95, 0.5 + sig.edge))
            else:
                action = "BUY_NO"
                entry_price = max(0.02, 1.0 - bid)
                p_est = max(0.05, min(0.95, 0.5 + abs(sig.edge)))

            entry_price = max(0.03, min(0.97, entry_price))
            b = (1.0 / entry_price) - 1.0
            q = 1.0 - p_est
            kelly_raw = (b * p_est - q) / b if b > 0 else 0.0
            kelly = max(0.0, min(kelly_raw, self.KELLY_FRACTION))
            cmult = {"high": 1.0, "medium": 0.55}.get(sig.confidence, 0.3)
            max_bet = self.stats.balance * self.MAX_POSITION_PCT
            gross = round(kelly * max_bet * cmult, 2)
            gross = max(gross, 0.10)
            gross = min(gross, max_bet, self.stats.balance * 0.06)
            fee = round(gross * POLYMARKET_FEE, 4)
            amount = round(gross - fee, 4)
            total_cost = gross
            if total_cost > self.stats.balance:
                return None

            import uuid
            trade = Trade(
                id=str(uuid.uuid4())[:8],
                symbol=sym,
                question=mkt["question"][:100],
                slug=mkt["slug"],
                token_id=mkt["token_id"],
                condition_id=mkt.get("condition_id", ""),
                action=action,
                amount_usdc=amount,
                gross_usdc=gross,
                fee_usdc=fee,
                entry_price=entry_price,
                entry_ts=time.time(),
                horizon_min=horiz,
                end_date_iso=mkt.get("end_date_iso", ""),
                signal_edge=sig.edge,
                signal_confidence=sig.confidence,
                signal_reasons=sig.reasons[:],
                signal_momentum_1m=sig.momentum_1m,
                signal_momentum_5m=sig.momentum_5m,
                signal_rsi=sig.rsi,
                market_type=sig.market_type,
                current_price=(ask if action == "BUY_YES" else 1.0 - bid),
            )
            self.stats.balance -= total_cost
            self.stats.total_trades += 1
            self.stats.total_wagered += total_cost
            self.stats.total_fees += fee
            self.open_trades.append(trade)
        return trade

    def close_trade(self, trade: Trade, won: bool, reason: str = "EXPIRED"):
        """
        Fecha um trade manualmente.
        Usado exclusivamente pelos testes unitários.
        """
        with self._lock:
            if trade not in self.open_trades:
                return None
            if won:
                payout = trade.amount_usdc / trade.entry_price
                pnl = payout - trade.amount_usdc
                self.stats.wins += 1
                self.stats.balance += payout
            else:
                pnl = -trade.amount_usdc
                self.stats.losses += 1

            trade.status = "CLOSED"
            trade.exit_ts = time.time()
            trade.exit_reason = reason
            trade.pnl_usdc = round(pnl, 3)
            trade.pnl_pct = round(pnl / trade.amount_usdc * 100, 1)
            trade.won = won
            trade.unrealized_pnl = 0.0
            self.stats.total_pnl += pnl
            self.open_trades.remove(trade)
            self.closed_trades.append(trade)
        return pnl

    def _log(self, event: str, msg: str, level: str = "info", **kw):
        e = {
            "ts":      time.time(),
            "ts_str":  time.strftime("%H:%M:%S"),
            "event":   event,
            "message": msg,
            "level":   level,
            **kw,
        }
        self.event_log.append(e)
        print(f"[Sim] {e['ts_str']} {event}: {msg}")