"""
Cálculo de puntajes normalizados (-100 a +100) para cada dimensión de análisis:
- Técnico
- Fundamental
- Noticias & Sentimiento
- Geopolítico & Macroeconómico
"""
from typing import Dict, Any
import logging
from config.settings import WEIGHTS
from data_fetchers.geopolitical import evaluate_sector_macro_exposure

logger = logging.getLogger(__name__)


def _coverage_ratio(values) -> float:
    """Return the fraction of indicators that were actually supplied."""
    return round(sum(value is not None for value in values) / len(values), 2) if values else 0.0


def calculate_data_coverage(
    tech_data: Dict[str, Any], fund_data: Dict[str, Any], news_data: Dict[str, Any], geo_data: Dict[str, Any]
) -> Dict[str, float]:
    """Measure usable evidence so absent provider data cannot look conclusive."""
    signal_total = sum(int(tech_data.get(key) or 0) for key in ("buy_signals", "sell_signals", "neutral_signals"))
    technical = _coverage_ratio([
        tech_data.get("rsi"), tech_data.get("sma_200"), tech_data.get("macd"),
        tech_data.get("recommendation") if tech_data.get("recommendation") not in (None, "", "N/D") else None,
        signal_total if signal_total > 0 else None,
    ])
    if tech_data.get("data_quality") == "PARTIAL":
        technical = min(technical, 0.6)
    elif tech_data.get("data_quality") == "UNAVAILABLE":
        technical = 0.0

    fundamental = _coverage_ratio([
        fund_data.get("trailing_pe"), fund_data.get("profit_margins"), fund_data.get("roe"),
        fund_data.get("debt_to_equity"), fund_data.get("revenue_growth"), fund_data.get("analyst_upside_pct"),
    ]) if fund_data.get("is_equity", True) else 1.0
    news = 1.0 if news_data.get("articles") else 0.5 if news_data.get("summary") else 0.0
    geopolitical = _coverage_ratio([geo_data.get("macro_score"), geo_data.get("risk_level")])
    return {"technical": technical, "fundamental": fundamental, "news": news, "geopolitical": geopolitical}


def calculate_technical_score(tech_data: Dict[str, Any], market_data: Dict[str, Any]) -> float:
    """Calcula un score de -100 a +100 basado en indicadores técnicos."""
    score = 0.0
    rec = tech_data.get("recommendation", "NEUTRAL").upper()
    
    # 1. Recomendación general de TradingView / Algoritmo
    rec_weights = {
        "STRONG_BUY": 50.0,
        "BUY": 25.0,
        "NEUTRAL": 0.0,
        "SELL": -25.0,
        "STRONG_SELL": -50.0,
    }
    score += rec_weights.get(rec, 0.0)

    # 2. Conteo de señales compra vs venta
    buys = tech_data.get("buy_signals", 0)
    sells = tech_data.get("sell_signals", 0)
    total = buys + sells + tech_data.get("neutral_signals", 0)
    if total > 0:
        signal_bias = ((buys - sells) / total) * 30.0
        score += signal_bias

    # 3. RSI
    rsi = tech_data.get("rsi")
    if rsi is not None:
        if rsi < 30:
            score += 20.0  # Sobrevendido (potencial rebote alcista)
        elif rsi > 70:
            score -= 20.0  # Sobrecomprado (riesgo de corrección)
        elif 40 <= rsi <= 60:
            score += 5.0   # En zona saludable

    # 4. Tendencia respecto a SMA200 (Golden/Death cross o posición respecto a la media de largo plazo)
    current_price = market_data.get("current_price", 0.0)
    sma_200 = tech_data.get("sma_200")
    if sma_200 and current_price > 0:
        if current_price > sma_200:
            score += 15.0  # Por encima de la media de 200 días (tendencia alcista principal)
        else:
            score -= 15.0  # Por debajo de la media de 200 días (tendencia bajista principal)

    # Clamping entre -100 y 100
    return round(max(-100.0, min(100.0, score)), 1)


