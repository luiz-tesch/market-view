"""
src/signals/strategies.py — Estratégias lucrativas para Polymarket

Três estratégias comprovadas que NÃO dependem de prever direção:

1. ARBITRAGE — Complete-set: compra YES + NO quando total < $1.00 (lucro garantido)
2. MARKET MAKING — Coloca ordens nos dois lados e lucra com spread
3. STALE PRICE SNIPING — Detecta quando preço do Polymarket está atrasado vs Binance

Também inclui:
4. DYNAMIC VOL — Calibra volatilidade em tempo real (melhora o fair price model)
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from collections import deque

from src.signals.engine import PriceBuffer, polymarket_dynamic_fee


# ═══════════════════════════════════════════════════════════════════════════════
# STRATEGY 1: COMPLETE-SET ARBITRAGE
# Se YES_price + NO_price < 1.00 (após fees), comprar ambos = lucro garantido
# Funciona porque um deles SEMPRE paga $1.00 na resolução
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class ArbitrageSignal:
    """Sinal de arbitragem — lucro garantido se executado."""
    type: str = "ARBITRAGE"
    yes_price: float = 0.0
    no_price: float = 0.0
    total_cost: float = 0.0      # yes + no + fees
    profit_per_set: float = 0.0   # 1.00 - total_cost
    profit_pct: float = 0.0       # profit / cost * 100
    yes_fee: float = 0.0
    no_fee: float = 0.0
    token_id: str = ""
    no_token_id: str = ""
    condition_id: str = ""
    question: str = ""
    symbol: str = ""


def find_arbitrage(
    yes_price: float,
    no_price: float,
    token_id: str = "",
    no_token_id: str = "",
    condition_id: str = "",
    question: str = "",
    symbol: str = "",
    min_profit_pct: float = 0.3,  # mínimo 0.3% de lucro para compensar risco de execução
) -> ArbitrageSignal | None:
    """
    Detecta oportunidade de arbitragem complete-set.

    Na Polymarket:
    - Comprar 1 share YES ao preço yes_price
    - Comprar 1 share NO ao preço no_price
    - Na resolução, um deles paga $1.00, o outro $0.00
    - Lucro = $1.00 - (yes_price + no_price + fees)

    Fee é cobrada em cada lado separadamente.
    """
    if yes_price <= 0.01 or no_price <= 0.01:
        return None
    if yes_price >= 0.99 or no_price >= 0.99:
        return None

    # Fees para cada lado
    yes_fee = polymarket_dynamic_fee(yes_price)
    no_fee = polymarket_dynamic_fee(no_price)

    # Custo total por set: preço YES + fee YES + preço NO + fee NO
    total_cost = yes_price * (1 + yes_fee) + no_price * (1 + no_fee)

    # Lucro = $1.00 (payout garantido) - custo total
    profit = 1.00 - total_cost

    if total_cost <= 0:
        return None

    profit_pct = (profit / total_cost) * 100

    if profit_pct < min_profit_pct:
        return None

    return ArbitrageSignal(
        yes_price=yes_price,
        no_price=no_price,
        total_cost=round(total_cost, 6),
        profit_per_set=round(profit, 6),
        profit_pct=round(profit_pct, 3),
        yes_fee=round(yes_fee, 6),
        no_fee=round(no_fee, 6),
        token_id=token_id,
        no_token_id=no_token_id,
        condition_id=condition_id,
        question=question,
        symbol=symbol,
    )


def scan_arbitrage(markets: list[dict]) -> list[ArbitrageSignal]:
    """
    Escaneia todos os mercados por oportunidades de arbitragem.
    markets: lista de dicts com keys: token_id, no_token_id, condition_id,
             question, symbol, yes_price/bid, no_price/no_bid
    """
    opportunities = []
    for m in markets:
        # Usa best bid para YES e best bid para NO (preço que conseguimos comprar)
        yes_ask = m.get("ask", m.get("yes_price", 0.5))
        no_ask = m.get("no_ask", m.get("no_price", 0.5))

        sig = find_arbitrage(
            yes_price=yes_ask,
            no_price=no_ask,
            token_id=m.get("token_id", ""),
            no_token_id=m.get("no_token_id", ""),
            condition_id=m.get("condition_id", ""),
            question=m.get("question", ""),
            symbol=m.get("symbol", ""),
        )
        if sig:
            opportunities.append(sig)

    # Ordena por maior lucro
    opportunities.sort(key=lambda x: -x.profit_pct)
    return opportunities


# ═══════════════════════════════════════════════════════════════════════════════
# STRATEGY 2: MARKET MAKING (SPREAD CAPTURE)
# Coloca ordens limit nos dois lados do spread
# Lucra quando ambos os lados são executados (bid-ask spread)
# Não precisa prever direção
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class MarketMakingSignal:
    """Sinal de market making — lucra com spread bid/ask."""
    type: str = "MARKET_MAKING"
    bid_price: float = 0.0      # preço que vamos oferecer para comprar
    ask_price: float = 0.0      # preço que vamos oferecer para vender
    spread: float = 0.0          # ask - bid
    mid_price: float = 0.0
    fair_price: float = 0.0      # preço justo calculado
    edge_per_side: float = 0.0   # lucro esperado por lado após fees
    inventory_skew: float = 0.0  # ajuste por inventário [-1, +1]
    token_id: str = ""
    condition_id: str = ""
    question: str = ""
    symbol: str = ""


def market_making_signal(
    current_bid: float,
    current_ask: float,
    fair_price: float,
    token_id: str = "",
    condition_id: str = "",
    question: str = "",
    symbol: str = "",
    inventory_position: float = 0.0,  # shares que já temos (-=short YES, +=long YES)
    min_spread: float = 0.02,         # spread mínimo para fazer market making
    min_edge: float = 0.005,          # edge mínimo por lado após fees
) -> MarketMakingSignal | None:
    """
    Calcula ordens de market making.

    Estratégia Avellaneda-Stoikov simplificada:
    - Preço justo (fair_price) é calculado pelo modelo log-normal
    - Spread é definido pela volatilidade + fee
    - Inventário faz skew dos preços (vende mais caro se temos muito, compra mais barato se pouco)
    """
    spread = current_ask - current_bid
    mid = (current_bid + current_ask) / 2

    if spread < min_spread:
        return None

    # Fee no mid price
    fee = polymarket_dynamic_fee(mid)

    # Nosso spread precisa ser maior que 2x fee (compra + venda)
    min_profitable_spread = 2 * fee + 0.01  # 1 cent margin

    if spread < min_profitable_spread:
        return None

    # Inventory skew: se temos muitos YES shares, queremos vender mais agressivamente
    # skew em [-1, +1]
    skew = max(-1.0, min(1.0, -inventory_position * 0.1))

    # Nosso bid/ask: ligeiramente melhores que o mercado, centrados no fair price
    half_spread = spread * 0.4  # tightening do spread
    our_bid = fair_price - half_spread + skew * 0.01
    our_ask = fair_price + half_spread + skew * 0.01

    # Clamp
    our_bid = max(0.02, min(0.98, our_bid))
    our_ask = max(0.02, min(0.98, our_ask))

    if our_ask <= our_bid:
        return None

    # Edge por lado = metade do spread - fee
    edge_per_side = (our_ask - our_bid) / 2 - fee

    if edge_per_side < min_edge:
        return None

    return MarketMakingSignal(
        bid_price=round(our_bid, 4),
        ask_price=round(our_ask, 4),
        spread=round(our_ask - our_bid, 4),
        mid_price=round(mid, 4),
        fair_price=round(fair_price, 4),
        edge_per_side=round(edge_per_side, 4),
        inventory_skew=round(skew, 3),
        token_id=token_id,
        condition_id=condition_id,
        question=question,
        symbol=symbol,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# STRATEGY 3: STALE PRICE SNIPING
# Detecta quando o preço no Polymarket está atrasado vs Binance spot
# Ex: Binance já moveu 0.5% UP mas Polymarket YES ainda não reagiu
# Compra YES antes que o mercado se ajuste
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class StalePriceSignal:
    """Sinal de stale price — Polymarket atrasado vs Binance."""
    type: str = "STALE_SNIPE"
    direction: str = "YES"        # YES ou NO
    confidence: str = "medium"
    edge: float = 0.0
    fee_adjusted_edge: float = 0.0
    strength: float = 0.0
    momentum_1m: float = 0.0
    momentum_5m: float = 0.0
    rsi: float = 50.0
    vwap_dev: float = 0.0
    bb_position: float = 0.5
    reasons: list[str] = field(default_factory=list)
    market_type: str = "price"
    symbol: str = ""
    # Stale-specific
    binance_move_pct: float = 0.0   # quanto Binance se moveu
    poly_lag_pct: float = 0.0        # quanto Poly está atrasado
    fair_prob: float = 0.5
    seconds_stale: float = 0.0       # há quanto tempo está stale


# Track de preços Polymarket para detectar stale
_poly_price_history: dict[str, deque] = {}  # token_id -> deque[(ts, mid)]
_last_binance_move: dict[str, tuple[float, float]] = {}  # symbol -> (ts, pct_move)


def track_poly_price(token_id: str, mid: float):
    """Registra preço Polymarket para tracking de stale."""
    if token_id not in _poly_price_history:
        _poly_price_history[token_id] = deque(maxlen=120)
    _poly_price_history[token_id].append((time.time(), mid))


def detect_stale_price(
    symbol: str,
    buf: PriceBuffer,
    token_id: str,
    market_price_yes: float,
    horizon_minutes: int,
    question: str = "",
    seconds_to_expiry: float | None = None,
    min_binance_move_pct: float = 0.15,   # Binance precisa ter movido pelo menos 0.15%
    max_poly_reaction: float = 0.02,       # Polymarket reagiu menos de 2 cents
    min_edge: float = 0.04,               # edge mínimo após fees (conservative)
) -> StalePriceSignal | None:
    """
    Detecta quando Polymarket está atrasado vs movimento real do Binance.

    Lógica:
    1. Calcula quanto Binance se moveu nos últimos 30-60s
    2. Calcula probabilidade fair baseada no novo preço
    3. Compara com preço Polymarket (que está stale)
    4. Se o gap é grande o suficiente → snipe
    """
    price_now = buf.latest_price()
    if not price_now:
        return None

    # Precisa de pelo menos 30s de dados
    if len(buf.ticks) < 30:
        return None

    # Preço de 30s atrás
    p_30s = buf.price_n_seconds_ago(30)
    p_60s = buf.price_n_seconds_ago(60)

    if not p_30s or not p_60s:
        return None

    # Movimento do Binance nos últimos 30s e 60s
    move_30s = (price_now / p_30s - 1) * 100
    move_60s = (price_now / p_60s - 1) * 100

    # Precisa de movimento significativo
    if abs(move_30s) < min_binance_move_pct and abs(move_60s) < min_binance_move_pct:
        return None

    best_move = move_30s if abs(move_30s) > abs(move_60s) else move_60s

    # Verifica se Polymarket reagiu
    poly_history = _poly_price_history.get(token_id, deque())
    if len(poly_history) < 5:
        return None

    # Preço Poly de 30s atrás
    now = time.time()
    poly_30s_ago = None
    for ts, p in reversed(poly_history):
        if now - ts >= 25:  # ~30s ago
            poly_30s_ago = p
            break

    if poly_30s_ago is None:
        poly_30s_ago = poly_history[0][1]

    poly_change = market_price_yes - poly_30s_ago

    # Se Poly já reagiu proporcionalmente, não é stale
    if abs(best_move) > 0 and abs(poly_change) / max(abs(best_move) * 0.01, 0.001) > 0.5:
        return None  # Poly reagiu mais de 50% do movimento

    # Calcula fair price com vol dinâmica
    vol = calculate_dynamic_vol(symbol, buf)
    ref_price = buf.price_n_seconds_ago(int(horizon_minutes * 60)) or price_now
    mins_left = seconds_to_expiry / 60.0 if seconds_to_expiry and seconds_to_expiry > 0 else horizon_minutes

    from src.signals.quant_brain import calculate_fair_prob
    fair_prob = calculate_fair_prob(price_now, ref_price, vol, mins_left)

    # Edge = fair - market
    # IMPORTANT: Cap the edge to prevent model overconfidence
    # The log-normal model gives extreme probs for small moves — don't trust >10% edge
    edge = fair_prob - market_price_yes
    fee = polymarket_dynamic_fee(market_price_yes)
    raw_fee_adj = abs(edge) - fee

    # Cap edge: max edge claim is proportional to the Binance move
    # A 0.15% Binance move shouldn't claim more than ~5% edge
    max_plausible_edge = abs(best_move) * 0.3  # 30% of binance move as edge (conservative)
    fee_adj = min(raw_fee_adj, max_plausible_edge / 100)

    if fee_adj < min_edge:
        return None

    direction = "YES" if edge > 0 else "NO"

    # Confidence: only high if Binance move is very large AND poly hasn't reacted
    if abs(best_move) > 0.5 and fee_adj > 0.06:
        confidence = "high"
    elif abs(best_move) > 0.25 and fee_adj > 0.04:
        confidence = "medium"
    else:
        confidence = "low"

    reasons = [
        f"STALE: Bin {best_move:+.3f}% Poly {poly_change:+.3f}",
        f"Fair={fair_prob:.1%} vs Mkt={market_price_yes:.1%}",
        f"Edge={fee_adj:.1%} after fee={fee:.1%}",
    ]

    print(f"[Stale] {symbol} {direction} bin_move={best_move:+.3f}% poly_lag={poly_change:+.3f} "
          f"fair={fair_prob:.3f} mkt={market_price_yes:.3f} edge={fee_adj:.3f}")

    return StalePriceSignal(
        direction=direction,
        confidence=confidence,
        edge=edge,
        fee_adjusted_edge=fee_adj,
        strength=min(1.0, fee_adj / 0.15),
        momentum_1m=(price_now / p_60s - 1) * 100 if p_60s else 0,
        momentum_5m=0.0,
        rsi=50.0,
        vwap_dev=0.0,
        bb_position=0.5,
        reasons=reasons,
        symbol=symbol,
        binance_move_pct=best_move,
        poly_lag_pct=poly_change,
        fair_prob=fair_prob,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# STRATEGY 4: DYNAMIC VOLATILITY CALIBRATION
# Calcula vol em tempo real a partir dos dados do Binance
# Muito melhor que valores fixos hardcoded
# ═══════════════════════════════════════════════════════════════════════════════

# Cache de vol calibrada
_vol_cache: dict[str, tuple[float, float]] = {}  # symbol -> (timestamp, vol_per_min)
VOL_CACHE_TTL = 120  # recalcula a cada 2 min

# Fallbacks (usados quando dados insuficientes)
VOL_FALLBACK = {
    "BTC":   0.00035,
    "ETH":   0.00050,
    "SOL":   0.00070,
    "BNB":   0.00045,
    "MATIC": 0.00080,
    "DOGE":  0.00075,
    "XRP":   0.00060,
}


def calculate_dynamic_vol(symbol: str, buf: PriceBuffer, min_ticks: int = 60) -> float:
    """
    Calcula volatilidade por minuto em tempo real a partir dos ticks do Binance.

    Usa log-returns de 1 segundo, agrupados em janelas de 1 minuto.
    Muito mais preciso que valores fixos, especialmente durante:
    - Flash crashes/pumps
    - Períodos de baixa volatilidade (overestimar vol = SKIP bons trades)
    - Períodos de alta vol (subestimar = entrar em trades ruins)
    """
    now = time.time()

    # Check cache
    if symbol in _vol_cache:
        cached_ts, cached_vol = _vol_cache[symbol]
        if now - cached_ts < VOL_CACHE_TTL:
            return cached_vol

    ticks = list(buf.ticks)
    if len(ticks) < min_ticks:
        return VOL_FALLBACK.get(symbol, 0.0005)

    # Calcula log-returns por minuto
    # Agrupa ticks em buckets de 1 minuto
    minute_prices: dict[int, list[float]] = {}
    for ts, price in ticks:
        minute = int(ts // 60)
        if minute not in minute_prices:
            minute_prices[minute] = []
        minute_prices[minute].append(price)

    if len(minute_prices) < 3:
        return VOL_FALLBACK.get(symbol, 0.0005)

    # Close price de cada minuto
    sorted_minutes = sorted(minute_prices.keys())
    minute_closes = [minute_prices[m][-1] for m in sorted_minutes]

    # Log-returns entre minutos consecutivos
    log_returns = []
    for i in range(1, len(minute_closes)):
        if minute_closes[i - 1] > 0:
            lr = math.log(minute_closes[i] / minute_closes[i - 1])
            log_returns.append(lr)

    if len(log_returns) < 2:
        return VOL_FALLBACK.get(symbol, 0.0005)

    # Desvio padrão dos log-returns = volatilidade por minuto
    mean = sum(log_returns) / len(log_returns)
    variance = sum((r - mean) ** 2 for r in log_returns) / len(log_returns)
    vol_per_min = math.sqrt(variance)

    # Clamp para evitar valores absurdos
    fallback = VOL_FALLBACK.get(symbol, 0.0005)
    vol_per_min = max(fallback * 0.2, min(fallback * 5.0, vol_per_min))

    _vol_cache[symbol] = (now, vol_per_min)
    return vol_per_min


# ═══════════════════════════════════════════════════════════════════════════════
# COMBINED STRATEGY ENGINE
# Prioridade: Arbitrage > Stale Price > Market Making > Quant Direction
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class CombinedSignal:
    """Sinal combinado de todas as estratégias."""
    strategy: str          # "ARBITRAGE", "STALE_SNIPE", "MARKET_MAKING", "QUANT_DIRECTION"
    direction: str         # "YES", "NO", "SKIP", "BOTH" (para arb/mm)
    confidence: str        # "low", "medium", "high"
    edge: float
    fee_adjusted_edge: float
    strength: float
    momentum_1m: float
    momentum_5m: float
    rsi: float
    vwap_dev: float
    bb_position: float
    reasons: list[str]
    market_type: str
    symbol: str
    # Strategy-specific
    arb_signal: ArbitrageSignal | None = None
    mm_signal: MarketMakingSignal | None = None
    stale_signal: StalePriceSignal | None = None
    dynamic_vol: float = 0.0


def combined_decide(
    symbol: str,
    buf: PriceBuffer,
    market_price_yes: float,
    horizon_minutes: int,
    question: str = "",
    seconds_to_expiry: float | None = None,
    bid: float | None = None,
    ask: float | None = None,
    token_id: str = "",
    no_token_id: str = "",
    condition_id: str = "",
    no_price: float | None = None,
) -> CombinedSignal | None:
    """
    Motor combinado de decisão.
    Testa estratégias em ordem de prioridade e retorna a primeira que dispara.
    """
    price_now = buf.latest_price()
    if not price_now:
        return None

    # Dynamic vol (sempre calcular — usado por múltiplas estratégias)
    dyn_vol = calculate_dynamic_vol(symbol, buf)

    mid = (bid + ask) / 2 if bid and ask else market_price_yes

    # Track Polymarket price para stale detection
    if token_id:
        track_poly_price(token_id, mid)

    # ── PRIORIDADE 1: Arbitragem ─────────────────────────────────────────
    if no_price is not None and no_price > 0.01:
        yes_buy = ask if ask else market_price_yes
        no_buy = no_price  # TODO: need ask price for NO token

        arb = find_arbitrage(
            yes_price=yes_buy,
            no_price=no_buy,
            token_id=token_id,
            no_token_id=no_token_id,
            condition_id=condition_id,
            question=question,
            symbol=symbol,
            min_profit_pct=0.3,
        )
        if arb:
            print(f"[ARB] {symbol} YES={arb.yes_price:.3f} + NO={arb.no_price:.3f} = "
                  f"{arb.total_cost:.4f} profit={arb.profit_pct:.2f}%")
            return CombinedSignal(
                strategy="ARBITRAGE",
                direction="BOTH",
                confidence="high",
                edge=arb.profit_per_set,
                fee_adjusted_edge=arb.profit_per_set,
                strength=min(1.0, arb.profit_pct / 2.0),
                momentum_1m=0, momentum_5m=0, rsi=50,
                vwap_dev=0, bb_position=0.5,
                reasons=[
                    f"ARB: YES={arb.yes_price:.3f}+NO={arb.no_price:.3f}={arb.total_cost:.4f}",
                    f"Profit={arb.profit_pct:.2f}% guaranteed",
                ],
                market_type="price",
                symbol=symbol,
                arb_signal=arb,
                dynamic_vol=dyn_vol,
            )

    # ── PRIORIDADE 2: Stale Price Sniping ────────────────────────────────
    stale = detect_stale_price(
        symbol=symbol,
        buf=buf,
        token_id=token_id,
        market_price_yes=mid,
        horizon_minutes=horizon_minutes,
        question=question,
        seconds_to_expiry=seconds_to_expiry,
    )
    if stale and stale.fee_adjusted_edge >= 0.03:
        return CombinedSignal(
            strategy="STALE_SNIPE",
            direction=stale.direction,
            confidence=stale.confidence,
            edge=stale.edge,
            fee_adjusted_edge=stale.fee_adjusted_edge,
            strength=stale.strength,
            momentum_1m=stale.momentum_1m,
            momentum_5m=stale.momentum_5m,
            rsi=stale.rsi,
            vwap_dev=stale.vwap_dev,
            bb_position=stale.bb_position,
            reasons=stale.reasons,
            market_type="price",
            symbol=symbol,
            stale_signal=stale,
            dynamic_vol=dyn_vol,
        )

    # ── PRIORIDADE 3: Market Making ──────────────────────────────────────
    # NOTE: Market making requires placing limit orders on BOTH sides of the book.
    # In DRY_RUN/simulation mode, we can't do this — it just becomes a directional bet.
    # DISABLED until real CLOB order placement is implemented.
    # To enable: set ENABLE_MM = True and implement limit order placement.
    ENABLE_MM = False
    if ENABLE_MM and bid and ask and ask > bid:
        ref_price = buf.price_n_seconds_ago(int(horizon_minutes * 60)) or price_now
        mins_left = seconds_to_expiry / 60.0 if seconds_to_expiry and seconds_to_expiry > 0 else horizon_minutes

        from src.signals.quant_brain import calculate_fair_prob
        fair = calculate_fair_prob(price_now, ref_price, dyn_vol, mins_left)

        mm = market_making_signal(
            current_bid=bid,
            current_ask=ask,
            fair_price=fair,
            token_id=token_id,
            condition_id=condition_id,
            question=question,
            symbol=symbol,
        )
        if mm:
            direction = "YES" if fair > mid else "NO"
            return CombinedSignal(
                strategy="MARKET_MAKING",
                direction=direction,
                confidence="medium",
                edge=mm.edge_per_side,
                fee_adjusted_edge=mm.edge_per_side,
                strength=min(1.0, mm.edge_per_side / 0.05),
                momentum_1m=0, momentum_5m=0, rsi=50,
                vwap_dev=0, bb_position=0.5,
                reasons=[
                    f"MM: bid={mm.bid_price:.3f} ask={mm.ask_price:.3f} spread={mm.spread:.3f}",
                    f"Fair={mm.fair_price:.3f} edge/side={mm.edge_per_side:.3f}",
                ],
                market_type="price",
                symbol=symbol,
                mm_signal=mm,
                dynamic_vol=dyn_vol,
            )

    # ── PRIORIDADE 4: Quant Direction (fallback) ─────────────────────────
    # Usa o quant_brain mas com dynamic vol e thresholds mais altos
    try:
        from src.signals.quant_brain import quant_decide, VOL_PER_MINUTE

        # Override vol with dynamic
        old_vol = VOL_PER_MINUTE.get(symbol)
        VOL_PER_MINUTE[symbol] = dyn_vol

        sig = quant_decide(
            symbol=symbol,
            buf=buf,
            market_price_yes=mid,
            horizon_minutes=horizon_minutes,
            question=question,
            seconds_to_expiry=seconds_to_expiry,
            bid=bid,
            ask=ask,
        )

        # Restore
        if old_vol is not None:
            VOL_PER_MINUTE[symbol] = old_vol

        if sig and sig.direction != "SKIP":
            # ONLY use direction prediction for end-of-cycle (last 60s)
            # Mid-cycle direction prediction is ~50/50, not profitable
            is_eoc = getattr(sig, 'is_end_of_cycle', False)
            if not is_eoc:
                sig = None  # Skip mid-cycle direction bets entirely

        if sig and sig.direction != "SKIP":
            # Requer edge MAIOR para direção (já que é a estratégia menos confiável)
            # Also cap the edge claim (model overconfidence protection)
            capped_edge = min(sig.fee_adjusted_edge, 0.10)  # max 10% edge claim
            if capped_edge >= 0.06:  # 6% mínimo
                return CombinedSignal(
                    strategy="QUANT_DIRECTION",
                    direction=sig.direction,
                    confidence=sig.confidence,
                    edge=sig.edge,
                    fee_adjusted_edge=capped_edge,
                    strength=sig.strength,
                    momentum_1m=sig.momentum_1m,
                    momentum_5m=sig.momentum_5m,
                    rsi=sig.rsi,
                    vwap_dev=sig.vwap_dev,
                    bb_position=sig.bb_position,
                    reasons=[f"[DynVol={dyn_vol:.5f}] " + r for r in sig.reasons[:3]],
                    market_type=sig.market_type,
                    symbol=symbol,
                    dynamic_vol=dyn_vol,
                )
    except ImportError:
        pass

    return None
