"""
Obtención de precios actuales y datos de mercado.

Fuente primaria: yfinance (Yahoo Finance) — el agregador #1 gratis, sin rate-limits.
Fallback liviano: Finviz (vía web_sources) cuando yfinance falla.
"""
import time
from typing import Dict, Any, Optional
import logging

from data_fetchers.web_sources import fetch_finviz, normalize_ticker_input, to_yfinance_ticker

logger = logging.getLogger(__name__)

_market_info_cache: Dict[str, Any] = {}
_MARKET_CACHE_TTL = 300  # 5 minutos


def clear_prices_cache() -> None:
    _market_info_cache.clear()


def get_position_market_data(
    ticker: str,
    shares: float = 0.0,
    entry_price: float = 0.0,
    notes: str = "",
    reported_price: Optional[float] = None,
) -> Dict[str, Any]:
    """Precio actual y métricas de mercado desde yfinance (primario)."""
    ticker = normalize_ticker_input(ticker)
    yf_symbol = to_yfinance_ticker(ticker)

    now = time.time()
    cached_entry = _market_info_cache.get(yf_symbol)
    if cached_entry and (now - cached_entry[0]) < _MARKET_CACHE_TTL:
        raw = cached_entry[1]
    else:
        current_price = 0.0
        previous_close = 0.0
        currency = "USD"
        short_name = ticker
        sector = "Desconocido"
        industry = "Desconocido"
        market_cap = None
        fifty_two_week_high = None
        fifty_two_week_low = None
        beta = None
        volume = None

        try:
            import yfinance as yf
            yf_obj = yf.Ticker(yf_symbol)
            try:
                fi = yf_obj.fast_info
                current_price = float(getattr(fi, "last_price", 0.0) or 0.0)
                previous_close = float(getattr(fi, "previous_close", 0.0) or current_price)
                currency = getattr(fi, "currency", "USD") or "USD"
                market_cap = getattr(fi, "market_cap", None)
                fifty_two_week_high = getattr(fi, "year_high", None)
                fifty_two_week_low = getattr(fi, "year_low", None)
            except Exception as e:
                logger.debug(f"yfinance fast_info falló para {ticker}: {e}")

            try:
                info = yf_obj.info or {}
                short_name = info.get("shortName") or info.get("longName") or ticker
                sector = info.get("sector") or sector
                industry = info.get("industry") or industry
                beta = beta or info.get("beta")
                volume = volume or info.get("regularMarketVolume") or info.get("volume")
                if current_price <= 0:
                    current_price = (
                        info.get("regularMarketPrice")
                        or info.get("currentPrice")
                        or info.get("ask")
                        or info.get("bid")
                        or 0.0
                    )
                if previous_close <= 0:
                    previous_close = info.get("regularMarketPreviousClose") or info.get("previousClose") or current_price
                if not fifty_two_week_high:
                    fifty_two_week_high = info.get("fiftyTwoWeekHigh")
                if not fifty_two_week_low:
                    fifty_two_week_low = info.get("fiftyTwoWeekLow")
                if not market_cap:
                    market_cap = info.get("marketCap")
            except Exception as e:
                logger.debug(f"yfinance info falló para {ticker}: {e}")
        except ImportError:
            pass

        # Fallback: Finviz (vía web_sources) cuando yfinance no entrega precio.
        if current_price <= 0:
            fv = fetch_finviz(ticker)
            if fv.get("ok") and fv.get("current_price"):
                current_price = float(fv["current_price"])
            if not market_cap and fv.get("market_cap"):
                market_cap = fv["market_cap"]
            if not fifty_two_week_high and fv.get("52w_high"):
                fifty_two_week_high = fv["52w_high"]
            if not fifty_two_week_low and fv.get("52w_low"):
                fifty_two_week_low = fv["52w_low"]
            if not sector and fv.get("sector"):
                sector = fv["sector"]
            if not industry and fv.get("industry"):
                industry = fv["industry"]

        # Fallback final con histórico.
        if current_price <= 0:
            try:
                import yfinance as yf
                hist = yf.Ticker(yf_symbol).history(period="5d")
                if not hist.empty:
                    current_price = float(hist["Close"].iloc[-1])
                    if len(hist) > 1:
                        previous_close = float(hist["Close"].iloc[-2])
            except Exception as e:
                logger.error(f"Fallback histórico falló para {ticker}: {e}")
                current_price = entry_price if entry_price > 0 else 0.0

        raw = {
            "current_price": current_price,
            "previous_close": previous_close,
            "currency": currency,
            "short_name": short_name,
            "sector": sector,
            "industry": industry,
            "market_cap": market_cap,
            "fifty_two_week_high": fifty_two_week_high,
            "fifty_two_week_low": fifty_two_week_low,
            "beta": beta,
            "volume": volume,
        }
        _market_info_cache[yf_symbol] = (now, raw)

    current_price = raw["current_price"]
    previous_close = raw["previous_close"]
    currency = raw["currency"]
    short_name = raw["short_name"]
    sector = raw["sector"]
    industry = raw["industry"]
    market_cap = raw["market_cap"]
    fifty_two_week_high = raw["fifty_two_week_high"]
    fifty_two_week_low = raw["fifty_two_week_low"]
    beta = raw["beta"]
    volume = raw["volume"]

    live_price = round(float(current_price or 0.0), 4)

    try:
        reported_price = round(float(reported_price), 4) if reported_price else 0.0
    except (TypeError, ValueError):
        reported_price = 0.0

    price_source = "Yahoo Finance"
    if reported_price and reported_price > 0:
        current_price = reported_price
        price_source = "Broker"
    elif live_price > 0:
        current_price = live_price

    current_price = round(float(current_price or 0.0), 4)
    previous_close = round(float(previous_close or current_price), 4)

    daily_change = round(current_price - previous_close, 4)
    daily_change_pct = round((daily_change / previous_close * 100) if previous_close > 0 else 0.0, 2)

    total_invested = round(shares * entry_price, 2) if (shares > 0 and entry_price > 0) else 0.0
    current_value = round(shares * current_price, 2) if shares > 0 else 0.0
    unrealized_pnl = round(current_value - total_invested, 2) if total_invested > 0 else 0.0
    unrealized_pnl_pct = round(((current_price - entry_price) / entry_price * 100), 2) if entry_price > 0 else 0.0

    dist_from_52w_high_pct = None
    dist_from_52w_low_pct = None
    if fifty_two_week_high and fifty_two_week_high > 0:
        dist_from_52w_high_pct = round(((current_price - fifty_two_week_high) / fifty_two_week_high) * 100, 2)
    if fifty_two_week_low and fifty_two_week_low > 0:
        dist_from_52w_low_pct = round(((current_price - fifty_two_week_low) / fifty_two_week_low) * 100, 2)

    return {
        "ticker": ticker,
        "name": short_name,
        "sector": sector,
        "industry": industry,
        "currency": currency,
        "current_price": current_price,
        "live_price": live_price,
        "price_source": price_source,
        "previous_close": previous_close,
        "daily_change": daily_change,
        "daily_change_pct": daily_change_pct,
        "shares": shares,
        "entry_price": entry_price,
        "total_invested": total_invested,
        "current_value": current_value,
        "unrealized_pnl": unrealized_pnl,
        "unrealized_pnl_pct": unrealized_pnl_pct,
        "fifty_two_week_high": fifty_two_week_high,
        "fifty_two_week_low": fifty_two_week_low,
        "dist_from_52w_high_pct": dist_from_52w_high_pct,
        "dist_from_52w_low_pct": dist_from_52w_low_pct,
        "market_cap": market_cap,
        "volume": volume,
        "beta": beta,
        "notes": notes,
    }
