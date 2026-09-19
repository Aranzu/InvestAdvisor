"""
Extracción de noticias y análisis de sentimiento financiero (Yahoo Finance, Google News RSS, Bloomberg/Investing).
"""
from typing import List, Dict, Any
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET
import logging
import time
import yfinance as yf
from textblob import TextBlob
from config.settings import DEFAULT_HEADERS
from data_fetchers.web_sources import to_yfinance_ticker

logger = logging.getLogger(__name__)

_news_cache: Dict[str, Any] = {}
_NEWS_CACHE_TTL = 900  # 15 minutos


def clear_news_cache() -> None:
    _news_cache.clear()

# Palabras clave financieras con sesgo alcista o bajista
BULLISH_KEYWORDS = {
    "surge", "rally", "soar", "gain", "profit", "beat", "beats", "record",
    "upgrade", "growth", "breakout", "bullish", "expansion", "buyback",
    "dividend", "milestone", "top pick", "outperform", "higher", "rebound"
}

BEARISH_KEYWORDS = {
    "slump", "plunge", "fall", "drop", "loss", "losses", "miss", "misses",
    "downgrade", "decline", "warning", "bearish", "crash", "probe", "lawsuit",
    "investigation", "tariff", "risk", "debt", "layoffs", "underperform", "fraud"
}


def _analyze_headline_sentiment(text: str) -> Dict[str, Any]:
    """Calcula el sentimiento de un titular combinando TextBlob y palabras clave financieras."""
    lower_text = text.lower()
    
    # 1. Análisis léxico por palabras clave
    bull_count = sum(1 for kw in BULLISH_KEYWORDS if kw in lower_text)
    bear_count = sum(1 for kw in BEARISH_KEYWORDS if kw in lower_text)
    keyword_bias = (bull_count - bear_count) * 0.25

    # 2. TextBlob polaridad (-1.0 a 1.0)
    blob = TextBlob(text)
    base_polarity = blob.sentiment.polarity

    # Ponderación combinada
    combined_score = max(-1.0, min(1.0, (base_polarity * 0.5) + keyword_bias))

    if combined_score > 0.15:
        tag = "POSITIVO"
    elif combined_score < -0.15:
        tag = "NEGATIVO"
    else:
        tag = "NEUTRAL"

    return {
        "score": round(combined_score, 2),
        "tag": tag,
    }


def _fetch_rss_news(query: str, max_items: int = 5) -> List[Dict[str, Any]]:
    """Consulta Google News RSS para encontrar noticias recientes con fuentes como Bloomberg, Investing, Reuters."""
    encoded_query = urllib.parse.quote(query)
    rss_url = f"https://news.google.com/rss/search?q={encoded_query}+when:5d&hl=en-US&gl=US&ceid=US:en"
    
    articles = []
    try:
        req = urllib.request.Request(rss_url, headers=DEFAULT_HEADERS)
        with urllib.request.urlopen(req, timeout=8) as response:
            xml_data = response.read()
            root = ET.fromstring(xml_data)
            
            for item in root.findall(".//item")[:max_items]:
                title = item.find("title").text if item.find("title") is not None else ""
                link = item.find("link").text if item.find("link") is not None else ""
                pub_date = item.find("pubDate").text if item.find("pubDate") is not None else ""
                source_elem = item.find("source")
                source_name = source_elem.text if source_elem is not None else "Google News"

                if title:
                    sentiment = _analyze_headline_sentiment(title)
                    articles.append({
                        "title": title,
                        "link": link,
                        "publisher": source_name,
                        "published": pub_date[:16] if pub_date else "Reciente",
                        "sentiment_tag": sentiment["tag"],
                        "sentiment_score": sentiment["score"],
                    })
    except Exception as e:
        logger.debug(f"Error consultando RSS para {query}: {e}")

    return articles


def get_position_news(ticker: str, company_name: str = "") -> Dict[str, Any]:
    """
    Obtiene noticias relevantes de múltiples fuentes (Yahoo Finance y Google News RSS)
    y calcula el score de sentimiento general (-100 a +100).
    """
    clean_ticker = ticker.strip().upper()
    cache_key = f"{clean_ticker}:{company_name.strip()}"
    now = time.time()
    cached = _news_cache.get(cache_key)
    if cached and (now - cached[0]) < _NEWS_CACHE_TTL:
        return dict(cached[1])

    articles: List[Dict[str, Any]] = []
    seen_titles = set()

    # 1. Noticias de Yahoo Finance
    try:
        yf_symbol = to_yfinance_ticker(clean_ticker)
        yf_ticker = yf.Ticker(yf_symbol)
        yf_news = yf_ticker.news or []
        for item in yf_news[:5]:
            content = item.get("content", {})
            title = content.get("title") or item.get("title")
            if title and title not in seen_titles:
                seen_titles.add(title)
                publisher = (
                    content.get("provider", {}).get("displayName")
                    or item.get("publisher")
                    or "Yahoo Finance"
                )
                link = (
                    content.get("canonicalUrl", {}).get("url")
                    or item.get("link")
                    or ""
                )
                sentiment = _analyze_headline_sentiment(title)
                articles.append({
                    "title": title,
                    "link": link,
                    "publisher": publisher,
                    "published": "Hoy / Reciente",
                    "sentiment_tag": sentiment["tag"],
                    "sentiment_score": sentiment["score"],
                })
    except Exception as e:
        logger.debug(f"Error extrayendo noticias Yahoo para {clean_ticker}: {e}")

    # 2. Noticias de Google News RSS (Bloomberg, Investing, Reuters, etc.)
    search_term = f'"{clean_ticker}" stock OR "{company_name}"' if company_name else f'"{clean_ticker}" market'
    rss_articles = _fetch_rss_news(search_term, max_items=5)
    for art in rss_articles:
        if art["title"] not in seen_titles:
            seen_titles.add(art["title"])
            articles.append(art)

    # Si no hay artículos, retornar estado neutral
    if not articles:
        res = {
            "articles": [],
            "sentiment_score": 0.0,
            "sentiment_label": "NEUTRAL",
            "summary": "Sin noticias de impacto reciente detectadas.",
        }
        _news_cache[cache_key] = (now, res)
        return res

    # Calcular score de sentimiento ponderado (-100 a +100)
    avg_score = sum(a["sentiment_score"] for a in articles) / len(articles)
    sentiment_score = round(avg_score * 100, 1)

    if sentiment_score >= 20:
        sentiment_label = "ALCISTA"
        summary = f"Noticias predominantemente positivas ({sum(1 for a in articles if a['sentiment_tag'] == 'POSITIVO')} titulares favorables)."
    elif sentiment_score <= -20:
        sentiment_label = "BAJISTA"
        summary = f"Titulares con advertencias o presión de venta ({sum(1 for a in articles if a['sentiment_tag'] == 'NEGATIVO')} titulares desfavorables)."
    else:
        sentiment_label = "NEUTRAL"
        summary = "Flujo de noticias balanceado o sin catalizadores extraordinarios."

    res = {
        "articles": articles[:8],
        "sentiment_score": sentiment_score,
        "sentiment_label": sentiment_label,
        "summary": summary,
    }
    _news_cache[cache_key] = (now, res)
    return res
