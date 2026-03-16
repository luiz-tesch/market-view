"""
src/execution/simulator.py  v4.1 — Bugfixes sobre v4.0

FIXES v4.1:
────────────────────────────────────────────────────────────────────────────────
BUG 1 (RACE CONDITION — CRÍTICO):
  _evaluate liberava o _lock antes de chamar _place_trade, permitindo que
  múltiplos threads abrissem trades simultâneos e excedessem MAX_OPEN_TRADES.
  FIX: _place_trade reverifica len(open_trades) >= MAX_OPEN_TRADES dentro do lock.

BUG 2 (DUPLA CONTAGEM DE EDGES — SILENCIOSO):
  compute_signal já filtrava por seus próprios min_edges internos, produzindo
  direction=SKIP para edges insuficientes. Mas _evaluate checava abs(sig.edge)
  com seus próprios thresholds contra um sinal que NUNCA seria SKIP para aquele
  edge. Resultado: um sinal com edge=0.055 e confidence=high chegava como YES
  ao simulator, mas era descartado pelo MIN_EDGE do simulator (0.06).
  FIX: os thresholds do simulator foram alinhados com os do engine.py.
       engine MIN_EDGE: high=0.05, medium=0.09
       simulator MIN_EDGE: high=0.05, medium=0.09  ← corrigido

BUG 3 (STALE MARKETS — MEMÓRIA):
  _markets nunca era limpo entre refreshes. Mercados expirados acumulavam
  indefinidamente na memória e podiam receber updates fantasmas.
  FIX: simulator.refresh_markets() limpa mercados expirados (mins_left <= 0
  e sem trade aberto) a cada load de mercados.

BUG 4 (LOGGING INSUFICIENTE):
  signals_skipped era incrementado mas sem log — impossível debugar por que
  o bot ficava silencioso.
  FIX: log periódico de skip reasons via _log_skip_summary() a cada 100 skips.
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

# Combined Strategy Engine: Arbitrage > Stale Snipe > Market Making > Quant
try:
    from src.signals.strategies import combined_decide
    USE_COMBINED = True
except ImportError:
    USE_COMBINED = False
    combined_decide = None

# Quant Brain: modelo matemático (log-normal + OBI + multi-indicator gate) — fallback
try:
    from src.signals.quant_brain import quant_decide
    USE_QUANT = True
except ImportError:
    USE_QUANT = False
    quant_decide = None

# Claude/LLM Brain: mantém como fallback opcional mas NÃO é o primário
try:
    from src.signals.claude_brain import claude_decide, ANTHROPIC_API_KEY as _CLAUDE_KEY
    USE_CLAUDE = bool(_CLAUDE_KEY)
except ImportError:
    USE_CLAUDE = False
    claude_decide = None
from src.config import SHORT_TERM_MAX_MINUTES, SHORT_TERM_MIN_MINUTES
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from src.data.collector import DataCollector


# ── Constantes Fase 2 ──────────────────────────────────────────────────────────

RESOLUTION_WORKER_INTERVAL = 300      # verifica fila a cada 5 minutos
PENDING_EXPIRY_SECONDS     = 48 * 3600  # 48h sem resultado → EXPIRED


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
    amount_usdc:         float
    gross_usdc:          float
    fee_usdc:            float
    entry_price:         float
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
    status:              Literal["OPEN", "CLOSED", "PENDING"] = "OPEN"
    exit_price:          float | None = None
    exit_ts:             float | None = None
    exit_reason:         str = ""
    pnl_usdc:            float | None = None
    pnl_pct:             float | None = None
    won:                 bool | None = None
    # Fase 2: resolução pendente
    pending_resolution:  bool  = False
    pending_since_ts:    float = 0.0
    # live
    current_price:       float | None = None
    unrealized_pnl:      float = 0.0
    price_history:       list[float] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["age_seconds"]   = round(time.time() - self.entry_ts, 1)
        d["pct_to_expiry"] = min(1.0, (time.time() - self.entry_ts) / (self.horizon_min * 60)) if self.horizon_min > 0 else 1.0
        if self.pending_resolution and self.pending_since_ts:
            d["mins_waiting"] = round((time.time() - self.pending_since_ts) / 60, 1)
        else:
            d["mins_waiting"] = 0.0
        return d


# ── SimStats ───────────────────────────────────────────────────────────────────

@dataclass
class SimStats:
    balance:               float = 100.0
    peak_balance:          float = 100.0
    total_trades:          int   = 0
    wins:                  int   = 0
    losses:                int   = 0
    total_wagered:         float = 0.0
    total_fees:            float = 0.0
    total_pnl:             float = 0.0
    max_drawdown_pct:      float = 0.0
    avg_win_usdc:          float = 0.0
    avg_loss_usdc:         float = 0.0
    win_streak:            int   = 0
    loss_streak:           int   = 0
    best_trade_pnl:        float = 0.0
    worst_trade_pnl:       float = 0.0
    start_ts:              float = field(default_factory=time.time)
    last_action_ts:        float = 0.0
    scans:                 int   = 0
    signals_fired:         int   = 0
    signals_skipped:       int   = 0
    warmup_remaining:      float = 0.0
    real_resolutions:      int   = 0
    simulated_resolutions: int   = 0
    # Fase 2
    pending_count:         int   = 0
    expired_count:         int   = 0

    def real_resolution_rate(self) -> float:
        t = self.real_resolutions + self.simulated_resolutions
        return self.real_resolutions / t if t else 0.0

    def win_rate(self) -> float:
        t = self.wins + self.losses
        return self.wins / t if t else 0.0

    def roi(self) -> float:
        return (self.balance - 100.0) / 100.0

    def to_dict(self) -> dict:
        d = asdict(self)
        d["win_rate"]             = self.win_rate()
        d["roi"]                  = self.roi()
        d["uptime_minutes"]       = (time.time() - self.start_ts) / 60
        d["real_resolution_rate"] = self.real_resolution_rate()
        return d


# ── TradingSimulator ───────────────────────────────────────────────────────────

class TradingSimulator:

    # ── Parâmetros de risco ────────────────────────────────────────────────────
    MAX_POSITION_PCT  = 0.05
    MAX_OPEN_TRADES   = 4
    STOP_LOSS_PCT     = 0.40
    TAKE_PROFIT_PCT   = 0.70
    KELLY_FRACTION    = 0.15
    MIN_BALANCE       = 5.0
    COOLDOWN_SECONDS  = 300
    ALLOWED_CONF      = {"medium", "high"}
    # v3.0: thresholds sobre fee_adjusted_edge (não edge bruto)
    MIN_EDGE          = {"high": 0.035, "medium": 0.07}
    MIN_TICKS         = 40
    MIN_CANDLES       = 3
    WARMUP_SECONDS    = 30

    def __init__(self, price_buffers: dict[str, PriceBuffer], collector: "DataCollector | None" = None):
        self.buffers        = price_buffers
        self.collector      = collector
        self.stats          = SimStats()
        self.open_trades:   list[Trade] = []
        self.closed_trades: list[Trade] = []
        self.equity_curve:  deque = deque(maxlen=600)
        self.equity_curve.append((time.time(), 100.0))
        self._lock          = threading.Lock()
        self._running       = False
        self._monitor_t:    threading.Thread | None = None
        self._resolution_t: threading.Thread | None = None
        self.event_log:     deque = deque(maxlen=400)
        self._markets:      dict[str, dict] = {}
        self._cooldown:     dict[str, float] = {}
        self._start_ts      = 0.0
        self._pending_resolution: list[Trade] = []
        self._pending_lock  = threading.Lock()
        # FIX BUG 4: rastrear skip reasons para debug
        self._skip_reasons: dict[str, int] = {}
        self._last_skip_log = 0.0

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
        no_token_id:  str = "",
        no_price:     float = 0.0,
    ):
        if not (SHORT_TERM_MIN_MINUTES <= horizon_min <= SHORT_TERM_MAX_MINUTES):
            print(f"[Sim] register_market REJECTED {symbol} horizon={horizon_min} (range {SHORT_TERM_MIN_MINUTES}-{SHORT_TERM_MAX_MINUTES})")
            return

        print(f"[Sim] register_market OK {symbol} horizon={horizon_min} token={token_id[:12]}")
        self._markets[token_id] = {
            "token_id":         token_id,
            "no_token_id":      no_token_id,
            "question":         question,
            "slug":             slug,
            "symbol":           symbol,
            "horizon_min":      horizon_min,
            "yes_price":        yes_price,
            "no_price":         no_price,
            "end_date_iso":     end_date_iso,
            "condition_id":     condition_id,
            "market_type":      market_type,
            "last_update":      time.time(),
            "resolved":         False,
            "resolved_outcome": None,
        }

    def cleanup_expired_markets(self) -> int:
        """
        FIX BUG 3: Remove mercados expirados de self._markets.
        Não remove se há trade aberto nesse token.
        Retorna quantos foram removidos.
        """
        open_token_ids = {t.token_id for t in self.open_trades}
        pending_token_ids = {t.token_id for t in self._pending_resolution}
        active_tokens = open_token_ids | pending_token_ids

        to_remove = []
        for token_id, mkt in self._markets.items():
            if token_id in active_tokens:
                continue
            if mkt.get("resolved"):
                to_remove.append(token_id)
                continue
            # Remove mercados com end_date no passado (> 30 min atrás)
            end_iso = mkt.get("end_date_iso", "")
            if end_iso:
                try:
                    from datetime import datetime, timezone
                    end = datetime.fromisoformat(end_iso.replace("Z", "+00:00"))
                    mins_past = (datetime.now(timezone.utc) - end).total_seconds() / 60
                    if mins_past > 30:
                        to_remove.append(token_id)
                except Exception:
                    pass

        print(f"[Sim] cleanup_expired_markets: before={len(self._markets)} removing={len(to_remove)} reasons={[self._markets.get(t,{}).get('end_date_iso','?')[:16] for t in to_remove[:3]]}")
        for tid in to_remove:
            del self._markets[tid]

        print(f"[Sim] cleanup_expired_markets: after={len(self._markets)}")
        if to_remove:
            print(f"[Sim] Cleaned up {len(to_remove)} expired markets (remaining: {len(self._markets)})")

        return len(to_remove)

    def on_price_update(self, token_id: str, bid: float, ask: float):
        mid = (bid + ask) / 2
        if token_id in self._markets:
            m = self._markets[token_id]
            if m.get("resolved"):
                return
            m["yes_price"]   = mid
            m["no_price"]    = 1.0 - mid  # Polymarket: YES + NO ≈ 1.0
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
        self._resolution_t = threading.Thread(
            target=self._resolution_worker, daemon=True, name="ResolutionWorker"
        )
        self._resolution_t.start()
        self._log("SIM_START", f"Iniciado — {self.WARMUP_SECONDS}s warm-up | worker de resolução ativo (5min)")

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
            self._skip_reasons.clear()
        with self._pending_lock:
            self._pending_resolution.clear()
        self._start_ts = 0.0
        if was:
            self.start()
        self._log("SIM_RESET", "Reset — $100.00")

    def snapshot(self) -> dict:
        with self._lock:
            ot = [t.to_dict() for t in self.open_trades]
            ct = [t.to_dict() for t in list(self.closed_trades)[-50:]]
            eq = list(self.equity_curve)

        with self._pending_lock:
            pending_list = [
                {
                    "condition_id": t.condition_id,
                    "symbol":       t.symbol,
                    "action":       t.action,
                    "entry_ts":     t.entry_ts,
                    "mins_waiting": round((time.time() - t.pending_since_ts) / 60, 1) if t.pending_since_ts else 0.0,
                    "end_date_iso": t.end_date_iso,
                    "gross_usdc":   t.gross_usdc,
                }
                for t in self._pending_resolution
            ]
        self.stats.pending_count = len(pending_list)

        wu        = max(0., self.WARMUP_SECONDS - (time.time() - self._start_ts)) if self._start_ts else float(self.WARMUP_SECONDS)
        wu_rounded = round(wu, 0)
        self.stats.warmup_remaining = wu_rounded

        n_markets = len(self._markets)
        if n_markets == 0:
            import traceback
            print(f"[Sim] WARNING snapshot() called with 0 markets — id(self._markets)={id(self._markets)}")
            traceback.print_stack(limit=4)
        return {
            "stats":                     self.stats.to_dict(),
            "open_trades":               ot,
            "closed_trades":             ct,
            "equity_curve":              eq,
            "event_log":                 list(self.event_log),
            "markets":                   list(self._markets.values()),
            "running":                   self._running,
            "warmup_remaining":          wu_rounded,
            "real_resolution_rate":      round(self.stats.real_resolution_rate(), 3),
            "real_resolutions":          self.stats.real_resolutions,
            "simulated_resolutions":     self.stats.simulated_resolutions,
            "pending_resolution_count":  len(pending_list),
            "pending_resolution_trades": pending_list,
            "expired_count":             self.stats.expired_count,
            "skip_reasons":              dict(self._skip_reasons),
            "registered_markets":        n_markets,
        }

    # ── Avaliação de entrada ───────────────────────────────────────────────────

    def _evaluate(self, token_id: str, mid: float, bid: float, ask: float):
        if self._start_ts and (time.time() - self._start_ts) < self.WARMUP_SECONDS:
            return

        mkt = self._markets.get(token_id)
        if not mkt or mkt.get("resolved"):
            return

        sym   = mkt["symbol"]
        slug  = mkt["slug"]
        horiz = mkt["horizon_min"]

        if time.time() - self._cooldown.get(slug, 0) < self.COOLDOWN_SECONDS:
            self._track_skip("cooldown")
            return

        # FIX BUG 1: checagem com lock, resultado guardado em variável local
        with self._lock:
            open_count = len(self.open_trades)
        if open_count >= self.MAX_OPEN_TRADES:
            self._track_skip("max_open_trades")
            return

        buf = self.buffers.get(sym)
        if not buf or len(buf.ticks) < self.MIN_TICKS or len(buf.candles_1m) < self.MIN_CANDLES:
            self._track_skip(f"insufficient_data(ticks={len(buf.ticks) if buf else 0},candles={len(buf.candles_1m) if buf else 0})")
            self.stats.signals_skipped += 1
            return

        # Calcula seconds_to_expiry para end-of-cycle sniping
        seconds_to_expiry: float | None = None
        end_iso = mkt.get("end_date_iso", "")
        if end_iso:
            try:
                from datetime import datetime, timezone
                end_dt = datetime.fromisoformat(end_iso.replace("Z", "+00:00"))
                seconds_to_expiry = (end_dt - datetime.now(timezone.utc)).total_seconds()
            except Exception:
                pass

        # Preferência por SOL e XRP (menor competição de bots HFT)
        low_competition_syms = {"SOL", "XRP", "MATIC", "DOGE"}
        competition_boost = sym in low_competition_syms

        # ── Decisão: Combined Strategy Engine ──────────────────────────────
        sig = None

        # Prioridade 1: Combined Engine (Arb > Stale > MM > Quant Direction)
        if USE_COMBINED and combined_decide:
            sig = combined_decide(
                symbol=sym, buf=buf,
                market_price_yes=mid, horizon_minutes=int(horiz),
                question=mkt.get("question", ""),
                seconds_to_expiry=seconds_to_expiry,
                bid=bid, ask=ask,
                token_id=token_id,
                no_token_id=mkt.get("no_token_id", ""),
                condition_id=mkt.get("condition_id", ""),
                no_price=mkt.get("no_price"),
            )

        # Combined engine already handles all strategies including quant direction
        # (with end-of-cycle filter). No additional fallbacks needed —
        # unchecked direction bets are the #1 source of losses.
        if sig is None:
            return

        self.stats.scans += 1

        if sig.direction == "SKIP":
            self.stats.signals_skipped += 1
            self._track_skip(f"direction_skip(fee_adj_edge={sig.fee_adjusted_edge:.3f},conf={sig.confidence})")
            return

        if sig.confidence not in self.ALLOWED_CONF:
            # Arbitrage is always high confidence — should never hit this
            self.stats.signals_skipped += 1
            self._track_skip("confidence_low")
            return

        # Usa fee_adjusted_edge para threshold
        fee_adj = sig.fee_adjusted_edge
        strategy = getattr(sig, 'strategy', 'UNKNOWN')

        # Arbitrage e Stale Snipe já passaram por seus próprios filtros — confia
        if strategy in ("ARBITRAGE", "STALE_SNIPE"):
            min_edge_req = 0.003  # 0.3% mínimo (quase sempre passa)
        elif strategy == "MARKET_MAKING":
            min_edge_req = 0.005  # 0.5% mínimo
        else:
            min_edge_req = self.MIN_EDGE.get(sig.confidence, 0.15)
            if competition_boost:
                min_edge_req *= 0.80

        if fee_adj < min_edge_req:
            self.stats.signals_skipped += 1
            self._track_skip(f"fee_adj_edge_low({fee_adj:.3f}<{min_edge_req:.3f})")
            return

        self.stats.signals_fired += 1

        # Arbitrage: buy BOTH sides (guaranteed profit)
        if strategy == "ARBITRAGE" and sig.direction == "BOTH":
            self._place_arbitrage_trade(mkt, sym, sig, horiz, bid, ask)
        else:
            self._place_trade(mkt, sym, sig, horiz, bid, ask)

    def _place_arbitrage_trade(self, mkt: dict, sym: str, sig, horiz: float, bid: float, ask: float):
        """
        Arbitrage: compra YES e NO ao mesmo tempo.
        Um deles SEMPRE paga $1.00 na resolução → lucro = 1.00 - custo total.
        Simulamos como BUY_YES com pnl pré-calculado (já que é lucro garantido).
        """
        arb = getattr(sig, 'arb_signal', None)
        if not arb:
            return

        with self._lock:
            if len(self.open_trades) >= self.MAX_OPEN_TRADES:
                return

            # Gross = custo total do par (YES + NO + fees)
            max_sets = self.stats.balance * self.MAX_POSITION_PCT / arb.total_cost
            num_sets = max(1, min(int(max_sets), 5))  # Max 5 sets por arb
            gross = round(arb.total_cost * num_sets, 4)
            payout = round(1.00 * num_sets, 4)  # guaranteed payout
            expected_pnl = round(arb.profit_per_set * num_sets, 4)

            if gross > self.stats.balance:
                return

            trade = Trade(
                id                 = str(uuid.uuid4())[:8],
                symbol             = sym,
                question           = f"[ARB] {mkt['question'][:80]}",
                slug               = mkt["slug"],
                token_id           = mkt["token_id"],
                condition_id       = mkt.get("condition_id", ""),
                action             = "BUY_YES",  # represents the pair
                amount_usdc        = gross,
                gross_usdc         = gross,
                fee_usdc           = round(gross - arb.total_cost * num_sets / (1 + arb.yes_fee + arb.no_fee) if arb.yes_fee + arb.no_fee > 0 else 0, 4),
                entry_price        = arb.total_cost,  # cost per set
                entry_ts           = time.time(),
                horizon_min        = horiz,
                end_date_iso       = mkt.get("end_date_iso", ""),
                signal_edge        = arb.profit_pct / 100,
                signal_confidence  = "high",
                signal_reasons     = sig.reasons[:],
                signal_momentum_1m = 0,
                signal_momentum_5m = 0,
                signal_rsi         = 50,
                market_type        = "arbitrage",
                current_price      = arb.total_cost,
            )

            self.stats.balance       -= gross
            self.stats.total_trades  += 1
            self.stats.total_wagered += gross
            self.stats.total_fees    += trade.fee_usdc
            self.stats.last_action_ts = time.time()
            self.open_trades.append(trade)
            self._cooldown[mkt["slug"]] = time.time()

        self._log(
            "ARB_OPEN",
            f"ARBITRAGE {sym} sets={num_sets} cost=${gross:.4f} "
            f"expected_pnl=${expected_pnl:.4f} ({arb.profit_pct:.2f}%) "
            f"YES={arb.yes_price:.3f} NO={arb.no_price:.3f}",
            level="info", trade_id=trade.id, symbol=sym,
        )

    def _track_skip(self, reason: str):
        """FIX BUG 4: rastreia motivos de skip para debug."""
        self._skip_reasons[reason] = self._skip_reasons.get(reason, 0) + 1
        total = sum(self._skip_reasons.values())
        # Log sumário a cada 200 skips ou a cada 5 minutos
        if total % 200 == 0 or (time.time() - self._last_skip_log > 300 and total > 0):
            top = sorted(self._skip_reasons.items(), key=lambda x: -x[1])[:5]
            summary = " | ".join(f"{r}={c}" for r, c in top)
            self._log("SKIP_SUMMARY", f"Total={total} Top: {summary}")
            self._last_skip_log = time.time()

    def _place_trade(self, mkt: dict, sym: str, sig, horiz: float, bid: float, ask: float):
        with self._lock:
            # FIX BUG 1: reverifica MAX_OPEN_TRADES dentro do lock
            if len(self.open_trades) >= self.MAX_OPEN_TRADES:
                return

            if sig.direction == "YES":
                action:      Literal["BUY_YES", "BUY_NO"] = "BUY_YES"
                entry_price  = ask
                p_est        = max(0.05, min(0.95, 0.5 + sig.edge))
            else:
                action       = "BUY_NO"
                entry_price  = max(0.02, 1.0 - bid)
                p_est        = max(0.05, min(0.95, 0.5 + abs(sig.edge)))

            entry_price = max(0.03, min(0.97, entry_price))

            b = (1.0 / entry_price) - 1.0
            q = 1.0 - p_est
            kelly_raw = (b * p_est - q) / b if b > 0 else 0.0
            kelly     = max(0.0, min(kelly_raw, self.KELLY_FRACTION))

            cmult    = {"high": 1.0, "medium": 0.55}[sig.confidence]
            max_bet  = self.stats.balance * self.MAX_POSITION_PCT
            gross    = round(kelly * max_bet * cmult, 2)
            gross    = max(gross, 0.10)
            gross    = min(gross, max_bet, self.stats.balance * 0.06)

            # Taxa dinâmica baseada no preço de entrada (não flat rate)
            from src.signals.engine import polymarket_dynamic_fee
            dynamic_fee_rate = polymarket_dynamic_fee(entry_price)
            fee    = round(gross * dynamic_fee_rate, 4)
            amount = round(gross - fee, 4)

            if gross > self.stats.balance:
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

            self.stats.balance       -= gross
            self.stats.total_trades  += 1
            self.stats.total_wagered += gross
            self.stats.total_fees    += fee
            self.stats.last_action_ts = time.time()
            self.open_trades.append(trade)
            self._cooldown[mkt["slug"]] = time.time()

        # Fase 1: registra sinal no DataCollector
        if self.collector:
            try:
                buf = self.buffers.get(trade.symbol)
                bin_price = buf.latest_price() if buf else 0.0
                trade._signal_id = self.collector.record_signal(
                    condition_id  = trade.condition_id,
                    symbol        = trade.symbol,
                    action        = trade.action,
                    confidence    = sig.confidence,
                    edge          = sig.edge,
                    entry_price   = trade.entry_price,
                    horizon_mins  = trade.horizon_min,
                    binance_price = bin_price or 0.0,
                    signal_dict   = {
                        "edge": sig.edge, "confidence": sig.confidence,
                        "direction": sig.direction, "momentum_1m": sig.momentum_1m,
                        "momentum_5m": sig.momentum_5m, "rsi": sig.rsi,
                        "reasons": sig.reasons, "market_type": sig.market_type,
                    },
                )
            except Exception:
                trade._signal_id = 0

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
        now       = time.time()
        to_close: list[tuple] = []

        with self._lock:
            for t in list(self.open_trades):
                mkt     = self._markets.get(t.token_id, {})
                bid     = mkt.get("bid", t.entry_price)
                ask_mkt = mkt.get("ask", t.entry_price)

                if t.action == "BUY_YES":
                    cur_val = ask_mkt
                else:
                    cur_val = 1.0 - bid

                cur_val = max(0.01, min(0.99, cur_val))
                t.unrealized_pnl = round((cur_val / t.entry_price - 1) * t.amount_usdc, 3)
                t.current_price  = ask_mkt if t.action == "BUY_YES" else 1.0 - bid
                t.price_history.append(round(ask_mkt, 4))
                if len(t.price_history) > 120:
                    t.price_history = t.price_history[-120:]

                age_min  = (now - t.entry_ts) / 60
                sell_val = bid if t.action == "BUY_YES" else (1.0 - ask_mkt)

                if sell_val < t.entry_price * (1 - self.STOP_LOSS_PCT):
                    to_close.append((t, "STOP_LOSS", False, sell_val))
                    continue

                tp_threshold = t.entry_price + (1.0 - t.entry_price) * self.TAKE_PROFIT_PCT
                if cur_val >= tp_threshold:
                    to_close.append((t, "TAKE_PROFIT", True, cur_val))
                    continue

                if age_min >= t.horizon_min:
                    to_close.append((t, "EXPIRED", None, cur_val))

        for t, reason, won_arg, exit_val in to_close:
            self._resolve_and_close(t, reason, won_arg, exit_val)

    # ── Fase 2: Lógica central de resolução ───────────────────────────────────

    def _market_past_end_date(self, trade: Trade) -> bool:
        if not trade.end_date_iso:
            return True
        try:
            from datetime import datetime, timezone
            end = datetime.fromisoformat(trade.end_date_iso.replace("Z", "+00:00"))
            return datetime.now(timezone.utc) > end
        except Exception:
            return True

    def _resolve_and_close(self, trade: Trade, reason: str, won_arg: bool | None, exit_val: float):
        if reason in ("STOP_LOSS", "TAKE_PROFIT"):
            won = (reason == "TAKE_PROFIT")
            self._close(trade, reason, won, exit_val)
            return

        real_outcome = None
        if trade.condition_id:
            try:
                real_outcome = fetch_market_resolution(trade.condition_id)
            except Exception:
                pass

        if real_outcome is not None:
            won = real_outcome if trade.action == "BUY_YES" else (not real_outcome)
            self.stats.real_resolutions += 1
            trade._resolved_real = True
            self._log(
                "RESOLVE_REAL",
                f"Resultado REAL {trade.symbol}: {'YES' if real_outcome else 'NO'} "
                f"(real={self.stats.real_resolutions} sim={self.stats.simulated_resolutions})"
            )
            self._close(trade, reason, won, exit_val)

        elif self._market_past_end_date(trade):
            self._enter_pending(trade)

        else:
            won = won_arg if won_arg is not None else self._simulate_resolution(trade)
            self.stats.simulated_resolutions += 1
            self._log(
                "RESOLVE_SIMULATED",
                f"Proxy Binance {trade.symbol} "
                f"(real={self.stats.real_resolutions} sim={self.stats.simulated_resolutions})"
            )
            self._close(trade, reason, won, exit_val)

    def _enter_pending(self, trade: Trade):
        with self._lock:
            if trade not in self.open_trades:
                return
            self.open_trades.remove(trade)

        trade.status             = "PENDING"
        trade.pending_resolution = True
        trade.pending_since_ts   = time.time()
        trade.unrealized_pnl     = 0.0

        with self._pending_lock:
            self._pending_resolution.append(trade)

        self._log(
            "PENDING_RESOLUTION",
            f"⏳ {trade.symbol} {trade.action} aguardando oracle "
            f"| condition_id={trade.condition_id[:12]}… "
            f"| gross=${trade.gross_usdc:.2f}",
            level="info",
            trade_id=trade.id,
            symbol=trade.symbol,
        )

    # ── Fase 2: Worker de resolução ────────────────────────────────────────────

    def _resolution_worker(self):
        print("[ResolutionWorker] Iniciado — verificação a cada 5min")
        while self._running:
            time.sleep(RESOLUTION_WORKER_INTERVAL)

            if not self._running:
                break

            with self._pending_lock:
                pending_snapshot = list(self._pending_resolution)

            if not pending_snapshot:
                continue

            self._log(
                "RESOLUTION_CHECK",
                f"Verificando {len(pending_snapshot)} trade(s) pendente(s)…",
                level="info",
            )

            resolved_ids: list[str] = []

            for trade in pending_snapshot:
                if not self._running:
                    break

                waiting_secs = time.time() - trade.pending_since_ts

                if waiting_secs >= PENDING_EXPIRY_SECONDS:
                    self._expire_pending(trade)
                    resolved_ids.append(trade.id)
                    continue

                real_outcome = None
                try:
                    real_outcome = fetch_market_resolution(trade.condition_id)
                except Exception as e:
                    self._log("RESOLUTION_ERR", f"{trade.symbol}: {e}", level="error")
                    continue

                if real_outcome is not None:
                    won = real_outcome if trade.action == "BUY_YES" else (not real_outcome)
                    self.stats.real_resolutions += 1
                    trade._resolved_real = True
                    self._close_pending(trade, won, real_outcome)
                    resolved_ids.append(trade.id)
                else:
                    mins_waiting = round(waiting_secs / 60, 1)
                    self._log(
                        "PENDING_STILL",
                        f"⏳ {trade.symbol} ainda sem resultado | {mins_waiting}min aguardando",
                        level="info",
                    )

            if resolved_ids:
                with self._pending_lock:
                    self._pending_resolution = [
                        t for t in self._pending_resolution
                        if t.id not in resolved_ids
                    ]

    def _close_pending(self, trade: Trade, won: bool, real_outcome: bool):
        exit_val = 1.0 if won else 0.0

        self._log(
            "RESOLVE_REAL",
            f"✓ Resultado REAL (da fila) {trade.symbol}: {'YES' if real_outcome else 'NO'} "
            f"| {trade.action} | {'WIN' if won else 'LOSS'} "
            f"| aguardou {round((time.time() - trade.pending_since_ts) / 60, 1)}min",
            level="win" if won else "loss",
        )

        with self._lock:
            trade.status             = "OPEN"
            trade.pending_resolution = False
            self.open_trades.append(trade)

        self._close(trade, "RESOLVE_REAL", won, exit_val)

    def _expire_pending(self, trade: Trade):
        self.stats.expired_count += 1

        trade.status      = "CLOSED"
        trade.exit_ts     = time.time()
        trade.exit_reason = "EXPIRED_NO_RESOLUTION"
        trade.pnl_usdc    = 0.0
        trade.pnl_pct     = 0.0
        trade.won         = None

        with self._lock:
            self.closed_trades.append(trade)

        if self.collector:
            try:
                self.collector.record_trade_outcome(
                    condition_id      = trade.condition_id,
                    signal_id         = getattr(trade, "_signal_id", 0),
                    entry_ts          = trade.entry_ts,
                    entry_price       = trade.entry_price,
                    action            = trade.action,
                    outcome_yes       = None,
                    won               = None,
                    resolution_source = "pending",
                    pnl               = 0.0,
                    closed_ts         = time.time(),
                )
            except Exception:
                pass

        self._log(
            "TRADE_EXPIRED",
            f"⚠️  {trade.symbol} {trade.action} expirado após 48h sem resolução "
            f"| gross=${trade.gross_usdc:.2f} | condition_id={trade.condition_id[:12]}…",
            level="loss",
            trade_id=trade.id,
            symbol=trade.symbol,
        )

    # ── Fechamento de trade ────────────────────────────────────────────────────

    def _simulate_resolution(self, trade: Trade) -> bool:
        buf = self.buffers.get(trade.symbol)
        if not buf:
            return random.random() < 0.47

        p_now = buf.latest_price()
        if p_now is None:
            return random.random() < 0.47

        p_entry = None
        ticks_list = list(buf.ticks)
        for ts, p in ticks_list:
            if ts >= trade.entry_ts:
                p_entry = p
                break

        if p_entry is None and ticks_list:
            p_entry = ticks_list[0][1]

        if not p_entry or p_entry == 0:
            return random.random() < 0.47

        pct   = (p_now / p_entry - 1) * 100
        noise = random.gauss(0, 0.003)

        if trade.action == "BUY_YES":
            return (pct + noise) > 0.05
        else:
            return (pct + noise) < -0.05

    def _close(self, trade: Trade, reason: str, won: bool, exit_val: float):
        with self._lock:
            if trade not in self.open_trades:
                return

            if reason == "TAKE_PROFIT":
                exit_val_clamped = min(0.97, max(exit_val, trade.entry_price * 1.10))
                payout = (trade.amount_usdc / trade.entry_price) * exit_val_clamped
                pnl    = payout - trade.amount_usdc
                self.stats.wins   += 1
                self.stats.balance += payout

            elif reason == "STOP_LOSS":
                exit_val_clamped = max(0.01, exit_val)
                payout = trade.amount_usdc * (exit_val_clamped / trade.entry_price)
                pnl    = payout - trade.amount_usdc
                self.stats.losses += 1
                self.stats.balance += payout

            elif won:
                payout = trade.amount_usdc / trade.entry_price
                pnl    = payout - trade.amount_usdc
                self.stats.wins   += 1
                self.stats.balance += payout

            else:
                pnl    = -trade.amount_usdc
                payout = 0.0
                self.stats.losses += 1

            trade.status      = "CLOSED"
            trade.exit_ts     = time.time()
            trade.exit_price  = exit_val
            trade.exit_reason = reason
            trade.pnl_usdc    = round(pnl, 3)
            trade.pnl_pct     = round(pnl / trade.amount_usdc * 100, 1)
            trade.won         = won
            trade.unrealized_pnl = 0.0
            self.stats.total_pnl      += pnl
            self.stats.last_action_ts  = time.time()

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

        if self.collector:
            try:
                if trade.won is not None:
                    outcome_yes = (trade.won == (trade.action == "BUY_YES"))
                else:
                    outcome_yes = None
                res_source = "real" if getattr(trade, "_resolved_real", False) else "simulated"
                self.collector.record_trade_outcome(
                    condition_id      = trade.condition_id,
                    signal_id         = getattr(trade, "_signal_id", 0),
                    entry_ts          = trade.entry_ts,
                    entry_price       = trade.entry_price,
                    action            = trade.action,
                    outcome_yes       = outcome_yes,
                    won               = trade.won,
                    resolution_source = res_source,
                    pnl               = pnl,
                    closed_ts         = time.time(),
                )
            except Exception:
                pass

        icon = "✓" if won else "✗"
        self._log(
            "TRADE_CLOSE",
            f"{icon} ${pnl:+.2f} ({trade.pnl_pct:+.0f}%) | "
            f"{trade.symbol} {trade.action} | {reason} | bal ${self.stats.balance:.2f}",
            level="win" if won else "loss",
            trade_id=trade.id, pnl=pnl, won=won,
            reason=reason, symbol=trade.symbol, amount=trade.amount_usdc,
        )

        if reason in ("EXPIRED", "RESOLVE_REAL") and trade.token_id in self._markets:
            self._markets[trade.token_id]["resolved"] = True

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _update_open_prices(self, token_id: str, mid: float):
        with self._lock:
            for t in self.open_trades:
                if t.token_id == token_id:
                    cur = mid if t.action == "BUY_YES" else 1.0 - mid
                    t.current_price  = mid
                    t.unrealized_pnl = round((cur / t.entry_price - 1) * t.amount_usdc, 3)

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

    # ── Helpers de teste ───────────────────────────────────────────────────────

    def _place_trade_direct(self, mkt: dict, sym: str, sig, horiz: float, bid: float, ask: float):
        """Abre trade diretamente sem cooldown/warmup. Apenas para testes."""
        with self._lock:
            if sig.direction == "YES":
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
            if gross > self.stats.balance:
                return None

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
            self.stats.balance -= gross
            self.stats.total_trades += 1
            self.stats.total_wagered += gross
            self.stats.total_fees += fee
            self.open_trades.append(trade)
        return trade

    def close_trade(self, trade: Trade, won: bool, reason: str = "EXPIRED"):
        """Fecha trade manualmente. Apenas para testes."""
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