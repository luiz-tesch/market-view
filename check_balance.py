"""
check_balance.py — Verifica saldo CLOB + saldo real on-chain (ERC1155) para diagnóstico de SELL.

Uso:
  python check_balance.py                          # verifica tudo
  python check_balance.py <token_id>               # foca em um token específico
"""
import os
import sys
import httpx
from dotenv import load_dotenv

load_dotenv()

PRIVATE_KEY = os.getenv("POLYGON_PRIVATE_KEY", "")
WALLET      = os.getenv("POLYGON_ADDRESS", "")
CLOB_URL    = "https://clob.polymarket.com"

# CTF (ConditionalTokens) contract on Polygon — ERC1155 com todas as shares
CTF_ADDRESS  = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
POLYGON_RPC  = os.getenv("POLYGON_RPC_URL", "https://1rpc.io/matic")

ERC1155_ABI = [
    {
        "inputs": [
            {"internalType": "address", "name": "account", "type": "address"},
            {"internalType": "uint256", "name": "id",      "type": "uint256"},
        ],
        "name": "balanceOf",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    }
]


def check_onchain_balance(wallet: str, token_id: str) -> tuple[int, str]:
    """
    Consulta o saldo real on-chain via ERC1155.balanceOf().
    Retorna (saldo_bruto, mensagem).
    """
    try:
        from web3 import Web3
        w3 = Web3(Web3.HTTPProvider(POLYGON_RPC, request_kwargs={"timeout": 10}))
        if not w3.is_connected():
            return -1, f"RPC desconectado ({POLYGON_RPC})"

        ctf = w3.eth.contract(
            address=Web3.to_checksum_address(CTF_ADDRESS),
            abi=ERC1155_ABI,
        )
        # token_id pode vir como string decimal longa (77 dígitos)
        tid_int = int(token_id)
        bal = ctf.functions.balanceOf(Web3.to_checksum_address(wallet), tid_int).call()
        # Conditional tokens têm 1 share = 1 unidade (sem decimais, diferente de USDC)
        return bal, "OK"
    except Exception as e:
        return -1, str(e)


def diagnose_token(client, token_id: str) -> None:
    """Diagnóstico completo de um token: API positions, orderbook, on-chain balance, allowance."""
    print(f"\n{'='*60}")
    print(f"  DIAGNÓSTICO TOKEN ...{token_id[-16:]}")
    print(f"  ID completo: {token_id}")
    print(f"{'='*60}")

    # 1. Saldo on-chain (fonte de verdade)
    bal_raw, msg = check_onchain_balance(WALLET, token_id)
    if bal_raw >= 0:
        print(f"  [ON-CHAIN] ERC1155 balanceOf = {bal_raw} shares (1 share = 1 unidade)")
        if bal_raw == 0:
            print("  *** SALDO ON-CHAIN ZERO — SELL vai falhar com 'not enough balance' ***")
            print("  *** Causa provável: mercado resolvido ou shares nunca chegaram ***")
        else:
            print(f"  OK — {bal_raw} shares disponíveis para vender on-chain")
    else:
        print(f"  [ON-CHAIN] Erro ao consultar: {msg}")

    # 2. Allowance via CLOB API
    try:
        from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
        allow = client.get_balance_allowance(
            params=BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL, token_id=token_id)
        )
        print(f"  [CLOB API] balance_allowance = {allow}")
    except Exception as e:
        print(f"  [CLOB API] Erro allowance: {e}")

    # 3. Orderbook atual
    try:
        r = httpx.get(f"{CLOB_URL}/book?token_id={token_id}", timeout=5)
        if r.status_code == 200:
            book = r.json()
            bids = book.get("bids", [])
            asks = book.get("asks", [])
            best_bid = float(bids[0]["price"]) if bids else 0.0
            best_ask = float(asks[0]["price"]) if asks else 0.0
            print(f"  [ORDERBOOK] best_bid={best_bid:.4f}  best_ask={best_ask:.4f}  bids={len(bids)}  asks={len(asks)}")
            if not bids and not asks:
                print("  *** ORDERBOOK VAZIO — mercado provavelmente fechado/resolvido ***")
        else:
            print(f"  [ORDERBOOK] HTTP {r.status_code}")
    except Exception as e:
        print(f"  [ORDERBOOK] Erro: {e}")

    # 4. Posições via Data API
    try:
        r = httpx.get(
            f"https://data-api.polymarket.com/positions?user={WALLET}",
            timeout=10,
        )
        if r.status_code == 200:
            positions = r.json() or []
            found = [p for p in positions if p.get("asset") == token_id]
            if found:
                p = found[0]
                print(f"  [DATA API] size={p.get('size')}  avgPrice={p.get('avgPrice')}  outcome={p.get('outcome')}")
            else:
                print("  [DATA API] Token não encontrado nas posições da API")
        else:
            print(f"  [DATA API] HTTP {r.status_code}")
    except Exception as e:
        print(f"  [DATA API] Erro: {e}")

    # 5. Recomendação
    print(f"\n  CONCLUSÃO:")
    if bal_raw == 0:
        print("  → Saldo on-chain é ZERO. A API de posições reporta shares que não existem mais.")
        print("  → Solução: remover posição da memória do bot (clear_position) e ignorar SELL.")
    elif bal_raw > 0:
        print(f"  → {bal_raw} shares existem on-chain. SELL deveria funcionar.")
        print("  → Execute update_balance_allowance para o CLOB reconhecer e tente novamente.")
    print(f"{'='*60}")


