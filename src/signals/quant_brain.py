"""
src/signals/quant_brain.py — Cérebro quantitativo para decisão de trades

Substitui o LLM (que chutava com "high confidence") por:
1. Modelo log-normal de probabilidade justa (spot vs strike)
2. Order Book Imbalance (OBI) — indicador microestrutural mais comprovado
3. End-of-cycle sniping — só opera nos últimos 60s quando direção está ~definida
4. Gate de confirmação multi-indicador (3 de 5 devem concordar)
5. Mean reversion dominante em sub-15min (z-score Bollinger)

Baseado em pesquisa:
- DeepLOB papers (order book imbalance)
- Stoikov 2017 (microprice)
- Systematic trading literature (mean reversion < 30min)
- Polymarket fee structure analysis
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass

from src.signals.engine import PriceBuffer, polymarket_dynamic_fee, rsi, bollinger, bb_position_fn


# ── Volatilidade implícita por símbolo (calibrada para 1 minuto) ──────────────
# Fonte: desvio padrão de retornos de 1min em dados Binance
VOL_PER_MINUTE = {
    "BTC":   0.00035,   # ~0.035% por minuto
    "ETH":   0.00050,
    "SOL":   0.00070,
    "BNB":   0.00045,
    "MATIC": 0.00080,
    "DOGE":  0.00075,
    "XRP":   0.00060,
}


@dataclass
class QuantSignal:
    direction: str       # "YES", "NO", "SKIP"
    confidence: str      # "low", "medium", "high"
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
    # Extras do quant brain
    fair_prob: float
    obi: float           # Order Book Imbalance [-1, +1]
    z_score: float        # Bollinger z-score
    indicators_agreeing: int  # quantos indicadores concordam na direção
    is_end_of_cycle: bool


def _normal_cdf(z: float) -> float:
    """Approximação da CDF normal padrão via erf."""
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


def calculate_fair_prob(
    spot_now: float,
    spot_at_reference: float,
    vol_per_min: float,
    minutes_remaining: float,
    question: str = "",
) -> float:
    """
    Calcula probabilidade justa para mercados "Up or Down".
    Usa modelo log-normal: P(spot_close > spot_open) = N(z)
    onde z = ln(spot_now / spot_open) / (vol * sqrt(T))
    """
    if spot_at_reference is None or spot_at_reference <= 0 or spot_now is None or spot_now <= 0:
        return 0.5

    if minutes_remaining <= 0:
        return 1.0 if spot_now > spot_at_reference else 0.0

    # Para mercados quase expirando, o spot atual é muito preditivo
    # z-score: quantos desvios padrão o spot está acima do strike
    sigma = vol_per_min * math.sqrt(max(minutes_remaining, 0.1))
    z = math.log(spot_now / spot_at_reference) / sigma

    return max(0.02, min(0.98, _normal_cdf(z)))


def calculate_obi(buf: PriceBuffer) -> float:
    """
    Order Book Imbalance simplificado usando CVD.
    Em produção usaria order book real; aqui usa buy/sell pressure do CVD.
    Retorna valor em [-1, +1]: positivo = mais compra.
    """
    return buf.cvd_normalized(window=50)


def calculate_z_score(buf: PriceBuffer, period: int = 20) -> float:
    """
    Z-score de Bollinger: (price - SMA) / StdDev.
    Usado para mean reversion: z < -2 = oversold, z > +2 = overbought.
    """
    closes = buf.closes(period + 5)
    if len(closes) < period:
        return 0.0
    window = closes[-period:]
    mean = sum(window) / period
    variance = sum((x - mean) ** 2 for x in window) / period
    std = math.sqrt(variance) if variance > 0 else 0.001
    price = closes[-1]
    return (price - mean) / std


def _extract_reference_price(buf: PriceBuffer, horizon_minutes: float) -> float | None:
    """
    Pega o preço de referência (abertura do período do mercado).
    Para mercados "Up or Down" de 5min, é o preço de 5min atrás.
    """
    seconds = int(horizon_minutes * 60)
    return buf.price_n_seconds_ago(seconds)


def quant_decide(
    symbol: str,
    buf: PriceBuffer,
    market_price_yes: float,
    horizon_minutes: int,
    question: str = "",
    seconds_to_expiry: float | None = None,
    bid: float | None = None,
    ask: float | None = None,
) -> QuantSignal | None:
    """
    Decisor quantitativo que substitui o LLM.

    Estratégia core:
    1. Calcula probabilidade justa via modelo log-normal
    2. Compara com preço de mercado para achar edge
    3. Confirma com indicadores técnicos (gate 3/5)
    4. Prioriza end-of-cycle (últimos 60s)
    """
    price_now = buf.latest_price()
    if not price_now:
        return None

    vol = VOL_PER_MINUTE.get(symbol, 0.0005)
    fee = polymarket_dynamic_fee(market_price_yes)
    ticks = list(buf.ticks)
    n_ticks = len(ticks)

    # ── Precisa de dados suficientes ──
    if n_ticks < 30:
        return None

    # ── Referência de preço (abertura do período) ──
    ref_price = _extract_reference_price(buf, horizon_minutes)
    if ref_price is None or ref_price <= 0:
        ref_price = buf.price_n_seconds_ago(300) or price_now

    # ── Minutos restantes ──
    mins_left = seconds_to_expiry / 60.0 if seconds_to_expiry and seconds_to_expiry > 0 else horizon_minutes
    is_end_of_cycle = seconds_to_expiry is not None and 0 < seconds_to_expiry <= 60

    # ── 1. Probabilidade justa (modelo log-normal) ──
    fair_prob = calculate_fair_prob(price_now, ref_price, vol, mins_left, question)

    # ── 2. Edge bruto ──
    edge = fair_prob - market_price_yes   # positivo = YES subprecificado
    fee_adj_edge = abs(edge) - fee

    # ── 3. Order Book Imbalance ──
    obi = calculate_obi(buf)

    # ── 4. Z-score (mean reversion) ──
    z_score = calculate_z_score(buf, period=20)

    # ── 5. Indicadores técnicos ──
    closes = buf.closes(30)
    r = rsi(closes, period=min(14, max(3, len(closes) - 1)))

    # Momentum
    p1m = buf.price_n_seconds_ago(60)
    mom_1m = ((price_now / p1m) - 1) * 100 if p1m and p1m > 0 else 0.0
    p5m = buf.price_n_seconds_ago(300)
    mom_5m = ((price_now / p5m) - 1) * 100 if p5m and p5m > 0 else 0.0

    # VWAP
    vwap_val = buf.vwap(minutes=30) or price_now
    vwap_dev = (price_now / vwap_val - 1) * 100 if vwap_val else 0.0

    # Bollinger
    lower, mid_bb, upper = bollinger(closes, period=min(20, len(closes)))
    bb_pos = bb_position_fn(price_now, lower, upper)

    # EMA crossover
    ema_closes = buf.closes(60)
    ema9 = buf.ema(9, ema_closes)
    ema21 = buf.ema(21, ema_closes)
    ema_bullish = ema9 is not None and ema21 is not None and ema9 > ema21

    # ── 6. Gate de confirmação: contar quantos indicadores concordam ──
    # Determina direção sugerida pelo modelo
    direction_up = edge > 0  # fair_prob > market → YES (price going up)

    agreeing = 0
    total_checked = 0
    reasons = []

    # Indicador 1: Momentum 1min
    if abs(mom_1m) > 0.02:
        total_checked += 1
        if (mom_1m > 0) == direction_up:
            agreeing += 1
            reasons.append(f"Mom1m {'+' if mom_1m > 0 else '-'}{abs(mom_1m):.2f}%")

    # Indicador 2: OBI (Order Book Imbalance / CVD)
    if abs(obi) > 0.05:
        total_checked += 1
        if (obi > 0) == direction_up:
            agreeing += 1
            reasons.append(f"OBI {'buy' if obi > 0 else 'sell'} {obi:+.2f}")

    # Indicador 3: RSI não contradiz
    total_checked += 1
    rsi_ok = True
    if direction_up and r > 75:
        rsi_ok = False  # overbought contradiz YES
    elif not direction_up and r < 25:
        rsi_ok = False  # oversold contradiz NO
    if rsi_ok:
        agreeing += 1
        if r < 30:
            reasons.append(f"RSI oversold {r:.0f}")
        elif r > 70:
            reasons.append(f"RSI overbought {r:.0f}")

    # Indicador 4: EMA crossover
    if ema9 is not None and ema21 is not None:
        total_checked += 1
        if ema_bullish == direction_up:
            agreeing += 1
            reasons.append(f"EMA9/21 {'bull' if ema_bullish else 'bear'}")

    # Indicador 5: VWAP posição
    if abs(vwap_dev) > 0.05:
        total_checked += 1
        # Em mean reversion sub-15min: preço acima do VWAP tende a reverter para baixo
        # MAS no end-of-cycle, momentum domina
        if is_end_of_cycle:
            if (vwap_dev > 0) == direction_up:
                agreeing += 1
                reasons.append(f"VWAP {vwap_dev:+.2f}% (momentum)")
        else:
            # Mean reversion: preço acima VWAP = bearish
            if (vwap_dev < 0) == direction_up:
                agreeing += 1
                reasons.append(f"VWAP {vwap_dev:+.2f}% (reversion)")

    # ── 7. Decisão final ──

    # Edge mínimo depende do contexto
    if is_end_of_cycle:
        # End-of-cycle: edge menor necessário (alta previsibilidade)
        min_edge = 0.02  # 2% após fees
        min_agreeing = 2
        reasons.insert(0, f"END-OF-CYCLE T-{seconds_to_expiry:.0f}s")
    else:
        # Mid-cycle: precisa de mais edge e mais confirmação
        min_edge = 0.04  # 4% após fees
        min_agreeing = 3

    # Filtro near-50%: fee máxima mata o edge
    if abs(market_price_yes - 0.5) < 0.03 and fee > 0.030:
        return QuantSignal(
            direction="SKIP", confidence="low", edge=edge,
            fee_adjusted_edge=fee_adj_edge, strength=0,
            momentum_1m=mom_1m, momentum_5m=mom_5m, rsi=r,
            vwap_dev=vwap_dev, bb_position=bb_pos,
            reasons=[f"Near-50% fee={fee:.1%}"], market_type="price",
            symbol=symbol, fair_prob=fair_prob, obi=obi, z_score=z_score,
            indicators_agreeing=agreeing, is_end_of_cycle=is_end_of_cycle,
        )

    # Verifica edge e confirmação
    if fee_adj_edge < min_edge:
        return QuantSignal(
            direction="SKIP", confidence="low", edge=edge,
            fee_adjusted_edge=fee_adj_edge, strength=0,
            momentum_1m=mom_1m, momentum_5m=mom_5m, rsi=r,
            vwap_dev=vwap_dev, bb_position=bb_pos,
            reasons=[f"Edge {fee_adj_edge:.1%} < min {min_edge:.1%}"],
            market_type="price", symbol=symbol, fair_prob=fair_prob,
            obi=obi, z_score=z_score, indicators_agreeing=agreeing,
            is_end_of_cycle=is_end_of_cycle,
        )

    if agreeing < min_agreeing:
        return QuantSignal(
            direction="SKIP", confidence="low", edge=edge,
            fee_adjusted_edge=fee_adj_edge, strength=0,
            momentum_1m=mom_1m, momentum_5m=mom_5m, rsi=r,
            vwap_dev=vwap_dev, bb_position=bb_pos,
            reasons=[f"Confirmação {agreeing}/{min_agreeing} insuficiente"],
            market_type="price", symbol=symbol, fair_prob=fair_prob,
            obi=obi, z_score=z_score, indicators_agreeing=agreeing,
            is_end_of_cycle=is_end_of_cycle,
        )

    # ── Trade! ──
    direction = "YES" if edge > 0 else "NO"

    # Confiança baseada em edge + confirmação
    if fee_adj_edge > 0.08 and agreeing >= 4:
        confidence = "high"
    elif fee_adj_edge > 0.04 and agreeing >= 3:
        confidence = "medium"
    else:
        confidence = "low"

    # End-of-cycle boost
    if is_end_of_cycle and confidence == "medium":
        confidence = "high"

    strength = min(1.0, fee_adj_edge / 0.15)

    reasons.insert(0, f"Fair={fair_prob:.1%} vs Mkt={market_price_yes:.1%} edge={fee_adj_edge:.1%}")

    print(f"[Quant] {symbol} {direction} conf={confidence} fair={fair_prob:.3f} mkt={market_price_yes:.3f} "
          f"edge={fee_adj_edge:.3f} obi={obi:+.2f} z={z_score:+.2f} agree={agreeing}/{total_checked} "
          f"{'EOC' if is_end_of_cycle else ''} | {'; '.join(reasons[:3])}")

    return QuantSignal(
        direction=direction, confidence=confidence, edge=edge,
        fee_adjusted_edge=fee_adj_edge, strength=strength,
        momentum_1m=mom_1m, momentum_5m=mom_5m, rsi=r,
        vwap_dev=vwap_dev, bb_position=bb_pos,
        reasons=reasons, market_type="price", symbol=symbol,
        fair_prob=fair_prob, obi=obi, z_score=z_score,
        indicators_agreeing=agreeing, is_end_of_cycle=is_end_of_cycle,
    )
