"""
Carrega e valida todas as configurações do .env
"""
import os
from dotenv import load_dotenv

load_dotenv()

# --- Credenciais ---
POLYGON_PRIVATE_KEY = os.getenv("POLYGON_PRIVATE_KEY")
POLYGON_ADDRESS = os.getenv("POLYGON_ADDRESS")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
NEWS_API_KEY = os.getenv("NEWS_API_KEY")

# --- Parâmetros de risco ---
MAX_BET_USDC = float(os.getenv("MAX_BET_USDC", "10"))
MAX_DAILY_USDC = float(os.getenv("MAX_DAILY_USDC", "50"))
MIN_EDGE = float(os.getenv("MIN_EDGE", "0.05"))

# --- Ambiente ---
DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

# --- Polymarket ---
POLYMARKET_HOST = "https://clob.polymarket.com"
CHAIN_ID = 137  # Polygon mainnet

def validate():
    """Verifica se as credenciais obrigatórias estão configuradas."""
    errors = []
    if not POLYGON_PRIVATE_KEY:
        errors.append("POLYGON_PRIVATE_KEY não configurada")
    if not GROQ_API_KEY:
        errors.append("GROQ_API_KEY não configurada (gratuito em console.groq.com)")
    if errors:
        raise EnvironmentError("Configuração incompleta:\n" + "\n".join(f"  ❌ {e}" for e in errors))
    print("✅ Configuração validada com sucesso")
    if DRY_RUN:
        print("⚠️  Modo DRY_RUN ativo — nenhuma aposta real será feita")