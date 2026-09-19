"""
Calendario y Fechas de Pago de Dividendos para posiciones XTB (USD).

Fuente primaria: yfinance (Yahoo Finance) — la más fiable para dividend rate,
yield, fechas ex-dividendo, historial de pagos y proyección del próximo ciclo.

Complemento opcional: Macrotrends / MarketScreener vía web_sources si
se requiere enriquecimiento posterior.
"""
from typing import List, Dict, Any, Optional
from datetime import datetime, date, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
import logging

from portfolio_analyzer import load_positions
from data_fetchers.web_sources import to_yfinance_ticker

logger = logging.getLogger(__name__)

MESES_ES = {
    1: "Ene", 2: "Feb", 3: "Mar", 4: "Abr", 5: "May", 6: "Jun",
    7: "Jul", 8: "Ago", 9: "Sep", 10: "Oct", 11: "Nov", 12: "Dic"
}

MESES_COMPLETO_ES = {
    1: "Enero", 2: "Febrero", 3: "Marzo", 4: "Abril", 5: "Mayo", 6: "Junio",
    7: "Julio", 8: "Agosto", 9: "Septiembre", 10: "Octubre", 11: "Noviembre", 12: "Diciembre"
}

_cached_dividend_report: Optional[Dict[str, Any]] = None
_cache_timestamp: Optional[datetime] = None
CACHE_TTL_SECONDS = 600


def _format_date_friendly(d: date) -> str:
    return f"{d.day:02d} {MESES_ES.get(d.month, '')} {d.year}"


def _format_month_friendly(d: date) -> str:
    return f"Aprox. {MESES_COMPLETO_ES.get(d.month, '')} {d.year}"


def _detect_frequency(diff_days: float) -> str:
    if diff_days < 45:
        return "Mensual"
    if 70 <= diff_days <= 110:
        return "Trimestral"
    if 150 <= diff_days <= 220:
        return "Semestral"
    return "Anual"