def main() -> None:
    focus_tokens = sys.argv[1:] if len(sys.argv) > 1 else []

    print(f"\nCarteira: {WALLET}")
    print(f"RPC Polygon: {POLYGON_RPC}")
    print("-" * 55)

    # Autentica no CLOB
    try:
        from py_clob_client.client import ClobClient
        client = ClobClient(host=CLOB_URL, key=PRIVATE_KEY, chain_id=137, signature_type=0)
        try:
            creds = client.derive_api_key()
        except Exception:
            creds = client.create_api_key()
        client.set_api_creds(creds)
        print("  CLOB: autenticado OK")
    except Exception as e:
        print(f"  CLOB auth falhou: {e}")
        return

    # Saldo USDC
    try:
        from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
        bal = client.get_balance_allowance(
            params=BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        )
        usdc = float(bal.get("balance", "0")) / 1e6
        print(f"  USDC livre: ${usdc:.4f}")
        print(f"  USDC allowance raw: {bal}")
    except Exception as e:
        print(f"  Saldo USDC erro: {e}")
        # fallback
        try:
            bal = client.get_balance_allowance()
            print(f"  Balance raw: {bal}")
        except Exception as e2:
            print(f"  fallback erro: {e2}")

    # Ordens abertas
    try:
        orders = client.get_orders()
        print(f"  Ordens abertas: {len(orders) if orders else 0}")
        if orders:
            for o in (orders or [])[:5]:
                print(f"    › {o}")
    except Exception as e:
        print(f"  get_orders erro: {e}")

    # Posições via Data API + saldo on-chain
    print("\n  Posições abertas (Data API):")
    try:
        r = httpx.get(
            f"https://data-api.polymarket.com/positions?user={WALLET}",
            timeout=10,
        )
        positions = r.json() if r.status_code == 200 else []
        if not positions:
            print("    nenhuma posição")
        else:
            for p in positions:
                asset = p.get("asset", "")
                size  = float(p.get("size", 0))
                avg   = float(p.get("avgPrice", 0))
                bal_raw, _ = check_onchain_balance(WALLET, asset)
                onchain_tag = f"[on-chain={bal_raw}]" if bal_raw >= 0 else "[on-chain=ERR]"
                print(f"    ...{asset[-12:]}  size={size:.2f}  avg={avg:.4f}  {onchain_tag}")
    except Exception as e:
        print(f"  positions erro: {e}")

    # Trading stats
    try:
        resp = httpx.get(
            f"{CLOB_URL}/data/trading-stats",
            params={"maker_address": WALLET},
            timeout=10,
        )
        if resp.status_code == 200:
            stats = resp.json()
            print(f"\n  Trading stats:")
            for k, v in stats.items():
                print(f"    {k}: {v}")
    except Exception as e:
        print(f"  trading-stats erro: {e}")

    print("-" * 55)

    # Diagnóstico detalhado — tokens especificados na linha de comando ou todos com posição
    if focus_tokens:
        for tid in focus_tokens:
            diagnose_token(client, tid)
    else:
        # Diagnóstica tokens com bid muito baixo (suspeitos)
        try:
            r = httpx.get(
                f"https://data-api.polymarket.com/positions?user={WALLET}",
                timeout=10,
            )
            positions = r.json() if r.status_code == 200 else []
            for p in (positions or []):
                asset = p.get("asset", "")
                avg   = float(p.get("avgPrice", 0))
                if avg > 0.05:  # só diagnostica posições com custo relevante
                    diagnose_token(client, asset)
        except Exception:
            pass


if __name__ == "__main__":
    main()
