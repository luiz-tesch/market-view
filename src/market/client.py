"""
Interface com a API do Polymarket (CLOB)
Busca mercados, preços e executa ordens
"""
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds
from py_clob_client.constants import POLYGON
from src import config


def get_client() -> ClobClient:
    """Cria e retorna cliente autenticado do Polymarket."""
    creds = ApiCreds(
        api_key="",       # preenchido após set_api_credentials()
        api_secret="",
        api_passphrase="",
    )
    client = ClobClient(
        host=config.POLYMARKET_HOST,
        key=config.POLYGON_PRIVATE_KEY,
        chain_id=POLYGON,
    )
    return client


def get_markets(client: ClobClient, limit: int = 20) -> list[dict]:
    """
    Retorna mercados ativos com maior volume.
    Cada mercado tem: question, outcomes, prices, volume
    """
    resp = client.get_markets(next_cursor="", limit=limit)
    markets = resp.get("data", [])

    # Filtra apenas mercados abertos
    active = [m for m in markets if m.get("active") and not m.get("closed")]
    return active


def get_market_price(client: ClobClient, token_id: str) -> float:
    """Retorna o preço mid atual de um token (entre 0 e 1)."""
    book = client.get_midpoint(token_id=token_id)
    return float(book.get("mid", 0))


def place_order(client: ClobClient, token_id: str, side: str, amount_usdc: float) -> dict:
    """
    Coloca uma ordem de mercado.
    side: 'BUY' ou 'SELL'
    Retorna dict com resultado ou erro.
    """
    if config.DRY_RUN:
        print(f"[DRY RUN] Ordem simulada: {side} ${amount_usdc} USDC no token {token_id}")
        return {"status": "dry_run", "token_id": token_id, "side": side, "amount": amount_usdc}

    from py_clob_client.clob_types import MarketOrderArgs, OrderType
    order_args = MarketOrderArgs(
        token_id=token_id,
        amount=amount_usdc,
    )
    signed_order = client.create_market_order(order_args)
    result = client.post_order(signed_order, OrderType.FOK)
    return result