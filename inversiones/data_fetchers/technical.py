"""
Análisis técnico vía TradingView (primario) y cálculo algorítmico nativo (fallback).

TradingView aporta la recomendación agregada y conteo de osciladores
(Buy/Neutral/Sell) que no se obtiene directamente de yfinance.
El fallback algorítmico calcula RSI/MACD/MAs/Bollinger desde histórico
yfinance cuando TradingView no responde.
"""
from typing import Dict, Any, Optional
import logging
import threading
import time

import pandas as pd
import numpy as np

from data_fetchers.web_sources import fetch_tradingview, fetch_finviz, normalize_ticker_input, to_yfinance_ticker

logger = logging.getLogger(__name__)

try:
    from tradingview_ta import TA_Handler, Interval
    HAS_TRADINGVIEW_TA = True
except ImportError:
    HAS_TRADINGVIEW_TA = False

_tech_cache: Dict[str, Dict[str, Any]] = {}
_tech_cache_ts: Dict[str, float] = {}
_TECH_CACHE_TTL = 30 * 60
_tv_lock = threading.Lock()


def clear_technical_cache() -> None:
    """Clear cached indicators after a user requests a portfolio refresh."""
    _tech_cache.clear()
    _tech_cache_ts.clear()


def _cache_get(ticker: str) -> Optional[Dict[str, Any]]:
    ts = _tech_cache_ts.get(ticker)
    if ts is not None and (time.time() - ts) < _TECH_CACHE_TTL:
        return _tech_cache.get(ticker)
    return None


def _cache_set(ticker: str, data: Dict[str, Any]) -> None:
    _tech_cache[ticker] = data
    _tech_cache_ts[ticker] = time.time()


def _calculate_indicators_pandas(df: pd.DataFrame) -> Dict[str, Any]:
    """Calcula indicadores con pandas desde histórico de yfinance."""
    close = df["Close"]

    sma_20 = float(close.rolling(window=20).mean().iloc[-1]) if len(close) >= 20 else None
    sma_50 = float(close.rolling(window=50).mean().iloc[-1]) if len(close) >= 50 else None
    sma_200 = float(close.rolling(window=200).mean().iloc[-1]) if len(close) >= 200 else None

    ema_20 = float(close.ewm(span=20, adjust=False).mean().iloc[-1]) if len(close) >= 20 else None
    ema_50 = float(close.ewm(span=50, adjust=False).mean().iloc[-1]) if len(close) >= 50 else None

    rsi_14 = 50.0
    if len(close) >= 15:
        delta = close.diff()
        gain = delta.clip(lower=0)
        loss = -delta.clip(upper=0)
        avg_gain = gain.ewm(alpha=1/14, min_periods=14, adjust=False).mean().iloc[-1]
        avg_loss = loss.ewm(alpha=1/14, min_periods=14, adjust=False).mean().iloc[-1]
        if avg_loss == 0 and avg_gain == 0:
            rsi_14 = 50.0
        elif avg_loss == 0:
            rsi_14 = 100.0
        elif avg_gain == 0:
            rsi_14 = 0.0
        else:
            rs = avg_gain / avg_loss
            rsi_14 = round(float(100 - (100 / (1 + rs))), 2)

    macd_val, macd_signal, macd_hist = None, None, None
    if len(close) >= 35:
        ema12 = close.ewm(span=12, adjust=False).mean()
        ema26 = close.ewm(span=26, adjust=False).mean()
        macd_line = ema12 - ema26
        signal_line = macd_line.ewm(span=9, adjust=False).mean()
        hist = macd_line - signal_line
        macd_val = round(float(macd_line.iloc[-1]), 4)
        macd_signal = round(float(signal_line.iloc[-1]), 4)
        macd_hist = round(float(hist.iloc[-1]), 4)

    atr_14 = None
    if len(close) >= 15 and {"High", "Low"}.issubset(df.columns):
        previous_close = close.shift(1)
        true_range = pd.concat([
            df["High"] - df["Low"],
            (df["High"] - previous_close).abs(),
            (df["Low"] - previous_close).abs(),
        ], axis=1).max(axis=1)
        atr_value = true_range.rolling(window=14).mean().iloc[-1]
        if pd.notna(atr_value):
            atr_14 = round(float(atr_value), 2)

    bb_upper, bb_lower = None, None
    if len(close) >= 20:
        std20 = close.rolling(window=20).std().iloc[-1]
        bb_upper = round(float(sma_20 + (2 * std20)), 2)
        bb_lower = round(float(sma_20 - (2 * std20)), 2)

    current_price = float(close.iloc[-1])

    buy_count = sell_count = neutral_count = 0
    if rsi_14 is not None:
        if rsi_14 < 30:
            buy_count += 1
        elif rsi_14 > 70:
            sell_count += 1
        else:
            neutral_count += 1
    if macd_val is not None and macd_signal is not None:
        if macd_val > macd_signal:
            buy_count += 1
        else:
            sell_count += 1
    for ma in [sma_20, sma_50, sma_200, ema_20, ema_50]:
        if ma is not None:
            if current_price > ma:
                buy_count += 1
            elif current_price < ma:
                sell_count += 1
            else:
                neutral_count += 1

    if buy_count >= sell_count + 3:
        recommendation = "STRONG_BUY" if buy_count >= 5 else "BUY"
    elif sell_count >= buy_count + 3:
        recommendation = "STRONG_SELL" if sell_count >= 5 else "SELL"
    else:
        recommendation = "NEUTRAL"

    return {
        "source": "Algoritmo Cuantitativo (Pandas)",
        "data_quality": "COMPLETE",
        "recommendation": recommendation,
        "buy_signals": buy_count,
        "sell_signals": sell_count,
        "neutral_signals": neutral_count,
        "rsi": rsi_14,
        "macd": macd_val,
        "macd_signal": macd_signal,
        "macd_hist": macd_hist,
        "atr_14": atr_14,
        "sma_20": round(sma_20, 2) if sma_20 else None,
        "sma_50": round(sma_50, 2) if sma_50 else None,
        "sma_200": round(sma_200, 2) if sma_200 else None,
        "ema_20": round(ema_20, 2) if ema_20 else None,
        "ema_50": round(ema_50, 2) if ema_50 else None,
        "bb_upper": bb_upper,
        "bb_lower": bb_lower,
    }


