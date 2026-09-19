"""
Módulo de recolección de datos multi-fuente:
- Precios y mercado (Yahoo Finance)
- Análisis técnico (TradingView + Fallback Algorítmico)
- Análisis fundamental (Ratios y consenso)
- Noticias financieras (Yahoo, Investing, Bloomberg vía feeds)
- Contexto geopolítico y macroeconómico global
"""
from .prices import get_position_market_data
from .technical import get_technical_analysis
from .fundamentals import get_fundamental_analysis
from .news import get_position_news
from .geopolitical import get_geopolitical_context

__all__ = [
    "get_position_market_data",
    "get_technical_analysis",
    "get_fundamental_analysis",
    "get_position_news",
    "get_geopolitical_context",
]
