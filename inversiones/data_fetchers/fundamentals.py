"""
Análisis fundamental y consenso de analistas.

Fuente primaria: yfinance (Yahoo Finance) — la más completa y fiable para
P/E, márgenes, growth, ROE, deuda, dividend yield, consenso analistas,
target prices y earnings calendar.

Complemento: MarketScreener (vía web_sources) para EPS forecasts por año
(2026/2027) y potencial upside.
"""
import time
from typing import Dict, Any, Optional
import logging

from data_fetchers.web_sources import fetch_marketscreener, normalize_ticker_input, to_yfinance_ticker

logger = logging.getLogger(__name__)

_fund_cache: Dict[str, Any] = {}
_FUND_CACHE_TTL = 900  # 15 minutos


def clear_fundamentals_cache() -> None:
    _fund_cache.clear()


def get_fundamental_analysis(ticker: str, current_price: Optional[float] = None) -> Dict[str, Any]:
    """Fundamentales completos desde yfinance (primario) + MarketScreener (forward)."""
    ticker = normalize_ticker_input(ticker)
    yf_symbol = to_yfinance_ticker(ticker)

    now = time.time()
    cached = _fund_cache.get(ticker)
    if cached and (now - cached[0]) < _FUND_CACHE_TTL:
        return dict(cached[1])

    is_crypto = "-USD" in ticker.upper() or ticker.upper().endswith("USDT")

    if is_crypto:
        try:
            import yfinance as yf
            tk = yf.Ticker(yf_symbol)
            info = tk.info or {}
            return {
                "type": "CRYPTOCURRENCY",
                "is_equity": False,
                "valuation_summary": "Activo digital / Cripto (Valoración por adopción y liquidez)",
                "trailing_pe": None,
                "forward_pe": None,
                "peg_ratio": None,
                "price_to_book": None,
                "profit_margins": None,
                "operating_margins": None,
                "roe": None,
                "debt_to_equity": None,
                "free_cash_flow": None,
                "revenue_growth": None,
                "earnings_growth": None,
                "dividend_yield": None,
                "target_mean_price": None,
                "target_high_price": None,
                "target_low_price": None,
                "analyst_upside_pct": None,
                "analyst_recommendation": "N/A (Crypto)",
                "num_analysts": 0,
                "sources": ["Yahoo Finance"],
            }
        except Exception:
            return {
                "type": "CRYPTOCURRENCY",
                "is_equity": False,
                "valuation_summary": "Activo digital / Cripto",
                "sources": ["Yahoo Finance"],
            }

    ms = fetch_marketscreener(ticker)
    sources = ["Yahoo Finance"]
    if ms.get("ok"):
        sources.append("MarketScreener")

    try:
        import yfinance as yf
        tk = yf.Ticker(yf_symbol)
        info = tk.info or {}
    except Exception as e:
        logger.debug(f"yfinance info falló para {ticker}: {e}")
        info = {}

    def _get(key, default=None):
        return info.get(key, default)

    trailing_pe = _get("trailingPE")
    forward_pe = _get("forwardPE")
    peg_ratio = _get("pegRatio")
    profit_margins = _get("profitMargins")
    operating_margins = _get("operatingMargins")
    roe = _get("returnOnEquity")
    debt_to_equity = _get("debtToEquity")
    free_cash_flow = _get("freeCashflow")
    revenue_growth = _get("revenueGrowth")
    earnings_growth = _get("earningsGrowth")
    dividend_yield = _get("dividendYield")
    price_to_book = _get("priceToBook")
    market_cap = _get("marketCap")

    target_mean = _get("targetMeanPrice")
    target_high = _get("targetHighPrice")
    target_low = _get("targetLowPrice")
    analyst_rec = str(_get("recommendationKey", "none") or "none").upper()
    num_analysts = _get("numberOfAnalystOpinions", 0) or 0

    # Earnings calendar
    next_earnings_date = None
    earnings_status = "Sin fecha confirmada"
    try:
        from datetime import date
        cal = tk.calendar
        if isinstance(cal, dict) and cal.get("Earnings Date"):
            ed_list = cal.get("Earnings Date")
            if ed_list and len(ed_list) > 0:
                first = ed_list[0]
                if hasattr(first, "strftime"):
                    next_earnings_date = first.strftime("%Y-%m-%d")
                    if first >= date.today():
                        earnings_status = f"Pendiente ({next_earnings_date})"
                    else:
                        earnings_status = f"Reportado ({next_earnings_date})"
    except Exception:
        pass

    # Upside del consenso
    analyst_upside_pct = None
    if target_mean and current_price and current_price > 0:
        analyst_upside_pct = round(((target_mean - current_price) / current_price) * 100, 2)
    elif ms.get("potential_pct") is not None:
        analyst_upside_pct = float(ms["potential_pct"])

    # Enriquecer con MarketScreener (forward EPS y crecimiento esperado)
    eps_2026 = ms.get("eps_2026")
    eps_2027 = ms.get("eps_2027")
    revenue_2026 = ms.get("revenue_2026")
    pe_2026 = ms.get("pe_2026")
    pe_2027 = ms.get("pe_2027")
    ms_consensus = ms.get("consensus")
    if not analyst_upside_pct and ms.get("potential_pct") is not None:
        analyst_upside_pct = float(ms["potential_pct"])

    valuation_summary = "Valoración dentro de promedios de mercado"
    if trailing_pe:
        if trailing_pe < 15:
            valuation_summary = "Múltiplos bajos (Posible subvaluación o sector de valor)"
        elif trailing_pe > 45:
            valuation_summary = "Múltiplos elevados (Exige alto crecimiento futuro)"
        else:
            valuation_summary = "Valoración equilibrada respecto a beneficios"

    return {
        "type": "EQUITY",
        "is_equity": True,
        "valuation_summary": valuation_summary,
        "trailing_pe": round(trailing_pe, 2) if trailing_pe else None,
        "forward_pe": round(forward_pe, 2) if forward_pe else None,
        "peg_ratio": round(peg_ratio, 2) if peg_ratio else None,
        "price_to_book": round(price_to_book, 2) if price_to_book else None,
        "profit_margins": round(profit_margins * 100, 2) if profit_margins else None,
        "operating_margins": round(operating_margins * 100, 2) if operating_margins else None,
        "roe": round(roe * 100, 2) if roe else None,
        "debt_to_equity": round(debt_to_equity, 2) if debt_to_equity else None,
        "free_cash_flow": free_cash_flow,
        "revenue_growth": round(revenue_growth * 100, 2) if revenue_growth else None,
        "earnings_growth": round(earnings_growth * 100, 2) if earnings_growth else None,
        "dividend_yield": round(dividend_yield * 100, 2) if dividend_yield else None,
        "market_cap": market_cap,
        "target_mean_price": round(target_mean, 2) if target_mean else None,
        "target_high_price": round(target_high, 2) if target_high else None,
        "target_low_price": round(target_low, 2) if target_low else None,
        "analyst_upside_pct": analyst_upside_pct,
        "analyst_recommendation": analyst_rec,
        "num_analysts": num_analysts,
        "next_earnings_date": next_earnings_date,
        "earnings_status": earnings_status,
        "eps_2026": round(eps_2026, 4) if eps_2026 else None,
        "eps_2027": round(eps_2027, 4) if eps_2027 else None,
        "revenue_2026": revenue_2026,
        "pe_2026": round(pe_2026, 2) if pe_2026 else None,
        "pe_2027": round(pe_2027, 2) if pe_2027 else None,
        "sources": sources,
    }
    _fund_cache[ticker] = (now, result)
    return result
