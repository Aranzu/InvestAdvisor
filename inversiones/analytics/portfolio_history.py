"""
Histórico del portafolio: series temporales de valor total y por posición.

Obtiene precios históricos vía yfinance, calcula el valor del portafolio
en cada punto y devuelve series para sparklines y métricas.
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional

from data_fetchers.web_sources import to_yfinance_ticker

_history_cache: Dict[str, Any] = {}
_HISTORY_CACHE_TTL = 600  # 10 minutos


def clear_history_cache() -> None:
    _history_cache.clear()


def get_history_for_position(
    ticker: str,
    shares: float,
    period: str = "3mo",
) -> List[Dict[str, Any]]:
    """Histórico de precio de una posición (lista de {date, value})."""
    try:
        import yfinance as yf
        yf_symbol = to_yfinance_ticker(ticker)
        hist = yf.Ticker(yf_symbol).history(period=period, interval="1d")
        if hist is None or hist.empty:
            return []
        out = []
        for date, row in hist.iterrows():
            try:
                d = date.date() if hasattr(date, "date") else date
                out.append({
                    "date": d.strftime("%Y-%m-%d"),
                    "value": round(float(row["Close"]) * float(shares), 2),
                })
            except Exception:
                continue
        return out
    except Exception:
        return []


def _fetch_ticker_history(ticker: str, shares: float, yf_period: str) -> Dict[str, float]:
    try:
        import yfinance as yf
        yf_symbol = to_yfinance_ticker(ticker)
        hist = yf.Ticker(yf_symbol).history(period=yf_period, interval="1d")
        if hist is None or hist.empty:
            return {}
        res: Dict[str, float] = {}
        for date, row in hist.iterrows():
            d = date.date() if hasattr(date, "date") else date
            key = d.strftime("%Y-%m-%d")
            res[key] = float(row["Close"]) * shares
        return res
    except Exception:
        return {}


def get_portfolio_history(
    positions: List[Dict[str, Any]],
    days: int = 30,
) -> Dict[str, Any]:
    """Histórico agregado del portafolio (suma de todas las posiciones por día)."""
    if not positions:
        return {"series": [], "period_days": days, "start_value": 0, "end_value": 0, "change_pct": 0}

    tickers_sig = ",".join(sorted(p.get("ticker", "") for p in positions))
    cache_key = f"{tickers_sig}:{days}"
    now = time.time()
    cached = _history_cache.get(cache_key)
    if cached and (now - cached[0]) < _HISTORY_CACHE_TTL:
        return dict(cached[1])

    invested_total = sum(
        float(p.get("shares", 0)) * float(p.get("entry_price", 0))
        for p in positions
    )

    period_map = {"30": "1mo", "90": "3mo", "180": "6mo", "365": "1y", "730": "2y"}
    yf_period = period_map.get(str(days), "3mo")

    valid_positions = [
        (p.get("ticker", "").strip().upper(), float(p.get("shares", 0)))
        for p in positions
        if p.get("ticker", "").strip() and float(p.get("shares", 0)) > 0
    ]

    histories: Dict[str, Dict[str, float]] = {}
    if valid_positions:
        with ThreadPoolExecutor(max_workers=min(8, len(valid_positions))) as executor:
            future_map = {
                executor.submit(_fetch_ticker_history, t, s, yf_period): t
                for t, s in valid_positions
            }
            for future in as_completed(future_map):
                t = future_map[future]
                try:
                    res = future.result()
                    for k, val in res.items():
                        histories.setdefault(k, {})
                        histories[k][t] = val
                except Exception:
                    continue

    if not histories:
        return {
            "series": [],
            "period_days": days,
            "start_value": 0,
            "end_value": 0,
            "change_pct": 0,
        }

    series = []
    for date in sorted(histories.keys()):
        total = sum(histories[date].values())
        series.append({"date": date, "value": round(total, 2)})

    start_value = series[0]["value"] if series else 0
    end_value = series[-1]["value"] if series else 0

    if start_value and start_value > 0:
        change_pct = round(((end_value - start_value) / start_value * 100), 2)
    else:
        change_pct = 0.0

    result = {
        "series": series,
        "period_days": days,
        "start_value": round(start_value, 2) if start_value else 0,
        "end_value": round(end_value, 2) if end_value else 0,
        "change_pct": change_pct,
        "invested": round(invested_total, 2),
    }
    _history_cache[cache_key] = (now, result)
    return result


def get_all_positions_history(positions: List[Dict[str, Any]], days: int = 30) -> Dict[str, List[Dict[str, Any]]]:
    """Histórico individual por posición (para sparklines en la tabla). Concurrente."""
    valid_positions = [
        (p.get("ticker", "").strip().upper(), float(p.get("shares", 0)))
        for p in positions
        if p.get("ticker", "").strip() and float(p.get("shares", 0)) > 0
    ]
    if not valid_positions:
        return {}

    out = {}
    with ThreadPoolExecutor(max_workers=min(8, len(valid_positions))) as executor:
        future_map = {
            executor.submit(get_history_for_position, t, s, period=f"{days}d"): t
            for t, s in valid_positions
        }
        for future in as_completed(future_map):
            t = future_map[future]
            try:
                history = future.result()
                if history:
                    out[t] = history
            except Exception:
                continue
    return out


def get_allocation_by_sector(positions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Distribución del portafolio por sector (% del valor total)."""
    total_value = sum(float(p.get("current_value", 0)) for p in positions)
    if total_value <= 0:
        return []

    by_sector: Dict[str, float] = {}
    for p in positions:
        sector = p.get("sector", "Sin clasificar") or "Sin clasificar"
        val = float(p.get("current_value", 0))
        by_sector[sector] = by_sector.get(sector, 0) + val

    items = []
    for sector, val in sorted(by_sector.items(), key=lambda x: -x[1]):
        pct = (val / total_value) * 100
        items.append({
            "sector": sector,
            "value": round(val, 2),
            "pct": round(pct, 1),
        })
    return items


