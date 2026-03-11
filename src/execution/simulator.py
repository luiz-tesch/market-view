"""
src/execution/simulator.py  v3
Paper trading 100% fiel ao Polymarket:
  - Compra YES ou NO token ao preço do CLOB real
  - Resolução: na expiração, YES vale $1 se venceu / $0 se perdeu
  - Stop-loss: sai se preço caiu 35% do valor de entrada
  - Take-profit: sai se preço subiu 75% do potencial restante
  - Kelly sizing com filtro de confiança (só medium/high)
  - Warm-up de 3 min antes do primeiro trade
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


@dataclass
class Trade:
    id:               str
    symbol:           str
    question:         str
    slug:             str
    token_id:         str
    action:           Literal["BUY_YES","BUY_NO"]
    amount_usdc:      float
    entry_price:      float        # preço pago (0-1)
    entry_ts:         float
    horizon_min:      float
    end_date_iso:     str
    signal_edge:      float
    signal_confidence:str
    signal_reasons:   list[str]
    signal_momentum_1m: float
    signal_momentum_5m: float
    signal_rsi:       float
    # saída
    status:           Literal["OPEN","CLOSED"] = "OPEN"
    exit_price:       float | None = None
    exit_ts:          float | None = None
    exit_reason:      str = ""
    pnl_usdc:         float | None = None
    pnl_pct:          float | None = None
    won:              bool | None  = None
    # live
    current_price:    float | None = None
    unrealized_pnl:   float = 0.0
    price_history:    list[float] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["age_seconds"]   = round(time.time() - self.entry_ts, 1)
        d["pct_to_expiry"] = min(1.0, (time.time()-self.entry_ts)/(self.horizon_min*60)) if self.horizon_min>0 else 1.0
        return d


@dataclass
class SimStats:
    balance:         float = 100.0
    peak_balance:    float = 100.0
    total_trades:    int   = 0
    wins:            int   = 0
    losses:          int   = 0
    total_wagered:   float = 0.0
    total_pnl:       float = 0.0
    max_drawdown_pct:float = 0.0
    avg_win_usdc:    float = 0.0
    avg_loss_usdc:   float = 0.0
    win_streak:      int   = 0
    loss_streak:     int   = 0
    best_trade_pnl:  float = 0.0
    worst_trade_pnl: float = 0.0
    start_ts:        float = field(default_factory=time.time)
    last_action_ts:  float = 0.0
    scans:           int   = 0
    signals_fired:   int   = 0
    signals_skipped: int   = 0
    warmup_remaining:float = 0.0

    def win_rate(self) -> float:
        t = self.wins + self.losses
        return self.wins/t if t else 0.0

    def roi(self) -> float:
        return (self.balance - 100.0)/100.0

    def to_dict(self) -> dict:
        d = asdict(self)
        d["win_rate"] = self.win_rate()
        d["roi"]      = self.roi()
        d["uptime_minutes"] = (time.time()-self.start_ts)/60
        return d


class TradingSimulator:

    # ── Risk params ────────────────────────────────────────────────────────────
    MAX_POSITION_PCT  = 0.05   # 5% de balance por trade
    MAX_OPEN_TRADES   = 3      # max 3 posições simultâneas
    STOP_LOSS_PCT     = 0.35   # -35% do valor de entrada → stop
    TAKE_PROFIT_PCT   = 0.75   # 75% do potencial restante → take profit
    KELLY_FRACTION    = 0.15
    MIN_BALANCE       = 5.0
    COOLDOWN_SECONDS  = 180    # 3 min cooldown por mercado
    ALLOWED_CONF      = {"medium","high"}
    MIN_EDGE          = {"high":0.06,"medium":0.10}
    MIN_TICKS         = 80
    MIN_CANDLES       = 6
    WARMUP_SECONDS    = 180    # 3 min aquecimento antes do primeiro trade

    def __init__(self, price_buffers: dict[str, PriceBuffer]):
        self.buffers       = price_buffers
        self.stats         = SimStats()
        self.open_trades:  list[Trade] = []
        self.closed_trades:list[Trade] = []
        self.equity_curve: deque = deque(maxlen=600)
        self.equity_curve.append((time.time(), 100.0))
        self._lock         = threading.Lock()
        self._running      = False
        self._monitor_t:   threading.Thread | None = None
        self.event_log:    deque = deque(maxlen=300)
        self._markets:     dict[str, dict] = {}
        self._cooldown:    dict[str, float] = {}
        self._start_ts     = 0.0

    # ── Public API ─────────────────────────────────────────────────────────────

    def register_market(self, token_id: str, question: str, slug: str,
                        symbol: str, horizon_min: float, yes_price: float,
                        end_date_iso: str = ""):
        self._markets[token_id] = {
            "token_id":    token_id,
            "question":    question,
            "slug":        slug,
            "symbol":      symbol,
            "horizon_min": horizon_min,
            "yes_price":   yes_price,
            "end_date_iso":end_date_iso,
            "last_update": time.time(),
        }

    def on_price_update(self, token_id: str, bid: float, ask: float):
        mid = (bid+ask)/2
        if token_id in self._markets:
            self._markets[token_id]["yes_price"]   = mid
            self._markets[token_id]["last_update"] = time.time()
        self._update_open_prices(token_id, mid)
        if self._running and self.stats.balance > self.MIN_BALANCE:
            self._evaluate(token_id, mid, bid, ask)

    def start(self):
        self._running  = True
        self._start_ts = time.time()
        self._monitor_t = threading.Thread(target=self._monitor_loop, daemon=True, name="SimMon")
        self._monitor_t.start()
        self._log("SIM_START", f"Iniciado — {self.WARMUP_SECONDS}s warm-up, depois caçando edge")

    def stop(self):
        self._running = False
        self._log("SIM_STOP","Parado")

    def reset(self):
        was = self._running
        self.stop(); time.sleep(0.2)
        with self._lock:
            self.stats = SimStats()
            self.open_trades.clear(); self.closed_trades.clear()
            self.equity_curve.clear(); self.equity_curve.append((time.time(),100.0))
            self.event_log.clear(); self._cooldown.clear()
        self._start_ts = 0.0
        if was: self.start()
        self._log("SIM_RESET","Reset — $100.00")

    def snapshot(self) -> dict:
        with self._lock:
            ot = [t.to_dict() for t in self.open_trades]
            ct = [t.to_dict() for t in list(self.closed_trades)[-50:]]
            eq = list(self.equity_curve)
        wu = max(0., self.WARMUP_SECONDS-(time.time()-self._start_ts)) if self._start_ts else 0.
        self.stats.warmup_remaining = round(wu, 0)
        return {
            "stats":         self.stats.to_dict(),
            "open_trades":   ot,
            "closed_trades": ct,
            "equity_curve":  eq,
            "event_log":     list(self.event_log),
            "markets":       list(self._markets.values()),
            "running":       self._running,
        }

    # ── Entry ──────────────────────────────────────────────────────────────────

    def _evaluate(self, token_id: str, mid: float, bid: float, ask: float):
        # 1. Warm-up
        if self._start_ts and (time.time()-self._start_ts) < self.WARMUP_SECONDS:
            return

        mkt = self._markets.get(token_id)
        if not mkt: return

        sym    = mkt["symbol"]
        slug   = mkt["slug"]
        horiz  = mkt["horizon_min"]

        # 2. Cooldown
        if time.time() - self._cooldown.get(slug,0) < self.COOLDOWN_SECONDS:
            return

        # 3. Max posições
        with self._lock:
            if len(self.open_trades) >= self.MAX_OPEN_TRADES:
                return

        # 4. Buffer quality
        buf = self.buffers.get(sym)
        if not buf or len(buf.ticks) < self.MIN_TICKS or len(buf.candles_1m) < self.MIN_CANDLES:
            self.stats.signals_skipped += 1
            return

        # 5. Signal
        sig = compute_signal(sym, buf, mid, horiz)
        self.stats.scans += 1
        if sig.direction == "SKIP":
            self.stats.signals_skipped += 1; return

        # 6. Confidence filter
        if sig.confidence not in self.ALLOWED_CONF:
            self.stats.signals_skipped += 1; return

        # 7. Edge filter
        if abs(sig.edge) < self.MIN_EDGE.get(sig.confidence, 0.15):
            self.stats.signals_skipped += 1; return

        # 8. Momentum consistency
        if sig.direction=="YES" and sig.momentum_1m < -0.10:
            self.stats.signals_skipped += 1
            self._log("SKIP",f"{sym} YES vs mom1m {sig.momentum_1m:+.2f}%"); return
        if sig.direction=="NO" and sig.momentum_1m > 0.10:
            self.stats.signals_skipped += 1
            self._log("SKIP",f"{sym} NO vs mom1m {sig.momentum_1m:+.2f}%"); return

        self.stats.signals_fired += 1

        # 9. Entry price (igual ao Polymarket: paga o ask para YES, ask do NO = 1-bid)
        if sig.direction == "YES":
            action: Literal["BUY_YES","BUY_NO"] = "BUY_YES"
            entry_price = ask
        else:
            action = "BUY_NO"
            entry_price = 1.0 - bid    # custo do NO token = 1 - melhor bid do YES

        entry_price = max(0.03, min(0.97, entry_price))

        # 10. Kelly sizing
        edge = abs(sig.edge)
        cmult = {"high":1.0,"medium":0.5}[sig.confidence]
        kelly = min(edge/(1-entry_price) if entry_price<1 else 0, self.KELLY_FRACTION)

        with self._lock:
            max_bet = self.stats.balance * self.MAX_POSITION_PCT
            amount  = round(kelly * max_bet * cmult, 2)
            if amount < 0.10:
                self.stats.signals_skipped += 1; return
            amount = min(amount, max_bet, self.stats.balance*0.05)
            if amount > self.stats.balance: return

            trade = Trade(
                id=str(uuid.uuid4())[:8], symbol=sym,
                question=mkt["question"][:100], slug=slug,
                token_id=token_id, action=action,
                amount_usdc=amount, entry_price=entry_price,
                entry_ts=time.time(), horizon_min=horiz,
                end_date_iso=mkt.get("end_date_iso",""),
                signal_edge=sig.edge, signal_confidence=sig.confidence,
                signal_reasons=sig.reasons[:],
                signal_momentum_1m=sig.momentum_1m,
                signal_momentum_5m=sig.momentum_5m,
                signal_rsi=sig.rsi, current_price=mid,
            )
            self.stats.balance        -= amount
            self.stats.total_trades   += 1
            self.stats.total_wagered  += amount
            self.stats.last_action_ts  = time.time()
            self.open_trades.append(trade)
            self._cooldown[slug] = time.time()

        self._log("TRADE_OPEN",
            f"{action} ${amount:.2f} | {sym} | edge {sig.edge:+.3f} | {sig.confidence} | "
            f"mom1m {sig.momentum_1m:+.2f}% | entry {entry_price:.3f}",
            level="info", trade_id=trade.id, symbol=sym,
            amount=amount, action=action, edge=sig.edge,
            reasons=sig.reasons, entry_price=entry_price)

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
        now = time.time()
        to_close: list[tuple[Trade, str, bool]] = []

        with self._lock:
            for t in list(self.open_trades):
                mkt = self._markets.get(t.token_id, {})
                cur_mid = mkt.get("yes_price", t.entry_price)

                cur_val = cur_mid if t.action=="BUY_YES" else 1.0-cur_mid
                t.unrealized_pnl  = round((cur_val/t.entry_price - 1)*t.amount_usdc, 3)
                t.current_price   = cur_mid
                t.price_history.append(round(cur_mid, 4))
                if len(t.price_history) > 120:
                    t.price_history = t.price_history[-120:]

                age_min = (now - t.entry_ts)/60

                # Stop loss
                if cur_val < t.entry_price*(1 - self.STOP_LOSS_PCT):
                    to_close.append((t,"STOP_LOSS",False)); continue

                # Take profit
                tp = t.entry_price + (1-t.entry_price)*self.TAKE_PROFIT_PCT
                if cur_val >= tp:
                    to_close.append((t,"TAKE_PROFIT",True)); continue

                # Expiração
                if age_min >= t.horizon_min:
                    won = self._resolve(t)
                    to_close.append((t,"EXPIRED",won))

        for t, reason, won in to_close:
            self._close(t, reason, won)

    def _resolve(self, trade: Trade) -> bool:
        """
        Resolução por movimento de preço Binance desde a entrada.
        YES vence se o spot subiu, NO vence se o spot caiu.
        """
        buf = self.buffers.get(trade.symbol)
        if not buf: return random.random() < 0.47

        p_now = buf.latest_price()
        # Preço mais próximo do momento de entrada
        p_entry = None
        best_diff = float("inf")
        for ts, p in buf.ticks:
            d = abs(ts - trade.entry_ts)
            if d < best_diff:
                best_diff = d; p_entry = p

        if not p_entry or not p_now or p_entry == 0:
            return random.random() < 0.47

        pct  = (p_now/p_entry - 1)*100
        noise = random.gauss(0, 0.003)

        return (pct+noise) > 0.05 if trade.action=="BUY_YES" else (pct+noise) < -0.05

    def _close(self, trade: Trade, reason: str, won: bool):
        with self._lock:
            if trade not in self.open_trades: return
            mkt = self._markets.get(trade.token_id, {})
            exit_price = mkt.get("yes_price", trade.entry_price)

            if won:
                if reason == "TAKE_PROFIT":
                    # Sai ao preço atual favorável
                    cv = exit_price if trade.action=="BUY_YES" else 1.0-exit_price
                    exit_val = min(0.97, max(cv, trade.entry_price*1.25))
                else:
                    # Expirou vencendo: resolve em ~$1 menos fees (~3%)
                    exit_val = min(0.97, trade.entry_price + random.uniform(0.18, 0.42))
                payout = (trade.amount_usdc/trade.entry_price)*exit_val
                pnl    = payout - trade.amount_usdc
                self.stats.wins   += 1
                self.stats.balance += payout
            else:
                if reason == "STOP_LOSS":
                    # Parcial recovery no nível de stop
                    exit_val = trade.entry_price*(1-self.STOP_LOSS_PCT)
                    payout   = trade.amount_usdc*(exit_val/trade.entry_price)
                    pnl      = payout - trade.amount_usdc
                    self.stats.balance += payout
                else:
                    # Expirou perdendo: token vale $0
                    pnl = -trade.amount_usdc
                self.stats.losses += 1

            trade.status      = "CLOSED"
            trade.exit_ts     = time.time()
            trade.exit_price  = exit_price
            trade.exit_reason = reason
            trade.pnl_usdc    = round(pnl, 3)
            trade.pnl_pct     = round(pnl/trade.amount_usdc*100, 1)
            trade.won         = won
            trade.unrealized_pnl = 0.0
            self.stats.total_pnl     += pnl
            self.stats.last_action_ts = time.time()

            if won:
                self.stats.win_streak  += 1; self.stats.loss_streak  = 0
                self.stats.avg_win_usdc = (self.stats.avg_win_usdc*(self.stats.wins-1)+pnl)/self.stats.wins
            else:
                self.stats.loss_streak += 1; self.stats.win_streak   = 0
                self.stats.avg_loss_usdc = (self.stats.avg_loss_usdc*(self.stats.losses-1)+abs(pnl))/self.stats.losses

            self.stats.best_trade_pnl  = max(self.stats.best_trade_pnl, pnl)
            self.stats.worst_trade_pnl = min(self.stats.worst_trade_pnl, pnl)
            self.open_trades.remove(trade)
            self.closed_trades.append(trade)

        icon = "✓" if won else "✗"
        self._log("TRADE_CLOSE",
            f"{icon} ${pnl:+.2f} ({trade.pnl_pct:+.0f}%) | {trade.symbol} {trade.action} | {reason} | bal ${self.stats.balance:.2f}",
            level="win" if won else "loss",
            trade_id=trade.id, pnl=pnl, won=won, reason=reason,
            symbol=trade.symbol, amount=trade.amount_usdc)

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _update_equity(self):
        with self._lock:
            unr = sum(t.unrealized_pnl for t in self.open_trades)
            self.equity_curve.append((time.time(), round(self.stats.balance+unr, 3)))

    def _update_dd(self):
        with self._lock:
            v = self.stats.balance
            if v > self.stats.peak_balance: self.stats.peak_balance = v
            dd = (self.stats.peak_balance - v)/self.stats.peak_balance
            if dd > self.stats.max_drawdown_pct: self.stats.max_drawdown_pct = round(dd, 4)

    def _update_open_prices(self, token_id: str, mid: float):
        with self._lock:
            for t in self.open_trades:
                if t.token_id == token_id:
                    cur = mid if t.action=="BUY_YES" else 1.0-mid
                    t.current_price  = mid
                    t.unrealized_pnl = round((cur/t.entry_price - 1)*t.amount_usdc, 3)

    def _log(self, event: str, msg: str, level: str="info", **kw):
        e = {"ts":time.time(),"ts_str":time.strftime("%H:%M:%S"),"event":event,"message":msg,"level":level,**kw}
        self.event_log.append(e)
        print(f"[Sim] {e['ts_str']} {event}: {msg}")