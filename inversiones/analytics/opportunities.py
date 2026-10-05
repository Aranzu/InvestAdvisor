"""
Scanner de Oportunidades de Compra (XTB-Only).

Pipeline de datos para cada candidato:
  1. yfinance → precio, P/E, growth, márgenes, ROE, deuda, consenso analistas
  2. TradingView → RSI + recomendación agregada (vía web_sources)
  3. MarketScreener → EPS forecast 2026/2027 + potencial upside (vía web_sources)
  4. Finviz screener → descubrimiento masivo de value/growth picks (vía web_sources)

Scoring (0-100):
  - Valuación: P/E < 18, PEG < 1, market cap razonable (30 pts)
  - Crecimiento: revenue growth > 15%, upside analistas > 20% (30 pts)
  - Técnico: RSI 35-55 (sweet spot), recomendación BUY de TradingView (20 pts)
  - Calidad: márgenes > 10%, ROE > 12%, deuda/equity < 100 (20 pts)
"""
from typing import List, Dict, Any, Optional
import json
import logging
import math
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from config.settings import BASE_DIR
from data_fetchers.web_sources import (
    fetch_tradingview,
    fetch_marketscreener,
    scan_finviz_screener,
    normalize_ticker_input,
    to_yfinance_ticker,
)
from data_fetchers.news import get_position_news
from data_fetchers.geopolitical import get_geopolitical_context, evaluate_sector_macro_exposure

logger = logging.getLogger(__name__)

CANDIDATES_FILE = BASE_DIR / "config" / "candidates.json"

import threading as _threading

_cached_opportunities: Optional[List[Dict[str, Any]]] = None
_cache_timestamp: float = 0.0
_CACHE_TTL_SECONDS = 60 * 60
_opportunities_lock = _threading.Lock()


