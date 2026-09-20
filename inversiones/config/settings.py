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

# Claves de LLM y proveedor
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "local").lower()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-5")
KIMI_API_KEY = os.getenv("KIMI_API_KEY", "")
KIMI_MODEL = os.getenv("KIMI_MODEL", "kimi-k2.6")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-pro")
DEEPSEEK_FLASH_MODEL = os.getenv("DEEPSEEK_FLASH_MODEL", "deepseek-flash")
DEEPSEEK_PORTFOLIO_EFFORT = os.getenv("DEEPSEEK_PORTFOLIO_EFFORT", "high")
DEEPSEEK_DISCOVERY_EFFORT = os.getenv("DEEPSEEK_DISCOVERY_EFFORT", "low")
DEEPSEEK_PORTFOLIO_MAX_TOKENS = int(os.getenv("DEEPSEEK_PORTFOLIO_MAX_TOKENS", "2200"))
DEEPSEEK_DISCOVERY_MAX_TOKENS = int(os.getenv("DEEPSEEK_DISCOVERY_MAX_TOKENS", "4500"))
DEEPSEEK_MAX_CONCURRENCY = int(os.getenv("DEEPSEEK_MAX_CONCURRENCY", "2"))
DEEPSEEK_USAGE_FILE = DATA_DIR / "deepseek_usage.jsonl"
OPENCODE_GO_API_KEY = os.getenv("OPENCODE_GO_API_KEY", "")
OPENCODE_GO_MODEL = os.getenv("OPENCODE_GO_MODEL", "deepseek-v4-pro")
OPENCODE_GO_BASE_URL = os.getenv("OPENCODE_GO_BASE_URL", "https://opencode.ai/zen/go/v1")

# Conversión CLP → USD para el gráfico consolidado (ajustable vía .env)
USDCLP = float(os.getenv("USDCLP", "950"))

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
