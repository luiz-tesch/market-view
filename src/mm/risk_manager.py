"""
src/mm/risk_manager.py — Gestão de inventário e risco para Market Making.

Modelo de P&L correto para MM (funciona long E short):
  P&L realizado = (avg_sell - avg_buy) × shares_matched
  matched       = min(total_comprado, total_vendido)

Capital tracking com reserva de ordens abertas:
  capital_comprometido = total_gasto_em_buys + valor_em_aberto_de_buys
  → impede estouro mesmo com fills assíncronos
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from threading import RLock

logger = logging.getLogger("mm.risk")


@dataclass
class Position:
    token_id: str
    question: str
    symbol:   str

    # Rastreio separado de compras e vendas
    total_bought:       float = 0.0  # shares compradas (acumulado)
    total_sold:         float = 0.0  # shares vendidas (acumulado)
    total_buy_cost:     float = 0.0  # USDC gasto em compras
    total_sell_revenue: float = 0.0  # USDC recebido em vendas

    fills:        int   = 0
    last_fill_ts: float = 0.0

    @property
    def yes_shares(self) -> float:
        """Posição líquida: positivo = long, negativo = short."""
        return self.total_bought - self.total_sold

    @property
    def avg_buy_price(self) -> float:
        return self.total_buy_cost / self.total_bought if self.total_bought > 0 else 0.0

    @property
    def avg_sell_price(self) -> float:
        return self.total_sell_revenue / self.total_sold if self.total_sold > 0 else 0.0

    @property
    def realized_pnl(self) -> float:
        """
        P&L realizado nas operações que fecharam (round trips).
        matched = pares bid+ask que foram ambos preenchidos.
        """
        matched = min(self.total_bought, self.total_sold)
        if matched < 0.01:
            return 0.0
        return (self.avg_sell_price - self.avg_buy_price) * matched

    @property
    def net_usdc_at_risk(self) -> float:
        """USDC em risco (posição líquida × preço médio de compra)."""
        net = self.yes_shares
        if net > 0:
            return net * self.avg_buy_price
        if net < 0:
            return abs(net) * (1 - self.avg_sell_price)
        return 0.0


@dataclass
class RiskConfig:
    max_position_shares:   float = 15.0
    max_usdc_per_market:   float = 8.0
    max_total_usdc:        float = 20.0
    order_usdc:            float = 5.0
    min_spread:            float = 0.012
    inventory_skew_factor: float = 0.025


class RiskManager:
    """
    Gerencia inventário, capital e risco do market maker.

    Dois mecanismos de controle de capital:
      1. Reserva de ordens abertas  → _open_buy_value
         Quando uma ordem BUY é colocada, o valor é reservado.
         Quando cancela ou fill → reserva é liberada/convertida.
      2. Capital gasto em fills BUY → _total_buy_spent
         Após fill, o USDC saiu de verdade.

    can_quote() verifica: buy_spent + open_buy_value < max_total_usdc
    → impede estouro mesmo com fills assíncronos do simulador.
    """

    def __init__(self, config: RiskConfig | None = None) -> None:
        self.config     = config or RiskConfig()
        self._positions: dict[str, Position] = {}
        self._lock      = RLock()

        # Capital em ordens BUY abertas (reservado mas não gasto)
        self._open_buy_value:  float = 0.0
        # Capital já gasto em fills BUY
        self._total_buy_spent: float = 0.0
        # P&L realizado total
        self._total_realized:  float = 0.0

        self._orders_placed = 0
        self._start_ts      = time.time()

    # ─────────────────────────────────────────────────────────────────────────
    # Rastreio de ordens abertas (chamado pelo MarketEngine)
    # ─────────────────────────────────────────────────────────────────────────

    def on_order_placed(self, token_id: str, side: str, size: float, price: float) -> None:
        """Reserva capital quando uma ordem BUY é colocada."""
        if side == "buy":
            with self._lock:
                self._open_buy_value += size * price
                self._orders_placed  += 1

    def on_order_cancelled(self, token_id: str, side: str, size: float, price: float) -> None:
        """Libera reserva quando uma ordem BUY é cancelada."""
        if side == "buy":
            with self._lock:
                self._open_buy_value = max(0.0, self._open_buy_value - size * price)

    # ─────────────────────────────────────────────────────────────────────────
    # Fills
    # ─────────────────────────────────────────────────────────────────────────

    def on_fill(
        self,
        token_id:  str,
        side:      str,
        size:      float,
        price:     float,
        question:  str = "",
        symbol:    str = "",
    ) -> None:
        usdc_value = size * price

        with self._lock:
            if token_id not in self._positions:
                self._positions[token_id] = Position(
                    token_id=token_id, question=question, symbol=symbol,
                )
            pos = self._positions[token_id]

            if side == "buy":
                pos.total_bought   += size
                pos.total_buy_cost += usdc_value
                # Converte reserva em capital gasto (libera o open e gasta definitivo)
                self._open_buy_value  = max(0.0, self._open_buy_value - usdc_value)
                self._total_buy_spent += usdc_value

            else:  # sell
                pos.total_sold        += size
                pos.total_sell_revenue += usdc_value

            pos.fills        += 1
            pos.last_fill_ts  = time.time()

            # Atualiza total realizado
            self._total_realized = sum(p.realized_pnl for p in self._positions.values())

        skew = self.get_inventory_skew(token_id)
        logger.info(
            "Fill %s %.2fsh @ %.3f | net=%.2f skew=%.2f pnl=$%+.4f",
            side.upper(), size, price,
            self._positions[token_id].yes_shares, skew,
            self._positions[token_id].realized_pnl,
        )

    # ─────────────────────────────────────────────────────────────────────────
    # Inventory skew & quote adjustment
    # ─────────────────────────────────────────────────────────────────────────

    def get_inventory_skew(self, token_id: str) -> float:
        with self._lock:
            pos = self._positions.get(token_id)
            if not pos:
                return 0.0
            return max(-1.0, min(1.0, pos.yes_shares / self.config.max_position_shares))

    def adjust_quotes(self, mid: float, base_spread: float, token_id: str) -> tuple[float, float]:
        skew     = self.get_inventory_skew(token_id)
        mid_adj  = mid - skew * self.config.inventory_skew_factor
        half     = max(base_spread / 2, self.config.min_spread / 2)

        bid = round(max(0.01, min(0.97, mid_adj - half)), 3)
        ask = round(max(0.02, min(0.99, mid_adj + half)), 3)

        if bid >= ask:
            bid = round(mid - self.config.min_spread / 2, 3)
            ask = round(mid + self.config.min_spread / 2, 3)

        return bid, ask

    def order_size(self, price: float, token_id: str, side: str) -> float:
        if price <= 0:
            return 0.0

        # Sell só é possível com inventário YES suficiente
        if side == "sell":
            with self._lock:
                pos = self._positions.get(token_id)
                yes_shares = pos.yes_shares if pos else 0.0
            if yes_shares < 5.0:
                return 0.0
            return round(min(yes_shares, self.config.order_usdc / price, self.config.max_position_shares), 1)

        base_shares = self.config.order_usdc / price
        skew = self.get_inventory_skew(token_id)

        if side == "buy" and skew > 0.5:
            base_shares *= (1 - skew) * 0.6

        result = round(max(0.5, min(base_shares, self.config.max_position_shares)), 1)
        # Polymarket mínimo de 5 shares — descarta ordens menores para não poluir API
        return result if result >= 5.0 else 0.0

    def clear_position(self, token_id: str) -> None:
        """Remove posição da memória (tokens perdidos ou já liquidados on-chain)."""
        with self._lock:
            if token_id in self._positions:
                pos = self._positions.pop(token_id)
                logger.warning("Posicao removida: ...%s (%.2f shares, custo $%.2f)",
                               token_id[-12:], pos.yes_shares, pos.net_usdc_at_risk)

    # ─────────────────────────────────────────────────────────────────────────
    # Guards
    # ─────────────────────────────────────────────────────────────────────────

    def can_quote(self, token_id: str) -> tuple[bool, str]:
        """Retorna (pode_colocar_buy, pode_colocar_sell)."""
        with self._lock:
            committed  = self._total_buy_spent + self._open_buy_value
            can_buy    = committed + self.config.order_usdc <= self.config.max_total_usdc
            pos        = self._positions.get(token_id)
            has_inventory = pos is not None and pos.yes_shares >= 5.0
            pos_too_large = pos is not None and abs(pos.yes_shares) >= self.config.max_position_shares * 1.5

        if pos_too_large:
            return False, f"Posição muito grande ({pos.yes_shares:.1f} shares)"
        if not can_buy and not has_inventory:
            return False, f"Capital comprometido ${committed:.2f} sem inventário para vender"
        return True, "OK"

    def can_buy(self, token_id: str) -> bool:
        with self._lock:
            committed = self._total_buy_spent + self._open_buy_value
            return committed + self.config.order_usdc <= self.config.max_total_usdc

    # ─────────────────────────────────────────────────────────────────────────
    # Stats
    # ─────────────────────────────────────────────────────────────────────────

    def stats(self) -> dict:
        with self._lock:
            positions = {
                tid: {
                    "yes_shares":      round(p.yes_shares, 2),
                    "realized_pnl":    round(p.realized_pnl, 4),
                    "avg_buy":         round(p.avg_buy_price, 4),
                    "avg_sell":        round(p.avg_sell_price, 4),
                    "total_bought":    round(p.total_bought, 2),
                    "total_sold":      round(p.total_sold, 2),
                    "fills":           p.fills,
                    "symbol":          p.symbol,
                }
                for tid, p in self._positions.items()
            }
            committed = self._total_buy_spent + self._open_buy_value
            return {
                "total_usdc_spent":   round(self._total_buy_spent, 2),
                "open_buy_value":     round(self._open_buy_value, 2),
                "capital_committed":  round(committed, 2),
                "total_realized_pnl": round(self._total_realized, 4),
                "orders_placed":      self._orders_placed,
                "uptime_min":         round((time.time() - self._start_ts) / 60, 1),
                "positions":          positions,
            }

    def summary_str(self) -> str:
        s = self.stats()
        return (
            f"Comprometido: ${s['capital_committed']:.2f}/${self.config.max_total_usdc:.0f} | "
            f"P&L: ${s['total_realized_pnl']:+.4f} | "
            f"Ordens: {s['orders_placed']} | "
            f"Uptime: {s['uptime_min']}min"
        )