def get_daily_change(positions: List[Dict[str, Any]]) -> Dict[str, Any]:
    """P&L del día (cambio % ponderado por valor actual de cada posición)."""
    total_yesterday = 0.0
    total_today = 0.0

    for p in positions:
        ticker = p.get("ticker", "").strip().upper()
        shares = float(p.get("shares", 0))
        if not ticker or shares <= 0:
            continue
        try:
            import yfinance as yf
            tk = yf.Ticker(ticker)
            info = tk.info or {}
            cur = float(p.get("current_price") or info.get("regularMarketPrice") or 0)
            prev = float(info.get("previousClose") or info.get("regularMarketPreviousClose") or cur)
            total_today += cur * shares
            total_yesterday += prev * shares
        except Exception:
            pass

    if total_yesterday <= 0:
        return {"change_usd": 0, "change_pct": 0, "yesterday_value": 0, "today_value": 0}
    change_usd = total_today - total_yesterday
    change_pct = (change_usd / total_yesterday) * 100
    return {
        "change_usd": round(change_usd, 2),
        "change_pct": round(change_pct, 2),
        "yesterday_value": round(total_yesterday, 2),
        "today_value": round(total_today, 2),
    }


def get_monthly_change(positions: List[Dict[str, Any]]) -> Dict[str, Any]:
    """P&L del último mes (sparkline vs hace 30 días)."""
    history = get_portfolio_history(positions, days=30)
    if not history.get("series") or len(history["series"]) < 2:
        return {"change_usd": 0, "change_pct": 0, "history": []}
    series = history["series"]
    start_val = series[0]["value"]
    end_val = series[-1]["value"]
    if start_val > 0:
        change_pct = round((end_val - start_val) / start_val * 100, 2)
    else:
        change_pct = 0
    return {
        "change_usd": round(end_val - start_val, 2),
        "change_pct": change_pct,
        "history": series,
    }