def _get_tradingview_mapping(ticker: str) -> Dict[str, str]:
    """Mapeo a symbol/exchange para tradingview_ta (legado)."""
    ticker_clean = ticker.strip().upper()
    if "-USD" in ticker_clean:
        base = ticker_clean.replace("-USD", "")
        return {"symbol": f"{base}USD", "screener": "crypto", "exchange": "BINANCE"}
    if ticker_clean in ("SPY", "VOO", "QQQ", "DIA", "IWM", "VTI", "GLD", "SLV", "TLT"):
        exchange = "AMEX" if ticker_clean in ("SPY", "GLD", "SLV", "VOO") else "NASDAQ"
        return {"symbol": ticker_clean, "screener": "america", "exchange": exchange}
    return {"symbol": ticker_clean, "screener": "america", "exchange": "NASDAQ"}


def get_technical_analysis(ticker: str) -> Dict[str, Any]:
    """TradingView via web_sources (primario) + fallback algorítmico."""
    ticker = normalize_ticker_input(ticker)
    cached = _cache_get(ticker)
    if cached is not None:
        return cached
    result = _compute_technical_analysis(ticker)
    _cache_set(ticker, result)
    return result


def _compute_technical_analysis(ticker: str) -> Dict[str, Any]:
    """Ejecuta el análisis técnico (sin caché)."""
    # 1) TradingView vía web_sources (parser HTML)
    tv = fetch_tradingview(ticker)
    if tv.get("ok") and tv.get("recommendation"):
        tv_result = {
            "source": "TradingView (web_sources)",
            "data_quality": "COMPLETE",
            "recommendation": tv.get("recommendation", "NEUTRAL"),
            "buy_signals": tv.get("buy_count", 0),
            "sell_signals": tv.get("sell_count", 0),
            "neutral_signals": tv.get("neutral_count", 0),
            "rsi": tv.get("rsi_14"),
            "macd": None,
            "macd_signal": None,
            "macd_hist": None,
            "atr_14": None,
            "sma_20": tv.get("sma_20"),
            "sma_50": tv.get("sma_50"),
            "sma_200": tv.get("sma_200"),
            "ema_20": None,
            "ema_50": None,
            "bb_upper": None,
            "bb_lower": None,
        }
        # The TradingView HTML can expose its aggregate label while omitting the
        # individual indicators. Never treat those missing fields as zero/neutral:
        # calculate them from the same ticker's price history instead.
        missing_indicators = (
            tv_result["rsi"] is None
            or tv_result["sma_200"] is None
            or (tv_result["buy_signals"] + tv_result["sell_signals"] + tv_result["neutral_signals"] == 0)
        )
        if not missing_indicators:
            return tv_result
        try:
            import yfinance as yf
            hist = yf.Ticker(to_yfinance_ticker(ticker)).history(period="1y")
            if not hist.empty and len(hist) >= 20:
                calculated = _calculate_indicators_pandas(hist)
                # Preserve TradingView's aggregate view, but use calculated values
                # only to fill the fields that TradingView did not publish.
                for key in ("rsi", "sma_20", "sma_50", "sma_200"):
                    if tv_result[key] is None:
                        tv_result[key] = calculated[key]
                if tv_result["buy_signals"] + tv_result["sell_signals"] + tv_result["neutral_signals"] == 0:
                    for key in ("buy_signals", "sell_signals", "neutral_signals"):
                        tv_result[key] = calculated[key]
                for key in ("macd", "macd_signal", "macd_hist", "atr_14", "ema_20", "ema_50", "bb_upper", "bb_lower"):
                    tv_result[key] = calculated[key]
                tv_result["source"] = "TradingView + Algoritmo Cuantitativo (Pandas)"
                tv_result["data_quality"] = "COMPLETE"
                return tv_result
        except Exception as e:
            logger.debug(f"No se pudo completar técnico desde histórico para {ticker}: {e}")

        # Finviz can sometimes provide RSI when neither TradingView nor the
        # historical provider returns it. It does not replace SMA/MACD data.
        try:
            finviz = fetch_finviz(ticker)
            if finviz.get("ok") and tv_result["rsi"] is None and finviz.get("rsi") is not None:
                tv_result["rsi"] = finviz["rsi"]
                tv_result["source"] = "TradingView + Finviz (RSI)"
        except Exception:
            pass
        tv_result["data_quality"] = "PARTIAL"
        return tv_result

    # 2) tradingview_ta legacy (si está instalado)
    if HAS_TRADINGVIEW_TA:
        tv_info = _get_tradingview_mapping(ticker)
        try:
            with _tv_lock:
                time.sleep(0.2)
                handler = TA_Handler(
                    symbol=tv_info["symbol"],
                    screener=tv_info["screener"],
                    exchange=tv_info["exchange"],
                    interval=Interval.INTERVAL_1_DAY,
                )
                try:
                    analysis = handler.get_analysis()
                except Exception:
                    time.sleep(0.6)
                    analysis = handler.get_analysis()
            summary = analysis.summary or {}
            indicators = analysis.indicators or {}
            rec = summary.get("RECOMMENDATION", "NEUTRAL").upper()
            rsi_val = indicators.get("RSI")
            if rsi_val is not None:
                rsi_val = round(float(rsi_val), 2)
            return {
                "source": "TradingView TA Scanner",
                "data_quality": "COMPLETE",
                "recommendation": rec,
                "buy_signals": int(summary.get("BUY", 0)),
                "sell_signals": int(summary.get("SELL", 0)),
                "neutral_signals": int(summary.get("NEUTRAL", 0)),
                "rsi": rsi_val,
                "macd": round(float(indicators.get("MACD.macd")), 4) if indicators.get("MACD.macd") else None,
                "macd_signal": round(float(indicators.get("MACD.signal")), 4) if indicators.get("MACD.signal") else None,
                "macd_hist": round(float(indicators["MACD.macd"] - indicators["MACD.signal"]), 4) if indicators.get("MACD.macd") and indicators.get("MACD.signal") else None,
                "sma_20": round(float(indicators["SMA20"]), 2) if indicators.get("SMA20") else None,
                "sma_50": round(float(indicators["SMA50"]), 2) if indicators.get("SMA50") else None,
                "sma_200": round(float(indicators["SMA200"]), 2) if indicators.get("SMA200") else None,
                "ema_20": round(float(indicators["EMA20"]), 2) if indicators.get("EMA20") else None,
                "ema_50": round(float(indicators["EMA50"]), 2) if indicators.get("EMA50") else None,
                "bb_upper": round(float(indicators["BB.upper"]), 2) if indicators.get("BB.upper") else None,
                "bb_lower": round(float(indicators["BB.lower"]), 2) if indicators.get("BB.lower") else None,
            }
        except Exception as e:
            logger.warning(f"TradingView TA no disponible para {ticker} ({e}). Usando fallback...")

    # 3) Fallback algorítmico desde yfinance
    try:
        import yfinance as yf
        hist = yf.Ticker(to_yfinance_ticker(ticker)).history(period="1y")
        if not hist.empty and len(hist) >= 20:
            return _calculate_indicators_pandas(hist)
    except Exception as e:
        logger.error(f"Error en fallback técnico para {ticker}: {e}")

    # 4) Datos neutros por defecto
    return {
        "source": "No disponible",
        "data_quality": "UNAVAILABLE",
        "recommendation": "NEUTRAL",
        "buy_signals": 0,
        "sell_signals": 0,
        "neutral_signals": 0,
        "rsi": 50.0,
        "macd": None,
        "macd_signal": None,
        "macd_hist": None,
        "sma_20": None,
        "sma_50": None,
        "sma_200": None,
        "ema_20": None,
        "ema_50": None,
        "bb_upper": None,
        "bb_lower": None,
    }