def load_candidates_watchlist() -> List[Dict[str, Any]]:
    if not CANDIDATES_FILE.exists():
        return []
    try:
        with open(CANDIDATES_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Error cargando candidatos: {e}")
        return []


def save_candidates_watchlist(candidates: List[Dict[str, Any]]) -> bool:
    try:
        with open(CANDIDATES_FILE, "w", encoding="utf-8") as f:
            json.dump(candidates, f, indent=2, ensure_ascii=False)
        return True
    except Exception as e:
        logger.error(f"Error guardando candidatos: {e}")
        return False


def safe_float(val: Any) -> Optional[float]:
    if val is None or val == "" or val == "N/A" or val == "-" or val == "—":
        return None
    if isinstance(val, str):
        s = val.strip().rstrip("%").replace(",", "")
        if not s:
            return None
        try:
            f = float(s)
            return f if math.isfinite(f) else None
        except ValueError:
            return None
    try:
        f = float(val)
        return f if math.isfinite(f) else None
    except (ValueError, TypeError):
        return None


def _clean_json(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _clean_json(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_clean_json(v) for v in obj]
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    return obj


def add_discovery_context(candidate: Dict[str, Any], geo_context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Attach a non-scoring news/macro advisory to a Discovery candidate.

    This intentionally does not alter opportunity_score or ranking. It is a review
    signal for the investor after the quantitative shortlist has been built.
    """
    geo = geo_context or get_geopolitical_context()
    news = get_position_news(candidate.get("ticker", ""), candidate.get("name", ""))
    exposure = evaluate_sector_macro_exposure(candidate.get("sector", ""), geo.get("risk_level", "MODERADO"))
    sentiment = news.get("sentiment_label", "NEUTRAL")
    macro_risk = geo.get("risk_level", "MODERADO")
    needs_review = sentiment == "BAJISTA" or (macro_risk == "ALTO" and exposure.get("impact_score", 0) < 0)
    candidate["context_advisory"] = {
        "label": "REVISAR" if needs_review else "SIN_ALERTA",
        "news_sentiment": sentiment,
        "news_summary": news.get("summary", "Sin noticias de impacto reciente detectadas."),
        "macro_risk": macro_risk,
        "sector_impact": exposure.get("impact_description", "Impacto macro no diferenciado."),
        "affects_score": False,
        "disclaimer": "Señal informativa; no modifica el score ni el orden de Discovery.",
    }
    return candidate


def _get_return_1m_pct(tk) -> Optional[float]:
    """Retorno de precio a 1 mes (%) para el chequeo anti-sesgo de recencia.

    Si el nombre ya subió fuerte en las últimas semanas, puede seguir siendo una
    buena empresa pero ya no un punto de entrada de alta convicción.
    """
    try:
        hist = tk.history(period="1mo")
        if hist is None or hist.empty or "Close" not in hist.columns:
            return None
        close = hist["Close"].dropna()
        if len(close) < 2:
            return None
        first, last = float(close.iloc[0]), float(close.iloc[-1])
        if first <= 0:
            return None
        return round((last - first) / first * 100, 1)
    except Exception:
        return None


def _classify_conviction(score: int, rsi: Optional[float],
                         return_1m_pct: Optional[float],
                         upside_pct: Optional[float]) -> tuple[str, bool]:
    """Nivel de convicción (ALTA/MEDIA/ESPECULATIVA) + flag anti-sesgo de recencia.

    Sección 2 del módulo de compra del Prompt Maestro: no forzar "Alta" si el
    re-rating ya ocurrió (>20% en 1 mes) o el técnico está extendido (RSI > 75).
    """
    extended = (return_1m_pct is not None and return_1m_pct > 20) or (rsi is not None and rsi > 75)
    if score >= 80 and not extended and (upside_pct is None or upside_pct >= 10):
        return "ALTA", extended
    if score >= 60:
        return "MEDIA", extended
    return "ESPECULATIVA", extended


def _yfinance_snapshot(ticker: str) -> Dict[str, Any]:
    """Snapshot completo desde yfinance (PRIMARIO)."""
    try:
        import yfinance as yf
        tk = yf.Ticker(to_yfinance_ticker(ticker))
        info = tk.info or {}
        try:
            current_price = float(tk.fast_info.last_price or 0.0)
        except Exception:
            current_price = info.get("regularMarketPrice") or info.get("currentPrice") or 0.0
        return {
            "name": info.get("shortName") or info.get("longName") or "",
            "current_price": current_price,
            "return_1m_pct": _get_return_1m_pct(tk),
            "market_cap": info.get("marketCap"),
            "pe_ttm": info.get("trailingPE"),
            "pe_forward": info.get("forwardPE"),
            "peg": info.get("pegRatio"),
            "eps_ttm": info.get("trailingEps"),
            "eps_next_y": info.get("forwardEps"),
            "dividend_yield_raw": info.get("dividendYield") or info.get("trailingAnnualDividendYield"),
            "revenue_growth": info.get("revenueGrowth"),
            "earnings_growth": info.get("earningsGrowth"),
            "profit_margin": info.get("profitMargins"),
            "operating_margin": info.get("operatingMargins"),
            "roe": info.get("returnOnEquity"),
            "debt_to_equity": info.get("debtToEquity"),
            "beta": info.get("beta"),
            "52w_high": info.get("fiftyTwoWeekHigh"),
            "52w_low": info.get("fiftyTwoWeekLow"),
            "target_mean": info.get("targetMeanPrice"),
            "target_high": info.get("targetHighPrice"),
            "target_low": info.get("targetLowPrice"),
            "analyst_consensus": str(info.get("recommendationKey", "none") or "none").upper(),
            "num_analysts": info.get("numberOfAnalystOpinions", 0) or 0,
            "sector": info.get("sector"),
            "industry": info.get("industry"),
        }
    except Exception as e:
        logger.debug(f"yfinance snapshot falló para {ticker}: {e}")
        return {}


def _score_opportunity(yf_data: Dict[str, Any], tv: Dict[str, Any], ms: Dict[str, Any]) -> Dict[str, Any]:
    """Calcula score combinado (0-100)."""
    # Si no hay datos fundamentales mínimos, penalizar desde el inicio
    has_fundamental = any(yf_data.get(k) for k in ("pe_ttm", "revenue_growth", "profit_margin", "roe"))
    score = 45.0 if not has_fundamental else 50.0
    if not has_fundamental:
        breakdown = {"data_quality": "Datos fundamentales insuficientes (-5 base)"}
    else:
        breakdown = {}

    pe = safe_float(yf_data.get("pe_ttm"))
    peg = safe_float(yf_data.get("peg"))
    if pe is not None and pe > 0:
        if pe < 12:
            score += 20
            breakdown["valuation"] = f"P/E {round(pe, 1)} (muy barato)"
        elif pe < 18:
            score += 12
            breakdown["valuation"] = f"P/E {round(pe, 1)} (razonable)"
        elif pe < 25:
            score += 5
            breakdown["valuation"] = f"P/E {round(pe, 1)} (justo)"
        elif pe > 50:
            score -= 20
            breakdown["valuation"] = f"P/E {round(pe, 1)} (caro)"
    if peg is not None and peg > 0:
        if peg < 1:
            score += 10
            breakdown["peg"] = f"PEG {round(peg, 2)} (subvaluado vs crecimiento)"
        elif peg > 2:
            score -= 5

    rev_growth = safe_float(yf_data.get("revenue_growth"))
    earnings_growth = safe_float(yf_data.get("earnings_growth"))
    if rev_growth is not None:
        if rev_growth >= 0.25:
            score += 20
            breakdown["revenue_growth"] = f"Revenue growth +{round(rev_growth * 100, 1)}%"
        elif rev_growth >= 0.15:
            score += 12
            breakdown["revenue_growth"] = f"Revenue growth +{round(rev_growth * 100, 1)}%"
        elif rev_growth < 0:
            score -= 15
            breakdown["revenue_growth"] = f"Revenue growth {round(rev_growth * 100, 1)}% (negativo)"
    if earnings_growth is not None and earnings_growth > 0.15:
        score += 10
        breakdown["earnings_growth"] = f"Earnings growth +{round(earnings_growth * 100, 1)}%"

    target_mean = safe_float(yf_data.get("target_mean"))
    current_price = safe_float(yf_data.get("current_price"))
    upside_pct = None
    if target_mean and current_price and current_price > 0:
        upside_pct = round(((target_mean - current_price) / current_price) * 100, 1)
    elif safe_float(ms.get("potential_pct")) is not None:
        upside_pct = safe_float(ms.get("potential_pct"))

    if upside_pct is not None:
        if upside_pct >= 30:
            score += 20
            breakdown["upside"] = f"Upside +{upside_pct}% (consenso)"
        elif upside_pct >= 15:
            score += 10
            breakdown["upside"] = f"Upside +{upside_pct}%"
        elif upside_pct < -5:
            score -= 10

    rsi = safe_float(tv.get("rsi_14"))
    rec = (tv.get("recommendation") or "NEUTRAL").upper()
    if rsi is not None:
        if 35 <= rsi <= 55:
            score += 15
            breakdown["rsi"] = f"RSI {rsi} (sweet spot de acumulación)"
        elif rsi < 30:
            score += 10
            breakdown["rsi"] = f"RSI {rsi} (sobreventa)"
        elif rsi > 75:
            score -= 15
            breakdown["rsi"] = f"RSI {rsi} (sobrecompra)"
    if "BUY" in rec:
        score += 10
        breakdown["tradingview"] = f"TradingView: {rec}"
    elif "SELL" in rec:
        score -= 10
        breakdown["tradingview"] = f"TradingView: {rec}"

    profit_margin = safe_float(yf_data.get("profit_margin"))
    roe = safe_float(yf_data.get("roe"))
    debt_equity = safe_float(yf_data.get("debt_to_equity"))
    if profit_margin is not None:
        if profit_margin >= 0.20:
            score += 10
            breakdown["margin"] = f"Margen neto {round(profit_margin * 100, 1)}%"
        elif profit_margin >= 0.10:
            score += 5
        elif profit_margin < 0:
            score -= 15
    if roe is not None:
        if roe >= 0.20:
            score += 7
            breakdown["roe"] = f"ROE {round(roe * 100, 1)}%"
        elif roe >= 0.12:
            score += 3
        elif roe < 0:
            score -= 10
    if debt_equity is not None:
        if debt_equity < 60:
            score += 3
            breakdown["balance"] = f"Deuda/Equity {round(debt_equity, 1)}"
        elif debt_equity > 200:
            score -= 10

    opportunity_score = int(round(max(10, min(99, score))))
    return {
        "opportunity_score": opportunity_score,
        "upside_pct": upside_pct,
        "breakdown": breakdown,
    }


def _derive_entry_tp_sl(
    yf_data: Dict[str, Any],
    tv: Dict[str, Any],
    target_mean: Optional[float],
) -> Dict[str, Any]:
    current_price = safe_float(yf_data.get("current_price")) or 0.0
    if current_price <= 0:
        return {
            "current_price": None,
            "entry": None,
            "stop_loss": None,
            "stop_loss_pct": None,
            "take_profit": None,
            "take_profit_pct": None,
            "entry_type": "A Mercado",
        }

    rsi = safe_float(tv.get("rsi_14"))
    entry = current_price
    entry_type = "A Mercado"
    if rsi and rsi > 60:
        entry = round(current_price * 0.96, 2)
        entry_type = "Límite en Pullback (-4%)"

    stop_loss = round(entry * 0.90, 2)
    stop_loss_pct = -10.0

    if target_mean and target_mean > entry:
        take_profit = round(target_mean, 2)
        take_profit_pct = round(((take_profit - entry) / entry) * 100, 1)
    else:
        take_profit = round(entry * 1.25, 2)
        take_profit_pct = 25.0

    return {
        "current_price": round(current_price, 2),
        "entry": round(entry, 2),
        "stop_loss": stop_loss,
        "stop_loss_pct": stop_loss_pct,
        "take_profit": take_profit,
        "take_profit_pct": take_profit_pct,
        "entry_type": entry_type,
    }


def analyze_candidate(candidate: Dict[str, Any]) -> Dict[str, Any]:
    """Pipeline completo: yfinance + TradingView + MarketScreener."""
    raw_ticker = candidate.get("ticker", "").strip()
    ticker = normalize_ticker_input(raw_ticker)
    name_hint = candidate.get("name", "").strip()
    sector_hint = candidate.get("sector", "Crecimiento")

    yf_data = _yfinance_snapshot(ticker)
    tv = fetch_tradingview(ticker)
    ms = fetch_marketscreener(ticker)

    # Quality gate: descartar solo si no hay precio (sin liquidez = no invertible)
    current_price = safe_float(yf_data.get("current_price")) or 0.0
    if current_price <= 0:
        return _clean_json({
            "ticker": ticker,
            "name": name_hint or ticker,
            "sector": sector_hint,
            "current_price": None,
            "opportunity_score": 0,
            "entry_rationale": "Sin precio en yfinance — descartado",
            "skip": True,
        })

    # Obtener nombre real de yfinance (mejor que el hint de Finviz) sin llamada extra
    actual_name = yf_data.get("name") or name_hint or ticker

    sector = yf_data.get("sector") or sector_hint
    target_mean = safe_float(yf_data.get("target_mean"))
    if not target_mean:
        target_mean = safe_float(ms.get("target_price"))

    pricing = _derive_entry_tp_sl(yf_data, tv, target_mean)
    scoring = _score_opportunity(yf_data, tv, ms)

    rsi = safe_float(tv.get("rsi_14"))
    rev_growth = safe_float(yf_data.get("revenue_growth"))
    pe = safe_float(yf_data.get("pe_ttm"))

    return_1m_pct = safe_float(yf_data.get("return_1m_pct"))
    conviction, anti_recency_flag = _classify_conviction(
        scoring["opportunity_score"], rsi, return_1m_pct, scoring["upside_pct"]
    )

    pe_str = f"P/E {round(pe, 1)}x" if pe else "valuación N/D"
    growth_str = f"+{round(rev_growth * 100, 1)}% ingresos" if rev_growth else "crecimiento estable"
    upside_str = f"upside {scoring['upside_pct']}%" if scoring["upside_pct"] is not None else "momentum técnico"
    tv_signal = (tv.get("recommendation") or "NEUTRAL").upper()
    motivo = (
        f"Fund.: {sector} ({pe_str}, {growth_str}). "
        f"Téc.: TradingView {tv_signal} (RSI {rsi}). "
        f"Proyección: {upside_str}."
    )

    sources = ["Yahoo Finance"]
    if tv.get("ok"):
        sources.append("TradingView")
    if ms.get("ok"):
        sources.append("MarketScreener")

    return _clean_json({
        "ticker": ticker,
        "name": actual_name or name_hint or ticker,
        "sector": sector,
        "current_price": pricing["current_price"],
        "daily_change_pct": safe_float(tv.get("change_pct")) or 0.0,
        "rsi": rsi,
        "tradingview_rec": tv_signal,
        "trailing_pe": round(pe, 1) if pe else None,
        "forward_pe": safe_float(yf_data.get("pe_forward")),
        "revenue_growth_pct": round(rev_growth * 100, 1) if rev_growth else None,
        "earnings_growth_pct": safe_float(yf_data.get("earnings_growth")),
        "profit_margin_pct": round(safe_float(yf_data.get("profit_margin")) * 100, 1) if yf_data.get("profit_margin") else None,
        "roe_pct": round(safe_float(yf_data.get("roe")) * 100, 1) if yf_data.get("roe") else None,
        "debt_to_equity": safe_float(yf_data.get("debt_to_equity")),
        "dividend_yield_pct": round(safe_float(yf_data.get("dividend_yield_raw")) * 100, 2) if yf_data.get("dividend_yield_raw") else None,
        "analyst_consensus": yf_data.get("analyst_consensus"),
        "analyst_target": round(target_mean, 2) if target_mean else None,
        "upside_pct": scoring["upside_pct"],
        "opportunity_score": scoring["opportunity_score"],
        "conviction": conviction,
        "anti_recency_flag": anti_recency_flag,
        "return_1m_pct": return_1m_pct,
        "entry_type": pricing["entry_type"],
        "suggested_entry": pricing["entry"],
        "suggested_stop_loss": pricing["stop_loss"],
        "stop_loss_pct": pricing["stop_loss_pct"],
        "suggested_take_profit": pricing["take_profit"],
        "take_profit_pct": pricing["take_profit_pct"],
        "confidence_10": int(round(max(1.0, min(10.0, scoring["opportunity_score"] / 10.0)))),
        "entry_rationale": motivo,
        "score_breakdown": scoring["breakdown"],
        "sources": sources,
        "as_of": time.strftime("%Y-%m-%d %H:%M:%S"),
    })


def _get_yf_name(ticker: str) -> Optional[str]:
    """Helper: obtiene el nombre de la empresa desde yfinance."""
    try:
        import yfinance as yf
        info = yf.Ticker(to_yfinance_ticker(ticker)).info or {}
        return info.get("shortName") or info.get("longName")
    except Exception:
        return None


def scan_opportunities(force_refresh: bool = False, max_workers: int = 6) -> List[Dict[str, Any]]:
    """Escanea watchlist y ordena por Opportunity Score descendente."""
    global _cached_opportunities, _cache_timestamp
    now = time.time()
    with _opportunities_lock:
        if not force_refresh and _cached_opportunities is not None and (now - _cache_timestamp) < _CACHE_TTL_SECONDS:
            return _cached_opportunities

    candidates = load_candidates_watchlist()
    if not candidates:
        return []

    results = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_cand = {executor.submit(analyze_candidate, c): c for c in candidates}
        for future in as_completed(future_to_cand):
            try:
                res = future.result()
                if res.get("current_price") and res["current_price"] > 0:
                    results.append(res)
            except Exception as e:
                logger.error(f"Error analizando candidato {future_to_cand[future].get('ticker')}: {e}")

    results.sort(key=lambda x: x["opportunity_score"], reverse=True)

    with _opportunities_lock:
        if results:
            _cached_opportunities = results
            _cache_timestamp = now
    return results


def find_value_picks(min_score: int = 70, top_n: int = 15) -> List[Dict[str, Any]]:
    """Top picks de valor/crecimiento (score >= min_score)."""
    all_results = scan_opportunities(force_refresh=False)
    return [r for r in all_results if r["opportunity_score"] >= min_score][:top_n]


def discover_value_growth_via_finviz(
    filters: Optional[Dict[str, str]] = None,
    top_n: int = 20,
) -> List[Dict[str, str]]:
    """
    Descubrimiento masivo vía Finviz screener.
    Devuelve filas crudas del screener (ticker + ratios clave) para
    alimentar la watchlist del usuario.
    """
    rows = scan_finviz_screener(filters, force=False)
    return rows[:top_n]
