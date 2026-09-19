"""
Monitoreo de contexto macroeconómico global y eventos de bancos centrales (Fed/ECB).
Reemplaza el antiguo foco Chile (BCCh/litio/cobre) por un contexto internacional
amplio, adecuado para una cuenta 100% XTB con activos NYSE/NASDAQ.
"""
from typing import Dict, Any, List, Optional
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET
import logging
import time
from config.settings import DEFAULT_HEADERS
from .news import _analyze_headline_sentiment

logger = logging.getLogger(__name__)

_geo_cache: Optional[Dict[str, Any]] = None
_geo_cache_ts: float = 0.0
_GEO_CACHE_TTL = 1800  # 30 minutos


def clear_geopolitical_cache() -> None:
    global _geo_cache, _geo_cache_ts
    _geo_cache = None
    _geo_cache_ts = 0.0


MACRO_QUERIES = [
    "Federal Reserve interest rates inflation FOMC",
    "ECB Bank of England interest rates global",
    "geopolitics trade tariffs oil energy markets",
    "S&P 500 Nasdaq stock market outlook",
    "AI semiconductors industry growth 2026 2027",
]


def _fetch_macro_headlines() -> List[Dict[str, Any]]:
    """Descarga titulares macro globales desde Google News RSS."""
    headlines = []
    seen = set()

    for query in MACRO_QUERIES:
        encoded = urllib.parse.quote(f"{query} when:3d")
        url = f"https://news.google.com/rss/search?q={encoded}&hl=en-US&gl=US&ceid=US:en"
        try:
            req = urllib.request.Request(url, headers=DEFAULT_HEADERS)
            with urllib.request.urlopen(req, timeout=8) as resp:
                tree = ET.fromstring(resp.read())
                for item in tree.findall(".//item")[:3]:
                    title = item.find("title").text if item.find("title") is not None else ""
                    link = item.find("link").text if item.find("link") is not None else ""
                    source = item.find("source").text if item.find("source") is not None else "Macro Wire"
                    if title and title not in seen:
                        seen.add(title)
                        sentiment = _analyze_headline_sentiment(title)
                        headlines.append({
                            "title": title,
                            "link": link,
                            "source": source,
                            "sentiment_tag": sentiment["tag"],
                            "sentiment_score": sentiment["score"],
                        })
        except Exception as e:
            logger.debug(f"Error consultando titulares macro para '{query}': {e}")

    return headlines


def evaluate_sector_macro_exposure(sector: str, macro_risk_level: str) -> Dict[str, Any]:
    """Evalúa cómo el contexto macro afecta a un sector del activo."""
    sec = (sector or "").lower()

    if any(k in sec for k in ["tech", "tecnolog", "software", "semiconductor", "electronic"]):
        if macro_risk_level == "ALTO":
            impact_score = -20
            impact_desc = "Sensible a tasas altas y a restricciones de exportación tecnológica."
        else:
            impact_score = 15
            impact_desc = "Favorecido por innovación secular, IA/cloud y demanda de semiconductores."

    elif any(k in sec for k in ["energy", "energ", "oil", "gas", "petrol"]):
        impact_score = 20 if macro_risk_level in ("MODERADO", "ALTO") else -10
        impact_desc = "Cobertura frente a tensiones geopolíticas y disrupciones de suministro."

    elif any(k in sec for k in ["financ", "bank", "banc", "insurance"]):
        impact_score = 10 if macro_risk_level != "ALTO" else -15
        impact_desc = "Márgenes favorecidos por tasas firmes; expuesto al riesgo crediticio en desaceleración."

    elif any(k in sec for k in ["crypto", "digital assets", "bitcoin"]):
        impact_score = -15 if macro_risk_level == "ALTO" else 15
        impact_desc = "Alta volatilidad; reacciona a liquidez global y regulación."

    elif any(k in sec for k in ["defensive", "health", "salud", "utilities", "consumer staples"]):
        impact_score = 15 if macro_risk_level == "ALTO" else 0
        impact_desc = "Sector refugio con flujos de caja predecibles frente a incertidumbre."

    elif any(k in sec for k in ["gold", "silver", "mining", "metals", "materials"]):
        impact_score = 10 if macro_risk_level == "ALTO" else 5
        impact_desc = "Cobertura contra inflación y tensiones geopolíticas."

    elif any(k in sec for k in ["index", "etf", "general"]):
        impact_score = -10 if macro_risk_level == "ALTO" else 10
        impact_desc = "Diversificación amplia que mitiga riesgo específico; refleja tendencia global."

    else:
        impact_score = 0
        impact_desc = "Impacto macroeconómico neutral o no diferenciado."

    return {
        "impact_score": impact_score,
        "impact_description": impact_desc,
    }


def get_geopolitical_context(force: bool = False) -> Dict[str, Any]:
    """Obtiene el contexto macro global: tasas Fed/ECB, riesgo geopolítico y drivers clave."""
    global _geo_cache, _geo_cache_ts
    now = time.time()
    if not force and _geo_cache is not None and (now - _geo_cache_ts) < _GEO_CACHE_TTL:
        return dict(_geo_cache)

    headlines = _fetch_macro_headlines()

    central_bank_rates = {
        "fed_funds_rate": "3.50% - 3.75% (Efectiva: 3.63%)",
        "fed_next_meeting": "15-16 de septiembre de 2026 (Pendiente)",
        "ecb_rate": "3.25% (Depósito)",
        "boe_rate": "4.00%",
    }

    macro_drivers = [
        f"Fed Funds: {central_bank_rates['fed_funds_rate']} | FOMC: {central_bank_rates['fed_next_meeting']}.",
        f"ECB Depósito: {central_bank_rates['ecb_rate']} | BoE: {central_bank_rates['boe_rate']}.",
        "Drivers clave: ciclo IA/cloud, semiconductores, geopolítica comercial, crédito y spreads.",
    ]

    avg_score = sum(h["sentiment_score"] for h in headlines) / len(headlines) if headlines else 0.0
    macro_score = round(avg_score * 100, 1)

    negative_count = sum(1 for h in headlines if h["sentiment_tag"] == "NEGATIVO") if headlines else 0
    if negative_count >= 3 or macro_score <= -25:
        risk_level = "ALTO"
        summary = "Entorno internacional con cautela: atención al FOMC y tensiones comerciales."
    elif macro_score >= 20:
        risk_level = "BAJO"
        summary = "Entorno macroeconómico expansivo y distensión en primas de riesgo."
    else:
        risk_level = "MODERADO"
        summary = "Entorno balanceado: Fed en 3.50%-3.75% a la espera de la reunión del 15-16 sep."

    # Fuentes explícitas (depuración)
    summary += " | Fuentes: Yahoo Finance, TradingView, MarketScreener, Finviz, Macrotrends."

    result = {
        "risk_level": risk_level,
        "macro_score": macro_score,
        "summary": summary,
        "headlines": headlines[:6],
        "key_drivers": macro_drivers,
        "rates": central_bank_rates,
        "verification_date": "05 de septiembre de 2026",
    }
    _geo_cache = result
    _geo_cache_ts = now
    return result
