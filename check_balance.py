"""
check_balance.py — Verifica saldo e allowance via CLOB API do Polymarket
"""
import os
import httpx
from dotenv import load_dotenv

load_dotenv()

PRIVATE_KEY = os.getenv("POLYGON_PRIVATE_KEY", "")
WALLET      = os.getenv("POLYGON_ADDRESS", "")
CLOB_URL    = "https://clob.polymarket.com"

def main():
    print(f"\nCarteira: {WALLET}")
    print("-" * 55)

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

    # Saldo USDC disponível
    try:
        from py_clob_client.constants import USDC
        bal = client.get_balance_allowance(params={"asset_type": USDC})
        print(f"  Saldo/Allowance USDC:")
        if isinstance(bal, dict):
            for k, v in bal.items():
                print(f"    {k}: {v}")
        else:
            print(f"    {bal}")
    except Exception as e:
        print(f"  get_balance_allowance erro: {e}")
        # fallback sem params
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
        else:
            print(f"  trading-stats: {resp.status_code} {resp.text[:200]}")
    except Exception as e:
        print(f"  trading-stats erro: {e}")

    print("-" * 55)

if __name__ == "__main__":
    main()
