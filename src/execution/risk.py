"""
Controle de risco: limita apostas por operação e por dia.
v2.0: Kelly fee-adjusted, quarter-Kelly para capital baixo, filtro horário.
"""
import json
import os
from datetime import date, datetime, timezone
from src import config
from src.signals.engine import polymarket_dynamic_fee

LEDGER_PATH = "logs/daily_ledger.json"


def _load_ledger() -> dict:
    if os.path.exists(LEDGER_PATH):
        with open(LEDGER_PATH) as f:
            return json.load(f)
    return {}


def _save_ledger(ledger: dict):
    os.makedirs("logs", exist_ok=True)
    with open(LEDGER_PATH, "w") as f:
        json.dump(ledger, f, indent=2)


def get_daily_spent() -> float:
    ledger = _load_ledger()
    today = str(date.today())
    return ledger.get(today, {}).get("spent", 0.0)


def register_bet(amount: float):
    ledger = _load_ledger()
    today = str(date.today())
    if today not in ledger:
        ledger[today] = {"spent": 0.0, "bets": []}
    ledger[today]["spent"] += amount
    ledger[today]["bets"].append(amount)
    _save_ledger(ledger)


def is_preferred_window() -> bool:
    """Janela de menor competição HFT: 02:00-08:00 UTC.
    Retorna True se o horário atual é favorável para capital baixo.
    """
    hour_utc = datetime.now(timezone.utc).hour
    return 2 <= hour_utc < 8


def calculate_bet_size(edge: float, confidence: str, current_price: float,
                       fee_adjusted_edge: float | None = None) -> float:
    """
    Quarter-Kelly fee-adjusted para capital baixo.

    Formula: f* = edge_ajustado / (1 - current_price)
    Usa 0.25x Kelly (quarter-Kelly) para reduzir variância com capital baixo.
    Máximo 10% do bankroll por trade.
    """
    if current_price >= 1.0:
        return 0.0

    # Usa fee_adjusted_edge se disponível; fallback para edge bruto menos fee estimada
    if fee_adjusted_edge is not None:
        effective_edge = max(0, fee_adjusted_edge)
    else:
        fee = polymarket_dynamic_fee(current_price)
        effective_edge = max(0, edge - fee)

    if effective_edge <= 0:
        return 0.0

    # Full Kelly
    full_kelly = effective_edge / (1 - current_price)
    # Quarter-Kelly: reduz variância drasticamente para capital baixo
    quarter_kelly = full_kelly * 0.25
    quarter_kelly = max(0, min(quarter_kelly, 0.10))  # cap em 10% do bankroll

    # Multiplicador por confiança
    multiplier = {"high": 1.0, "medium": 0.6, "low": 0.30}.get(confidence, 0.25)
    bet = quarter_kelly * config.MAX_BET_USDC * multiplier

    # Bônus de 20% na janela de menor competição (02-08 UTC)
    if is_preferred_window():
        bet *= 1.20

    # Verifica limite diário
    daily_remaining = config.MAX_DAILY_USDC - get_daily_spent()
    bet = min(bet, daily_remaining, config.MAX_BET_USDC)

    return round(max(bet, 0), 2)


def can_bet(amount: float) -> tuple[bool, str]:
    """Retorna (pode_apostar, motivo)."""
    if amount <= 0:
        return False, "Valor calculado é zero ou negativo"
    daily_spent = get_daily_spent()
    if daily_spent + amount > config.MAX_DAILY_USDC:
        return False, f"Limite diário atingido (${daily_spent:.2f}/${config.MAX_DAILY_USDC:.2f})"
    return True, "OK"