def _analyze_single_ticker_dividends(pos: Dict[str, Any], today: date) -> Dict[str, Any]:
    """Extrae calendario real de dividendos desde yfinance."""
    ticker = pos.get("ticker", "").strip().upper()
    name = pos.get("name", ticker)
    shares = float(pos.get("shares", 0.0))
    currency = "USD"
    source = pos.get("source", "XTB")
    platform = "XTB"

    dividend_rate = None
    dividend_yield = None
    last_div_val = 0.0
    last_div_date = None
    recent_history = False
    frequency = "Trimestral"
    ex_date_final: Optional[date] = None
    pay_date_final: Optional[date] = None
    notes = ""
    status = "ESTIMADA"

    try:
        import yfinance as yf
        tk = yf.Ticker(to_yfinance_ticker(ticker))
        info = tk.info or {}

        dividend_rate = info.get("dividendRate") or info.get("trailingAnnualDividendRate")
        dividend_yield_raw = info.get("dividendYield") or info.get("trailingAnnualDividendYield")
        if dividend_yield_raw is not None:
            dividend_yield = round(dividend_yield_raw * 100, 2) if dividend_yield_raw < 0.08 else round(dividend_yield_raw, 2)

        # Calendar oficial
        cal = getattr(tk, "calendar", None) or {}
        if isinstance(cal, dict):
            cal_div = cal.get("Dividend Date")
            cal_ex = cal.get("Ex-Dividend Date")
            if hasattr(cal_div, "year"):
                pay_date_final = cal_div.date() if hasattr(cal_div, "date") else cal_div
            if hasattr(cal_ex, "year"):
                ex_date_final = cal_ex.date() if hasattr(cal_ex, "date") else cal_ex
            if pay_date_final:
                status = "CONFIRMADA"
                notes = "Fecha oficial confirmada por la empresa."

        # Historial de dividendos
        try:
            divs = tk.dividends
            if divs is not None and not divs.empty:
                last_dt = divs.index[-1]
                last_div_date = last_dt.date() if hasattr(last_dt, "date") else None
                last_div_val = float(divs.iloc[-1])
                if last_div_date and (today - last_div_date).days <= 730:
                    recent_history = True

                # Detectar frecuencia
                if len(divs) >= 2:
                    diffs = [(divs.index[i] - divs.index[i-1]).days for i in range(1, len(divs))]
                    avg = sum(diffs[-3:]) / min(3, len(diffs))
                    frequency = _detect_frequency(avg)

                # Si no hay calendar, proyectar próximo ciclo
                if not pay_date_final and last_div_date:
                    cycle_days = {"Trimestral": 91, "Semestral": 182, "Mensual": 30, "Anual": 365}[frequency]
                    nxt = last_div_date
                    while nxt < today:
                        nxt = nxt + timedelta(days=cycle_days)
                    pay_date_final = nxt
                    ex_date_final = pay_date_final - timedelta(days=14)
                    status = "ESTIMADA"
                    notes = f"Proyección basada en cadencia {frequency.lower()} (último pago {last_div_date})."
        except Exception as e:
            logger.debug(f"Error leyendo historial de dividendos para {ticker}: {e}")

        # exDividendDate timestamp (si está disponible)
        if not ex_date_final and info.get("exDividendDate"):
            try:
                ex_date_final = datetime.fromtimestamp(info["exDividendDate"]).date()
            except Exception:
                pass

        # Si pago ya pasó, proyectar el siguiente
        if pay_date_final and pay_date_final < today:
            cycle_days = {"Trimestral": 91, "Semestral": 182, "Mensual": 30, "Anual": 365}.get(frequency, 91)
            nxt = pay_date_final
            while nxt < today:
                nxt = nxt + timedelta(days=cycle_days)
            pay_date_final = nxt
            ex_date_final = nxt - timedelta(days=14)
            status = "ESTIMADA"
            notes = f"Próximo ciclo estimado ({frequency.lower()})."
    except Exception as e:
        logger.debug(f"Error general dividends para {ticker}: {e}")

    has_dividend = bool(
        (dividend_rate and dividend_rate > 0)
        or recent_history
        or (pay_date_final and pay_date_final >= today)
        or (ex_date_final and ex_date_final >= today)
    )

    if not has_dividend:
        return {
            "ticker": ticker,
            "name": name,
            "shares": shares,
            "currency": currency,
            "source": source,
            "platform": platform,
            "has_dividend": False,
            "status": "SIN_DIVIDENDO",
            "status_badge": "Sin dividendo",
            "frequency": "No paga dividendo",
            "ex_dividend_date": None,
            "payment_date": None,
            "ex_date_display": "N/A",
            "payment_date_display": "No distribuye dividendos",
            "dividend_rate": None,
            "dividend_yield": dividend_yield,
            "dividend_per_share": 0.0,
            "estimated_payout": 0.0,
            "annual_payout": 0.0,
            "days_until_ex": None,
            "days_until_payment": None,
            "sort_date": "1900-01-01",
            "notes": "Empresa orientada al crecimiento y reinversión de utilidades; no distribuye dividendos regulares.",
        }

    payments_per_year = {"Trimestral": 4, "Semestral": 2, "Mensual": 12, "Anual": 1}.get(frequency, 4)
    if last_div_val > 0:
        dps_payment = round(last_div_val, 4)
    elif dividend_rate and dividend_rate > 0:
        dps_payment = round(dividend_rate / payments_per_year, 4)
    else:
        dps_payment = 0.0

    if not dividend_rate and dps_payment > 0:
        dividend_rate = round(dps_payment * payments_per_year, 4)

    estimated_payout = round(shares * dps_payment, 2)
    annual_payout = round(shares * (dividend_rate or (dps_payment * payments_per_year)), 2)

    days_until_ex = (ex_date_final - today).days if ex_date_final else None
    days_until_payment = (pay_date_final - today).days if pay_date_final else None

    if status == "CONFIRMADA":
        pay_display = _format_date_friendly(pay_date_final) if pay_date_final else "Confirmada (Pendiente)"
        ex_display = _format_date_friendly(ex_date_final) if ex_date_final else "Confirmada"
        status_badge = "Confirmada"
    else:
        pay_display = _format_month_friendly(pay_date_final) if pay_date_final else "Aprox. Próximo ciclo"
        ex_display = f"Aprox. {_format_date_friendly(ex_date_final)}" if ex_date_final else "Aprox. previo a pago"
        status_badge = "Estimada"

    sort_date = pay_date_final.strftime("%Y-%m-%d") if pay_date_final else "1900-01-01"

    return {
        "ticker": ticker,
        "name": name,
        "shares": shares,
        "currency": currency,
        "source": source,
        "platform": platform,
        "has_dividend": True,
        "status": status,
        "status_badge": status_badge,
        "frequency": frequency,
        "ex_dividend_date": ex_date_final.strftime("%Y-%m-%d") if ex_date_final else None,
        "payment_date": pay_date_final.strftime("%Y-%m-%d") if pay_date_final else None,
        "ex_date_display": ex_display,
        "payment_date_display": pay_display,
        "dividend_rate": round(dividend_rate, 4) if dividend_rate else None,
        "dividend_yield": dividend_yield,
        "dividend_per_share": dps_payment,
        "estimated_payout": estimated_payout,
        "annual_payout": annual_payout,
        "days_until_ex": days_until_ex,
        "days_until_payment": days_until_payment,
        "sort_date": sort_date,
        "notes": notes or "Calendario desde Yahoo Finance.",
    }


