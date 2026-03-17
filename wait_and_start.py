"""
wait_and_start.py — Aguarda saldo USDC aparecer e inicia o bot automaticamente.
"""
import os
import sys
import time
import subprocess
from dotenv import load_dotenv

load_dotenv()

PRIVATE_KEY   = os.getenv("POLYGON_PRIVATE_KEY", "")
CLOB_URL      = "https://clob.polymarket.com"
MIN_BALANCE   = 1.0   # USDC mínimo para iniciar
POLL_INTERVAL = 15    # segundos entre checagens

def get_usdc_balance() -> float:
    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
        client = ClobClient(host=CLOB_URL, key=PRIVATE_KEY, chain_id=137, signature_type=0)
        try:
            creds = client.derive_api_key()
        except Exception:
            creds = client.create_api_key()
        client.set_api_creds(creds)
        r = client.get_balance_allowance(
            params=BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        )
        raw = r.get("balance", "0")
        return float(raw) / 1e6  # USDC tem 6 casas decimais
    except Exception as e:
        print(f"  [ERRO] {e}")
        return 0.0

def main():
    print("=" * 50)
    print("  AGUARDANDO SALDO USDC...")
    print(f"  Minimo para iniciar: ${MIN_BALANCE:.2f} USDC")
    print(f"  Checando a cada {POLL_INTERVAL}s")
    print("=" * 50)

    attempt = 0
    while True:
        attempt += 1
        bal = get_usdc_balance()
        ts = time.strftime("%H:%M:%S")
        print(f"  [{ts}] tentativa #{attempt} | saldo: ${bal:.4f} USDC", flush=True)

        if bal >= MIN_BALANCE:
            print(f"\n  SALDO DETECTADO: ${bal:.4f} USDC")
            print("  Iniciando bot_mm.py...\n")
            subprocess.run([sys.executable, "bot_mm.py"])
            break

        time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    main()
