"""
Interface com a API do Polymarket (Gamma API)
Busca mercados, preços e executa ordens
"""
import json
import requests
from src import config

GAMMA_URL = "https://gamma-api.polymarket.com/markets"


def get_client():
    """Em DRY_RUN sem private key, retorna None (leitura via REST)."""
    if not config.POLYGON_PRIVATE_KEY or config.POLYGON_PRIVATE_KEY.startswith("sua_"):
        if config.DRY_RUN:
            return None
        raise EnvironmentError("POLYGON_PRIVATE_KEY necessária para apostas reais")

    from py_clob_client.client import ClobClient
    from py_clob_client.constants import POLYGON
    return ClobClient(host=config.POLYMARKET_HOST, key=config.POLYGON_PRIVATE_KEY, chain_id=POLYGON)


def get_markets(client, limit: int = 20) -> list[dict]:
    """Retorna mercados ativos via Gamma API (sem autenticação)."""
    resp = requests.get(
        GAMMA_URL,
        params={"active": "true", "closed": "false", "limit": limit},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    return data if isinstance(data, list) else data.get("data", [])


def get_market_price(client, market: dict) -> float:
    """
    Extrai o preço do outcome 'Yes' direto do objeto de mercado.
    outcomePrices é uma string JSON: '["0.251", "0.749"]'
    """
    try:
        prices = json.loads(market.get("outcomePrices", "[0.5, 0.5]"))
        return float(prices[0])  # índice 0 = Yes
    except Exception:
        return float(market.get("lastTradePrice", 0.5))


def get_yes_token_id(market: dict) -> str | None:
    """Extrai o token_id do outcome 'Yes'."""
    try:
        tokens = json.loads(market.get("clobTokenIds", "[]"))
        return tokens[0] if tokens else None
    except Exception:
        return None


def place_order(client, token_id: str, side: str, amount_usdc: float) -> dict:
    """Coloca uma ordem de mercado (ou simula em DRY_RUN)."""
    if config.DRY_RUN:
        print(f"[DRY RUN] {side} ${amount_usdc} USDC no token {token_id[:16]}...")
        return {"status": "dry_run", "token_id": token_id, "side": side, "amount": amount_usdc}

    from py_clob_client.clob_types import MarketOrderArgs, OrderType
    order_args = MarketOrderArgs(token_id=token_id, amount=amount_usdc)
    signed_order = client.create_market_order(order_args)
    return client.post_order(signed_order, OrderType.FOK)