def get_dividend_schedule_for_positions(
    positions: Optional[List[Dict[str, Any]]] = None,
    force_refresh: bool = False,
) -> Dict[str, Any]:
    """Genera el informe consolidado de dividendos para posiciones XTB."""
    global _cached_dividend_report, _cache_timestamp

    now = datetime.now()
    if not force_refresh and _cached_dividend_report is not None and _cache_timestamp is not None:
        elapsed = (now - _cache_timestamp).total_seconds()
        if elapsed < CACHE_TTL_SECONDS:
            return _cached_dividend_report

    pos_list = positions if positions is not None else load_positions()
    today = date.today()

    results: List[Dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(_analyze_single_ticker_dividends, p, today): p for p in pos_list}
        for future in as_completed(futures):
            try:
                results.append(future.result())
            except Exception as e:
                logger.error(f"Error analizando dividendo de {futures[future].get('ticker')}: {e}")

    platforms: Dict[str, List[Dict[str, Any]]] = {"XTB": []}
    for item in results:
        platforms["XTB"].append(item)

    # Ordenar por ex-date (más reciente primero), luego por payment_date
    def _sort_key(item):
        # Priorizar próximos ex-dates primero; pagos confirmados arriba
        ex_date = item.get("ex_dividend_date") or "9999-12-31"
        pay_date = item.get("payment_date") or "9999-12-31"
        status_priority = 0 if item.get("status") == "CONFIRMADA" else 1
        return (status_priority, ex_date, pay_date)

    platforms["XTB"].sort(key=_sort_key)

    all_sorted = sorted(results, key=lambda x: (x.get("sort_date") or "1900-01-01"), reverse=True)

    dividend_payers = [r for r in results if r.get("has_dividend")]
    total_positions_count = len(results)
    paying_count = len(dividend_payers)

    upcoming_payments = [r for r in dividend_payers if r.get("days_until_payment") is not None and r["days_until_payment"] >= 0]
    upcoming_payments.sort(key=lambda x: x["days_until_payment"])
    next_payment = upcoming_payments[0] if upcoming_payments else (dividend_payers[0] if dividend_payers else None)

    upcoming_ex = [r for r in dividend_payers if r.get("days_until_ex") is not None and r["days_until_ex"] >= 0]
    upcoming_ex.sort(key=lambda x: x["days_until_ex"])
    next_ex = upcoming_ex[0] if upcoming_ex else None

    total_annual_usd = sum(r.get("annual_payout", 0.0) for r in results if r.get("currency") == "USD")

    report = {
        "updated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "total_positions": total_positions_count,
        "paying_count": paying_count,
        "non_paying_count": total_positions_count - paying_count,
        "total_annual_usd": round(total_annual_usd, 2),
        "next_payment": next_payment,
        "next_ex_dividend": next_ex,
        "platforms": platforms,
        "all_positions": all_sorted,
        "data_sources": ["Yahoo Finance"],
    }

    _cached_dividend_report = report
    _cache_timestamp = now
    return report
