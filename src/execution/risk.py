"""
Controle de risco: limita apostas por operação e por dia
"""
import json
import os
from datetime import date
from src import config

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


def calculate_bet_size(edge: float, confidence: str, current_price: float) -> float:
    """
    Kelly Criterion simplificado:
    f = edge / (1 - current_price)
    Limitado pelo MAX_BET_USDC e saldo diário restante.
    """
    if current_price >= 1.0:
        return 0.0

    kelly = edge / (1 - current_price)
    kelly = max(0, min(kelly, 0.25))  # máximo 25% Kelly

    # Reduz por confiança
    multiplier = {"high": 1.0, "medium": 0.5, "low": 0.25}.get(confidence, 0.25)
    bet = kelly * config.MAX_BET_USDC * multiplier

    # Verifica limite diário
    daily_remaining = config.MAX_DAILY_USDC - get_daily_spent()
    bet = min(bet, daily_remaining, config.MAX_BET_USDC)

    return round(bet, 2)


def can_bet(amount: float) -> tuple[bool, str]:
    """Retorna (pode_apostar, motivo)."""
    if amount <= 0:
        return False, "Valor calculado é zero ou negativo"
    daily_spent = get_daily_spent()
    if daily_spent + amount > config.MAX_DAILY_USDC:
        return False, f"Limite diário atingido (${daily_spent:.2f}/${config.MAX_DAILY_USDC:.2f})"
    return True, "OK"