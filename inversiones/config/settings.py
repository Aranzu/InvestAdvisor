"""
Configuración central del Analizador de Inversiones.
Carga variables de entorno y define parámetros del motor de análisis y fuentes de datos.
"""
import os
from pathlib import Path
from dotenv import load_dotenv

# Cargar .env si existe
BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

# Rutas de datos
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
DEFAULT_PORTFOLIO_FILE = BASE_DIR / "config" / "default_portfolio.json"

# Configuración del servidor web
HOST = os.getenv("HOST", "127.0.0.1")
PORT = int(os.getenv("PORT", "5000"))
DEBUG = os.getenv("DEBUG", "False").lower() in ("true", "1", "yes")

# Pesos del motor de recomendación (Normalizados a 1.0)
WEIGHTS = {
    "technical": float(os.getenv("WEIGHT_TECHNICAL", "0.30")),
    "fundamental": float(os.getenv("WEIGHT_FUNDAMENTAL", "0.35")),
    "news": float(os.getenv("WEIGHT_NEWS", "0.20")),
    "geopolitical": float(os.getenv("WEIGHT_GEOPOLITICAL", "0.15")),
}

# Claves de LLM y proveedor (SOLO gratuitos: Groq primario, OpenRouter como fallback)
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "groq").lower()
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
GROQ_FLASH_MODEL = os.getenv("GROQ_FLASH_MODEL", "openai/gpt-oss-20b")
GROQ_PORTFOLIO_MAX_TOKENS = int(os.getenv("GROQ_PORTFOLIO_MAX_TOKENS", "2500"))
GROQ_DISCOVERY_MAX_TOKENS = int(os.getenv("GROQ_DISCOVERY_MAX_TOKENS", "4500"))
GROQ_MAX_CONCURRENCY = int(os.getenv("GROQ_MAX_CONCURRENCY", "2"))
GROQ_USAGE_FILE = DATA_DIR / "groq_usage.jsonl"
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "nvidia/nemotron-3-super-120b-a12b:free")
OPENROUTER_PORTFOLIO_MAX_TOKENS = int(os.getenv("OPENROUTER_PORTFOLIO_MAX_TOKENS", "2500"))
OPENROUTER_DISCOVERY_MAX_TOKENS = int(os.getenv("OPENROUTER_DISCOVERY_MAX_TOKENS", "2400"))
OPENROUTER_BASE_URL = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")

# Conversión CLP → USD para el gráfico consolidado (ajustable vía .env)
USDCLP = float(os.getenv("USDCLP", "950"))

# ── Perfil de riesgo (tope de pérdida máxima por posición) ────────────────────
# Define cuánta pérdida adicional admite un stop estructural frente al precio
# actual: el SL nunca queda a más de `max_loss_pct`% por debajo del precio, sin
# importar cuán lejos quede el soporte técnico − k×ATR. Cada perfil también
# ajusta el múltiplo del Chandelier (protección de ganancias): mayor múltiplo =
# más colchón contra el ruido (deja correr más la ganancia, mejor en beta alta);
# y el gap mínimo frente al precio (no dejar un stop pegado al ruido).
# Selección vía RISK_PROFILE en .env (default: "moderado").
RISK_PROFILE = os.getenv("RISK_PROFILE", "moderado").lower()
RISK_PROFILES = {
    "agresivo": {"max_loss_pct": 15.0, "chandelier_atr_multiple": 3.0, "chandelier_min_gap_atr": 1.0},
    "moderado": {"max_loss_pct": 10.0, "chandelier_atr_multiple": 2.0, "chandelier_min_gap_atr": 1.0},
    "conservador": {"max_loss_pct": 7.0, "chandelier_atr_multiple": 2.0, "chandelier_min_gap_atr": 1.5},
}

# Fuentes de noticias y feeds RSS
RSS_FEEDS = {
    "yahoo_finance": "https://finance.yahoo.com/news/rssindex",
    "google_news_finance": "https://news.google.com/rss/headlines/section/topic/BUSINESS?hl=en-US&gl=US&ceid=US:en",
    "google_news_macro": "https://news.google.com/rss/search?q=geopolitics+inflation+interest+rates+global+economy+when:3d&hl=en-US&gl=US&ceid=US:en",
    "reuters_business": "https://www.reutersagency.com/feed/?best-topics=business-finance&post_type=best",
}

# Cabeceras HTTP estándar para scrapers y feeds
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
}