def calculate_fundamental_score(fund_data: Dict[str, Any], market_data: Dict[str, Any]) -> float:
    """Calcula un score de -100 a +100 basado en ratios de valuación y balances."""
    score = 0.0
    
    # Si no es acción tradicional (ETF o Cripto), asignar score neutral o de tendencia
    if not fund_data.get("is_equity", True):
        # Para ETFs, se premia la diversificación
        if fund_data.get("type") == "ETF":
            return 20.0
        # Para Cripto, momentum / adopción
        return 0.0

    # 1. P/E Trailing
    pe = fund_data.get("trailing_pe")
    if pe is not None and pe > 0:
        if pe < 15:
            score += 25.0  # Barato / Valor
        elif pe < 28:
            score += 10.0  # Razonable
        elif pe > 50:
            score -= 20.0  # Excesivamente exigente

    # 2. Márgenes de ganancia
    profit_margins = fund_data.get("profit_margins")
    if profit_margins is not None:
        if profit_margins > 20:
            score += 20.0
        elif profit_margins > 10:
            score += 10.0
        elif profit_margins < 0:
            score -= 25.0  # Pérdidas netas

    # 3. ROE (Rentabilidad sobre patrimonio)
    roe = fund_data.get("roe")
    if roe is not None:
        if roe > 25:
            score += 15.0
        elif roe > 12:
            score += 8.0
        elif roe < 0:
            score -= 15.0

    # 4. Deuda / Patrimonio (Debt to Equity)
    debt_equity = fund_data.get("debt_to_equity")
    if debt_equity is not None:
        if debt_equity < 80:
            score += 10.0  # Balance sólido y bajo apalancamiento
        elif debt_equity > 200:
            score -= 15.0  # Alto riesgo de endeudamiento

    # 5. Crecimiento de ingresos y beneficios
    rev_growth = fund_data.get("revenue_growth")
    if rev_growth is not None:
        if rev_growth > 15:
            score += 15.0
        elif rev_growth < -5:
            score -= 20.0

    # 6. Consenso de Analistas (Wall Street target price)
    analyst_rec = fund_data.get("analyst_recommendation", "").lower()
    if any(k in analyst_rec for k in ["strong_buy", "buy"]):
        score += 15.0
    elif any(k in analyst_rec for k in ["underperform", "sell"]):
        score -= 20.0

    upside = fund_data.get("analyst_upside_pct")
    if upside is not None:
        if upside > 15:
            score += 10.0
        elif upside < -10:
            score -= 10.0

    return round(max(-100.0, min(100.0, score)), 1)


def calculate_news_score(news_data: Dict[str, Any]) -> float:
    """Calcula el score de noticias y sentimiento (-100 a +100)."""
    return round(float(news_data.get("sentiment_score", 0.0)), 1)


def calculate_geopolitical_score(geo_data: Dict[str, Any], market_data: Dict[str, Any]) -> float:
    """Calcula el score de impacto geopolítico y macro para el activo (-100 a +100)."""
    base_macro = float(geo_data.get("macro_score", 0.0))
    risk_level = geo_data.get("risk_level", "MODERADO")
    
    sector = market_data.get("sector", "General")
    sector_impact = evaluate_sector_macro_exposure(sector, risk_level)
    
    combined = (base_macro * 0.5) + (sector_impact["impact_score"] * 0.5)
    return round(max(-100.0, min(100.0, combined)), 1)


def calculate_composite_scores(
    market_data: Dict[str, Any],
    tech_data: Dict[str, Any],
    fund_data: Dict[str, Any],
    news_data: Dict[str, Any],
    geo_data: Dict[str, Any]
) -> Dict[str, Any]:
    """
    Consolida las 4 dimensiones de análisis con sus pesos configurados
    y calcula el puntaje global final (-100 a +100).
    """
    tech_score = calculate_technical_score(tech_data, market_data)
    fund_score = calculate_fundamental_score(fund_data, market_data)
    news_score = calculate_news_score(news_data)
    geo_score = calculate_geopolitical_score(geo_data, market_data)

    coverage = calculate_data_coverage(tech_data, fund_data, news_data, geo_data)
    # Preserve a neutral baseline but reduce the influence of dimensions with
    # sparse evidence. A partial TradingView aggregate must not dominate a call.
    tech_score = round(tech_score * (0.35 + 0.65 * coverage["technical"]), 1)
    fund_score = round(fund_score * (0.35 + 0.65 * coverage["fundamental"]), 1)
    news_score = round(news_score * (0.35 + 0.65 * coverage["news"]), 1)
    geo_score = round(geo_score * (0.35 + 0.65 * coverage["geopolitical"]), 1)

    w_tech = WEIGHTS.get("technical", 0.30)
    w_fund = WEIGHTS.get("fundamental", 0.35)
    w_news = WEIGHTS.get("news", 0.20)
    w_geo = WEIGHTS.get("geopolitical", 0.15)

    # Si es cripto o ETF donde los fundamentales de balance no aplican igual,
    # redistribuir el peso hacia lo técnico y las noticias.
    if not fund_data.get("is_equity", True):
        w_tech = 0.45
        w_news = 0.30
        w_geo = 0.25
        w_fund = 0.0

    total_weight = w_tech + w_fund + w_news + w_geo
    global_score = (
        (tech_score * w_tech) +
        (fund_score * w_fund) +
        (news_score * w_news) +
        (geo_score * w_geo)
    ) / total_weight

    return {
        "technical_score": tech_score,
        "fundamental_score": fund_score,
        "news_score": news_score,
        "geopolitical_score": geo_score,
        "global_score": round(global_score, 1),
        "weights_used": {
            "technical": w_tech,
            "fundamental": w_fund,
            "news": w_news,
            "geopolitical": w_geo,
        },
        "data_coverage": coverage,
        "data_coverage_pct": round(sum(coverage[key] * weight for key, weight in (
            ("technical", w_tech), ("fundamental", w_fund), ("news", w_news), ("geopolitical", w_geo),
        )) / total_weight * 100, 1),
    }
