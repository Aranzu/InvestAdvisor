"""Fundamentales y proyecciones de analistas sobre Yahoo Finance (funcion 2).

Este modulo es la unica puerta a los datos **fundamentales** del screener. Igual
que ``data_yahoo`` para las velas, expone un ``fetcher`` inyectable para que los
tests corran offline y deterministas, y guarda lo descargado en un cache JSON con
TTL para no golpear la red en cada corrida.

Por que cache con TTL y no incremental como las velas: los fundamentales no
tienen "rango de fechas". Cambian de golpe (cuando la empresa reporta o cuando un
analista mueve el objetivo), asi que se guardan por dia y se refrezcan cuando
vence el TTL. Un valor de hoy es "actualizado" por definicion de corrida programada.

El parseo de ``Ticker.info`` vive aca y no en ``core``: es I/O disfrazado. ``core``
solo ve un ``Fundamentals`` ya normalizado.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from swing.core.fundamentals import Fundamentals
from swing.core.piotroski import AnnualInputs, f_score

#: Devuelve el diccionario crudo de ``Ticker.info`` (o uno equivalente), o ``None``.
InfoFetcher = Callable[[str], Mapping[str, object] | None]

#: Dias que un fundamento cacheado se considera vigente.
DEFAULT_TTL_DAYS = 1

#: Recorte del resumen del negocio: entra en un prompt, no en una tesis.
SUMMARY_CHARS = 600

#: Version del formato cacheado. Se sube cuando cambia la normalizacion de un
#: campo, para que lo guardado con la regla vieja no se lea como vigente.
CACHE_SCHEMA = 5


def yfinance_info(symbol: str) -> Mapping[str, object] | None:
    """``Ticker.info`` mas objetivos de analistas y estimaciones (best effort).

    Se enriquece el ``info`` con ``analyst_price_targets`` (mas confiable que las
    claves de ``info``) y con las estimaciones de crecimiento si estan. Cada
    fuente secundaria va en su propio ``try``: que falle una no invalida el resto.
    """
    import yfinance as yf

    ticker = yf.Ticker(symbol)
    try:
        info = ticker.get_info()
    except Exception:
        return None
    if not isinstance(info, dict):
        return None
    merged: dict[str, object] = dict(info)

    try:
        targets = ticker.analyst_price_targets
    except Exception:
        targets = None
    if isinstance(targets, dict):
        merged.setdefault("targetMeanPrice", targets.get("mean"))
        merged.setdefault("targetHighPrice", targets.get("high"))
        merged.setdefault("targetLowPrice", targets.get("low"))

    try:
        eps_est = _plus_one_year(ticker.growth_estimates, "stockTrend")
    except Exception:
        eps_est = None
    try:
        rev_est = _plus_one_year(ticker.revenue_estimate, "growth")
    except Exception:
        rev_est = None
    if eps_est is not None:
        merged["_eps_growth_next_year"] = eps_est
    if rev_est is not None:
        merged["_revenue_growth_next_year"] = rev_est
    _merge_eps_trend(ticker, merged)
    _merge_eps_revisions(ticker, merged)
    _merge_analyst_revisions(ticker, merged)
    _merge_quarterly(ticker, merged)
    _merge_annual(ticker, merged)
    _merge_earnings(ticker, merged)
    _merge_primary_document(ticker, merged)
    return merged


def _period_row(frame: Any, period: str = "0q") -> Any:
    """Fila de un DataFrame de yfinance (``eps_trend``/``eps_revisions``) por periodo."""
    if frame is None or not hasattr(frame, "loc"):
        return None
    try:
        if period in frame.index:
            return frame.loc[period]
        return frame.iloc[0]
    except Exception:
        return None


def _series_value(row: Any, *names: str) -> float | None:
    """Primer valor numerico de la fila cuya etiqueta coincide (sin distinguir mayusculas)."""
    if row is None or not hasattr(row, "index"):
        return None
    wanted = {name.strip().lower() for name in names}
    try:
        for label, value in row.items():
            if str(label).strip().lower() not in wanted:
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            number = float(value)
            if number == number:  # descarta NaN
                return number
    except Exception:
        return None
    return None


#: Base minima de EPS (USD) para publicar una revision: debajo de esto el porcentaje
#: es ruido (una base de 0,05 USD convierte un centavo en +20 %).
EPS_REVISION_MIN_BASE = 0.25


def _merge_eps_trend(ticker: object, merged: dict[str, object]) -> None:
    """Consenso de EPS actual y de hace 60 dias, separado por ano.

    ``eps_trend`` trae ``0q``/``+1q`` (trimestre) y ``0y``/``+1y`` (ano en curso y
    proximo). Se publican los dos anos porque la revision del trimestre y la del
    ano siguiente pueden apuntar en direcciones distintas. Si la base (consenso de
    hace 60 dias) es menor a ``EPS_REVISION_MIN_BASE``, la cifra se suprime.
    """
    try:
        frame = ticker.eps_trend  # type: ignore[attr-defined]
    except Exception:
        return
    old_row = _period_row(frame)
    current = _series_value(old_row, "current")
    old = _series_value(old_row, "60daysAgo", "60daysAgoConsensus")
    if current is not None:
        merged["_eps_current"] = current
    if old is not None:
        merged["_eps_consensus_60d_ago"] = old
        if current is not None and old != 0.0:
            merged["_eps_revision_60d"] = (current - old) / abs(old)

    for period, key, flag in (
        ("0y", "_eps_revision_cy", "_eps_revision_cy_suppressed"),
        ("+1y", "_eps_revision_ny", "_eps_revision_ny_suppressed"),
    ):
        row = _period_row(frame, period)
        current_year = _series_value(row, "current")
        base_year = _series_value(row, "60daysAgo", "60daysAgoConsensus")
        if current_year is None or base_year is None:
            continue
        if abs(base_year) < EPS_REVISION_MIN_BASE:
            merged[flag] = True
            continue
        merged[key] = (current_year - base_year) / abs(base_year)


def _merge_eps_revisions(ticker: object, merged: dict[str, object]) -> None:
    """Conteo de alzas/bajas de EPS. yfinance hoy solo publica ventanas de 7 y 30 dias."""
    try:
        row = _period_row(ticker.eps_revisions)  # type: ignore[attr-defined]
    except Exception:
        return
    up_60 = _series_value(row, "upLast60days", "upLast60Days", "upLast60d")
    down_60 = _series_value(row, "downLast60days", "downLast60Days", "downLast60d")
    if up_60 is not None:
        merged["_analyst_up_60d"] = int(up_60)
    if down_60 is not None:
        merged["_analyst_down_60d"] = int(down_60)
    up_30 = _series_value(row, "upLast30days", "upLast30Days", "upLast30d")
    down_30 = _series_value(row, "downLast30days", "downLast30Days", "downLast30d")
    if up_30 is not None:
        merged["_analyst_up_30d"] = int(up_30)
    if down_30 is not None:
        merged["_analyst_down_30d"] = int(down_30)


def _merge_analyst_revisions(ticker: object, merged: dict[str, object]) -> None:
    """Fecha del ultimo cambio de recomendacion/objetivo (``upgrades_downgrades``).

    Sin esta fecha el objetivo medio no tiene antiguedad: el informe no puede
    marcarlo como desactualizado (> 90 dias) y el prompt pide hacerlo.
    """
    try:
        frame = ticker.upgrades_downgrades  # type: ignore[attr-defined]
    except Exception:
        return
    if frame is None or not hasattr(frame, "index"):
        return
    days: list[dt.date] = []
    try:
        for value in frame.index:
            day = _column_date(value)
            if day is not None:
                days.append(day)
    except Exception:
        return
    if days:
        merged["_last_analyst_revision_date"] = max(days).isoformat()


_ANNUAL_ROWS: dict[str, tuple[str, tuple[str, ...]]] = {
    "net_income": ("financials", ("Net Income", "Net Income Common Stockholders")),
    "revenue": ("financials", ("Total Revenue", "Operating Revenue")),
    "gross_profit": ("financials", ("Gross Profit",)),
    "total_assets": ("balance_sheet", ("Total Assets",)),
    "long_term_debt": ("balance_sheet", ("Long Term Debt", "Long Term Debt And Capital Lease Obligation")),
    "current_assets": ("balance_sheet", ("Current Assets", "Total Current Assets")),
    "current_liabilities": ("balance_sheet", ("Current Liabilities", "Total Current Liabilities")),
    "shares": ("balance_sheet", ("Ordinary Shares Number", "Share Issued")),
    "operating_cash_flow": (
        "cashflow",
        ("Operating Cash Flow", "Cash Flow From Continuing Operating Activities"),
    ),
}


def _merge_annual(ticker: object, merged: dict[str, object]) -> None:
    """F-Score de Piotroski con los dos ultimos ejercicios anuales (``_f_score``/``_f_score_n``).

    Cada cifra se ancla a la fecha de su ejercicio; si el balance o el flujo no tienen esa
    fecha, la senal queda sin calcular en vez de comparar ejercicios distintos.
    """
    frames = {name: _frame(ticker, name) for name in ("financials", "balance_sheet", "cashflow")}
    series = {
        key: _dated_series(frames[frame_name], aliases) for key, (frame_name, aliases) in _ANNUAL_ROWS.items()
    }
    dates = sorted(set(series["revenue"]) | set(series["net_income"]))
    if len(dates) < 2:
        return
    current_date, previous_date = dates[-1], dates[-2]

    def inputs(day: dt.date) -> AnnualInputs:
        return AnnualInputs(**{key: values.get(day) for key, values in series.items()})

    result = f_score(inputs(current_date), inputs(previous_date))
    if result.computed:
        merged["_f_score"] = result.score
        merged["_f_score_n"] = result.computed


def _merge_quarterly(ticker: object, merged: dict[str, object]) -> None:
    """Ultimos trimestres alineados por fecha: FCF, deuda neta, margenes y Y/Y.

    Antes se tomaban las filas como listas posicionales y se comparaba ``[-1]``
    con ``[-5]``. Al descartar NaN, una columna faltante corria la serie y el Y/Y
    terminaba comparando trimestres distintos. Ahora cada valor se ancla a la
    fecha de su columna y el año anterior se busca por fecha, no por posicion.
    """
    financials = _frame(ticker, "quarterly_financials")
    cashflow = _frame(ticker, "quarterly_cashflow")
    balance = _frame(ticker, "quarterly_balance_sheet")

    revenue = _dated_series(financials, ("Total Revenue", "Operating Revenue"))
    operating = _dated_series(financials, ("Operating Income", "Operating Income Or Loss", "EBIT"))
    net_income = _dated_series(
        financials,
        ("Net Income", "Net Income Common Stockholders", "Net Income Including Noncontrolling Interests"),
    )
    fcf = _chronological(cashflow, ("Free Cash Flow",))
    if len(fcf) < 2:
        ocf = _chronological(cashflow, ("Operating Cash Flow", "Cash Flow From Continuing Operating Activities"))
        capex = _chronological(cashflow, ("Capital Expenditure", "Capital Expenditures"))
        alternative = _sum_series(ocf, capex)
        if len(alternative) > len(fcf):
            fcf = alternative
    net_debt = _chronological(balance, ("Net Debt",))
    if len(net_debt) < 2:
        debt = _chronological(balance, ("Total Debt",))
        cash = _chronological(
            balance,
            (
                "Cash And Cash Equivalents",
                "Cash Cash Equivalents And Short Term Investments",
                "Cash And Cash Equivalents And Short Term Investments",
            ),
        )
        alternative_debt = _subtract_series(debt, cash)
        if len(alternative_debt) > len(net_debt):
            net_debt = alternative_debt

    if len(fcf) >= 1:
        merged["_fcf_last_q"] = fcf[-1]
    if len(fcf) >= 2:
        merged["_fcf_prior_q"] = fcf[-2]
    if len(net_debt) >= 1:
        merged["_net_debt"] = net_debt[-1]
    if len(net_debt) >= 2:
        merged["_net_debt_change"] = net_debt[-1] - net_debt[-2]
    revenue_dates = sorted(revenue)
    if revenue_dates:
        merged["_revenue_last_q"] = revenue[revenue_dates[-1]]
    if len(revenue_dates) >= 2:
        merged["_revenue_prior_q"] = revenue[revenue_dates[-2]]
    if revenue_dates:
        growth_last = _growth_yoy(revenue, revenue_dates[-1])
        if growth_last is not None:
            merged["_revenue_growth_last_q"] = growth_last
    if len(revenue_dates) >= 2:
        growth_prior = _growth_yoy(revenue, revenue_dates[-2])
        if growth_prior is not None:
            merged["_revenue_growth_prior_q"] = growth_prior
    quarter_growth = [
        growth for day in revenue_dates[-4:] if (growth := _growth_yoy(revenue, day)) is not None
    ]
    if quarter_growth:
        merged["_revenue_growth_quarters"] = tuple(quarter_growth)

    common_dates = sorted(set(revenue) & set(operating))
    if not common_dates:
        return
    last = common_dates[-1]
    if revenue[last] == 0.0:
        return
    margin_last = operating[last] / revenue[last]
    merged["_oper_margin_last_q"] = margin_last
    merged["_margin_period"] = last.isoformat()
    merged["_margin_basis"] = "GAAP trimestral"
    if last in net_income:
        profit_last = net_income[last] / revenue[last]
        merged["_profit_margin_last_q"] = profit_last
        # Margen neto por encima del operativo = ganancias no operativas: la
        # cifra es real, pero no sirve como senal de deterioro sin separarlas.
        merged["_extraordinary_item"] = profit_last > margin_last + 1e-3
    year = _year_ago(common_dates, last)
    if year is not None and revenue[year] != 0.0:
        margin_year = operating[year] / revenue[year]
        merged["_oper_margin_yoy_bps"] = (margin_last - margin_year) * 10_000.0


def _frame(ticker: object, attribute: str) -> Any:
    try:
        frame = getattr(ticker, attribute)
    except Exception:
        return None
    return frame if hasattr(frame, "index") else None


def _chronological(frame: Any, aliases: Sequence[str]) -> list[float]:
    """Fila pedida, ordenada de la columna mas vieja a la mas nueva."""
    if frame is None or not hasattr(frame, "loc"):
        return []
    wanted = {alias.strip().lower() for alias in aliases}
    try:
        columns = list(frame.columns)
    except Exception:
        return []
    if not columns:
        return []
    try:
        ordered = sorted(columns, key=lambda column: str(column))
    except Exception:
        ordered = columns
    row_label = None
    try:
        for label in frame.index:
            if str(label).strip().lower() in wanted:
                row_label = label
                break
    except Exception:
        return []
    if row_label is None:
        return []
    out: list[float] = []
    try:
        series = frame.loc[row_label]
        for column in ordered:
            value = series[column]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            number = float(value)
            if number == number:
                out.append(number)
    except Exception:
        return []
    return out


def _dated_series(frame: Any, aliases: Sequence[str]) -> dict[dt.date, float]:
    """Fila pedida con la fecha de cada columna, sin huecos ni corrimientos.

    Las columnas de yfinance son cierres trimestrales (``Timestamp``). La fecha
    es lo unico que permite comparar el mismo trimestre año contra año cuando
    falta una columna intermedia.
    """
    if frame is None or not hasattr(frame, "loc"):
        return {}
    wanted = {alias.strip().lower() for alias in aliases}
    try:
        columns = list(frame.columns)
    except Exception:
        return {}
    row_label = None
    try:
        for label in frame.index:
            if str(label).strip().lower() in wanted:
                row_label = label
                break
    except Exception:
        return {}
    if row_label is None:
        return {}
    out: dict[dt.date, float] = {}
    try:
        series = frame.loc[row_label]
        for column in columns:
            day = _column_date(column)
            if day is None:
                continue
            value = series[column]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            number = float(value)
            if number != number:  # descarta NaN
                continue
            out[day] = number
    except Exception:
        return {}
    return out


def _column_date(value: object) -> dt.date | None:
    """Fecha de una columna trimestral, sea ``date``, ``datetime`` o ``Timestamp``."""
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    converter = getattr(value, "to_pydatetime", None)
    if callable(converter):
        try:
            converted = converter()
        except Exception:
            converted = None
        if isinstance(converted, dt.datetime):
            return converted.date()
        if isinstance(converted, dt.date):
            return converted
    try:
        return dt.date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _year_ago(dates: Sequence[dt.date], key: dt.date) -> dt.date | None:
    """Cierre trimestral mas cercano a un año antes de ``key`` (tolerancia 60 dias)."""
    if not dates:
        return None
    try:
        target = key.replace(year=key.year - 1)
    except ValueError:  # 29 de febrero
        target = key.replace(year=key.year - 1, day=28)
    candidates = [day for day in dates if day != key]
    if not candidates:
        return None
    best = min(candidates, key=lambda day: abs((day - target).days))
    return best if abs((best - target).days) <= 60 else None


def _growth_yoy(series: Mapping[dt.date, float], key: dt.date) -> float | None:
    """Crecimiento Y/Y de una serie fechada, comparando el mismo trimestre."""
    base = _year_ago(list(series), key)
    if base is None or series.get(base, 0.0) == 0.0:
        return None
    return series[key] / series[base] - 1.0


def _sum_series(left: Sequence[float], right: Sequence[float]) -> list[float]:
    if not left or not right:
        return []
    return [a + b for a, b in zip(left, right, strict=False)]


def _subtract_series(left: Sequence[float], right: Sequence[float]) -> list[float]:
    if not left or not right:
        return []
    return [a - b for a, b in zip(left, right, strict=False)]


def _calendar_earnings_date(ticker: object, today: dt.date) -> dt.date | None:
    """Fecha estimada del proximo reporte segun ``Ticker.calendar``."""
    try:
        calendar = ticker.calendar  # type: ignore[attr-defined]
    except Exception:
        return None
    values: object = None
    if isinstance(calendar, Mapping):
        values = calendar.get("Earnings Date")
    elif hasattr(calendar, "columns"):
        for key in ("Earnings Date", "EarningsDate"):
            if key in calendar.columns:
                values = calendar[key].tolist()
                break
    if values is None:
        return None
    if not isinstance(values, (list, tuple)):
        values = [values]
    days = []
    for value in values:
        moment = _as_datetime(value)
        if moment is not None and moment.date() >= today:
            days.append(moment.date())
    return min(days) if days else None


def _merge_earnings(ticker: object, merged: dict[str, object]) -> None:
    """Timing, movimiento implicito, movimiento historico y ultimo reporte."""
    dates: Any = None
    try:
        dates = ticker.earnings_dates  # type: ignore[attr-defined]
    except Exception:
        pass
    past: list[dt.datetime] = []
    upcoming: dt.date | None = None
    upcoming_time: int | None = None
    estimated = False
    if dates is not None and hasattr(dates, "index"):
        today = dt.date.today()
        try:
            for stamp in dates.index:
                moment = _as_datetime(stamp)
                if moment is None:
                    continue
                day = moment.date()
                if day >= today:
                    if upcoming is None or day < upcoming:
                        upcoming = day
                        upcoming_time = moment.hour
                else:
                    past.append(moment)
        except Exception:
            pass
    today = dt.date.today()
    if upcoming is None:
        upcoming = _calendar_earnings_date(ticker, today)
        estimated = upcoming is not None
    if upcoming is not None:
        merged["_earnings_date"] = upcoming.isoformat()
        merged["_earnings_date_estimated"] = estimated
        merged["_earnings_timing"] = _timing(upcoming_time)
    if past:
        merged["_last_report_date"] = max(moment.date() for moment in past).isoformat()
    cik = merged.get("cik")
    if isinstance(cik, (int, str)) and str(cik).strip():
        merged["_last_report_url"] = (
            "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany"
            f"&CIK={str(cik).strip()}&type=10-Q&dateb=&owner=include&count=40"
        )
    price = _num(merged, "currentPrice") or _num(merged, "regularMarketPrice")
    if upcoming is not None and price is not None:
        implied = _implied_move(ticker, upcoming, price)
        if implied is not None:
            merged["_earnings_implied_move"] = implied
    if past:
        average = _avg_post_earnings_move(ticker, past)
        if average is not None:
            merged["_earnings_avg_move"] = average


def _merge_primary_document(ticker: object, merged: dict[str, object]) -> None:
    """Enlace al documento primario del ultimo reporte (8-K/6-K/10-Q/10-K).

    ``Ticker.sec_filings`` es una lista de dicts, mas nueva primero. Se prefiere el
    exhibit de resultados (EX-99.1/EX-99.2) de un 8-K; si no hay, el 10-Q/10-K.
    """
    try:
        filings = ticker.sec_filings  # type: ignore[attr-defined]
    except Exception:
        return
    if not isinstance(filings, list):
        return
    for filing in filings:
        if not isinstance(filing, dict):
            continue
        form = str(filing.get("type") or "").strip().upper()
        exhibits = filing.get("exhibits")
        url: object = None
        if form in {"10-Q", "10-K"} and isinstance(exhibits, dict):
            url = exhibits.get(form)
        elif form in {"8-K", "6-K"} and isinstance(exhibits, dict):
            for key in ("EX-99.1", "EX-99.2", "99.1", "99.2"):
                if exhibits.get(key):
                    url = exhibits[key]
                    break
        if not isinstance(url, str) or not url:
            continue
        merged["_last_report_url"] = url
        merged["_last_report_type"] = form
        merged.setdefault("_last_report_date", str(filing.get("date") or ""))
        return


def _as_datetime(value: object) -> dt.datetime | None:
    if isinstance(value, dt.datetime):
        return value
    if isinstance(value, dt.date):
        return dt.datetime(value.year, value.month, value.day)
    try:
        parsed = dt.datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed


def _timing(hour: int | None) -> str:
    if hour is None:
        return "sin confirmar"
    if hour < 9:
        return "antes del cierre"
    if hour >= 16:
        return "despues del cierre"
    return "sin confirmar"


def _implied_move(ticker: object, earnings: dt.date, price: float) -> float | None:
    """Straddle ATM de la primera expiracion posterior al reporte, sobre el precio."""
    try:
        expirations = list(ticker.options)  # type: ignore[attr-defined]
    except Exception:
        return None
    if not expirations or price <= 0.0:
        return None
    chosen = None
    for expiry in expirations:
        try:
            day = dt.date.fromisoformat(str(expiry))
        except ValueError:
            continue
        if day >= earnings:
            chosen = str(expiry)
            break
    if chosen is None:
        return None
    try:
        chain = ticker.option_chain(chosen)  # type: ignore[attr-defined]
    except Exception:
        return None
    call = _atm_price(chain.calls, price)
    put = _atm_price(chain.puts, price)
    if call is None or put is None:
        return None
    return (call + put) / price


def _atm_price(frame: Any, price: float) -> float | None:
    if frame is None or not hasattr(frame, "empty") or frame.empty:
        return None
    try:
        strikes = frame["strike"]
        row = frame.loc[(strikes - price).abs().idxmin()]
    except Exception:
        return None
    for key in ("lastPrice", "ask", "bid"):
        try:
            value = row[key]
        except Exception:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        number = float(value)
        if number > 0.0 and number == number:
            return number
    return None


def _avg_post_earnings_move(ticker: object, dates: Sequence[dt.datetime]) -> float | None:
    """Movimiento medio absoluto de la primera sesion *posterior* a cada reporte.

    Un reporte despues del cierre se mide en la sesion siguiente; uno antes del
    cierre, en la sesion del mismo dia. Comparar contra el cierre previo evita
    mezclar el movimiento de la víspera con el del reporte.
    """
    history: Any = None
    try:
        history = ticker.history(period="2y", interval="1d", auto_adjust=False)  # type: ignore[attr-defined]
    except Exception:
        pass
    if history is None or not hasattr(history, "index") or len(history) < 2:
        return None
    try:
        closes = [(_as_datetime(stamp), float(value)) for stamp, value in history["Close"].items()]
    except Exception:
        return None
    moves: list[float] = []
    for moment in sorted(dates):
        after_close = moment.hour >= 16
        target = moment.date()
        for index in range(1, len(closes)):
            stamp, close = closes[index]
            if stamp is None:
                continue
            day = stamp.date()
            if day < target or (after_close and day == target):
                continue
            previous = closes[index - 1][1]
            if previous > 0.0:
                moves.append(abs(close / previous - 1.0))
            break
    if not moves:
        return None
    return sum(moves[-8:]) / len(moves[-8:])


def _plus_one_year(frame: object, column: str) -> float | None:
    """Valor de la fila ``+1y`` en ``column`` (o la primera columna numerica).

    ``growth_estimates`` trae el crecimiento de EPS (``stockTrend``) y
    ``revenue_estimate`` el de ventas (``growth``). Son fuentes distintas: usar la
    misma para las dos duplicaria el EPS como si fuera revenue. Si el formato
    cambia, devuelve ``None``.
    """
    if frame is None or not hasattr(frame, "loc"):
        return None
    try:
        row = frame.loc["+1y"]
    except Exception:
        return None
    candidates: list[object] = []
    try:
        if column in row.index:
            candidates.append(row[column])
        candidates.extend(row)
    except Exception:
        return None
    for value in candidates:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        number = float(value)
        if number == number:  # descarta NaN
            return number
    return None


def _num(info: Mapping[str, object], key: str) -> float | None:
    value = info.get(key)
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        result = float(value)
    elif isinstance(value, str):
        try:
            result = float(value.strip())
        except ValueError:
            return None
    else:
        return None
    return result if result == result else None  # descarta NaN


def _int(info: Mapping[str, object], key: str) -> int | None:
    value = _num(info, key)
    return int(value) if value is not None else None


def _flag(info: Mapping[str, object], key: str) -> bool | None:
    value = info.get(key)
    return value if isinstance(value, bool) else None


def _floats(value: object) -> tuple[float, ...]:
    """Lista de numeros (sin NaN ni booleanos) -> tupla; cualquier otra forma, vacia."""
    if not isinstance(value, (list, tuple)):
        return ()
    out: list[float] = []
    for item in value:
        if isinstance(item, (int, float)) and not isinstance(item, bool) and item == item:
            out.append(float(item))
    return tuple(out)


def _text(info: Mapping[str, object], key: str) -> str | None:
    value = info.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _date(info: Mapping[str, object], key: str) -> dt.date | None:
    value = info.get(key)
    if isinstance(value, dt.date):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return dt.date.fromisoformat(value.strip())
        except ValueError:
            return None
    return None


def _debt_to_equity(info: Mapping[str, object]) -> float | None:
    """yfinance reporta el D/E **siempre** en porcentaje (145.7 = 1.457 veces).

    No se adivina por magnitud: un D/E bajo (p. ej. 5.2 = 5 %) quedaria leido como
    5.2 veces y la puerta ``max_debt_to_equity`` rechazaria a la empresa menos
    endeudada.
    """
    value = _num(info, "debtToEquity")
    return None if value is None else value / 100.0


def _dividend_yield(info: Mapping[str, object]) -> float | None:
    """Rendimiento por dividendo como fraccion.

    ``dividendYield`` cambio de unidad entre versiones de yfinance (hoy viene en
    %: 0.32 = 0.32 %), asi que se prefieren las fuentes sin ambiguedad:
    ``dividendRate / precio`` y ``trailingAnnualDividendYield`` (fraccion).
    """
    rate = _num(info, "dividendRate")
    price = _num(info, "currentPrice") or _num(info, "regularMarketPrice")
    if rate is not None and price is not None and price > 0.0:
        return rate / price
    trailing = _num(info, "trailingAnnualDividendYield")
    if trailing is not None:
        return trailing
    value = _num(info, "dividendYield")
    return None if value is None else value / 100.0


def fundamentals_from_info(
    ticker: str,
    info: Mapping[str, object],
    *,
    today: dt.date,
    source: str = "yfinance:info",
) -> Fundamentals:
    """``Ticker.info`` normalizado a ``Fundamentals``. Nunca levanta por un campo."""
    summary = _text(info, "longBusinessSummary")
    if summary is not None and len(summary) > SUMMARY_CHARS:
        summary = summary[:SUMMARY_CHARS].rstrip() + "..."
    profit_margin = _num(info, "profitMargins")
    operating_margin = _num(info, "operatingMargins")
    margin_basis = _text(info, "_margin_basis")
    extraordinary = _flag(info, "_extraordinary_item")
    # Sin estado trimestral se usa el margen TTM de ``info``: se declara la base
    # y se conserva el aviso de partidas no operativas.
    if margin_basis is None and operating_margin is not None:
        margin_basis = "TTM (info)"
    if extraordinary is None and profit_margin is not None and operating_margin is not None:
        extraordinary = profit_margin > operating_margin + 1e-3
    return Fundamentals(
        ticker=ticker.strip().upper(),
        as_of=today,
        source=source,
        sector=_text(info, "sector"),
        industry=_text(info, "industry"),
        business_summary=summary,
        market_cap=_num(info, "marketCap"),
        trailing_pe=_num(info, "trailingPE"),
        forward_pe=_num(info, "forwardPE"),
        price_to_book=_num(info, "priceToBook"),
        peg_ratio=_num(info, "pegRatio"),
        return_on_equity=_num(info, "returnOnEquity"),
        profit_margin=profit_margin,
        operating_margin=operating_margin,
        revenue_growth=_num(info, "revenueGrowth"),
        earnings_growth=_num(info, "earningsGrowth"),
        debt_to_equity=_debt_to_equity(info),
        current_ratio=_num(info, "currentRatio"),
        dividend_yield=_dividend_yield(info),
        beta=_num(info, "beta"),
        eps_trailing=_num(info, "trailingEps"),
        eps_forward=_num(info, "forwardEps"),
        target_mean=_num(info, "targetMeanPrice"),
        target_high=_num(info, "targetHighPrice"),
        target_low=_num(info, "targetLowPrice"),
        recommendation_mean=_num(info, "recommendationMean"),
        recommendation_key=_text(info, "recommendationKey"),
        analyst_count=_int(info, "numberOfAnalystOpinions"),
        eps_growth_estimate=_num(info, "_eps_growth_next_year"),
        revenue_growth_estimate=_num(info, "_revenue_growth_next_year"),
        fcf_last_q=_num(info, "_fcf_last_q"),
        fcf_prior_q=_num(info, "_fcf_prior_q"),
        net_debt=_num(info, "_net_debt"),
        net_debt_change=_num(info, "_net_debt_change"),
        f_score=_int(info, "_f_score"),
        f_score_computed=_int(info, "_f_score_n"),
        oper_margin_yoy_bps=_num(info, "_oper_margin_yoy_bps"),
        oper_margin_last_q=_num(info, "_oper_margin_last_q"),
        profit_margin_last_q=_num(info, "_profit_margin_last_q"),
        margin_basis=margin_basis,
        margin_period=_date(info, "_margin_period"),
        extraordinary_item=extraordinary,
        revenue_last_q=_num(info, "_revenue_last_q"),
        revenue_prior_q=_num(info, "_revenue_prior_q"),
        revenue_growth_last_q=_num(info, "_revenue_growth_last_q"),
        revenue_growth_prior_q=_num(info, "_revenue_growth_prior_q"),
        revenue_growth_quarters=_floats(info.get("_revenue_growth_quarters")),
        guidance_vs_consensus=_num(info, "_guidance_vs_consensus"),
        eps_consensus_60d_ago=_num(info, "_eps_consensus_60d_ago"),
        eps_revision_60d=_num(info, "_eps_revision_60d"),
        eps_revision_current_year=_num(info, "_eps_revision_cy"),
        eps_revision_next_year=_num(info, "_eps_revision_ny"),
        eps_revision_current_year_suppressed=_flag(info, "_eps_revision_cy_suppressed") is True,
        eps_revision_next_year_suppressed=_flag(info, "_eps_revision_ny_suppressed") is True,
        analyst_up_60d=_int(info, "_analyst_up_60d"),
        analyst_down_60d=_int(info, "_analyst_down_60d"),
        analyst_up_30d=_int(info, "_analyst_up_30d"),
        analyst_down_30d=_int(info, "_analyst_down_30d"),
        last_analyst_revision_date=_date(info, "_last_analyst_revision_date"),
        target_change_60d=_num(info, "_target_change_60d"),
        target_as_of=_date(info, "_last_analyst_revision_date") or today,
        earnings_timing=_text(info, "_earnings_timing"),
        earnings_date=_date(info, "_earnings_date"),
        earnings_date_estimated=_flag(info, "_earnings_date_estimated") is True,
        earnings_implied_move=_num(info, "_earnings_implied_move"),
        earnings_avg_move=_num(info, "_earnings_avg_move"),
        last_report_date=_date(info, "_last_report_date"),
        last_report_type=_text(info, "_last_report_type"),
        last_report_url=_text(info, "_last_report_url"),
    )


def _payload(f: Fundamentals, history: Sequence[Mapping[str, object]] = ()) -> dict[str, object]:
    return {
        "schema": CACHE_SCHEMA,
        "_target_history": list(history),
        "ticker": f.ticker,
        "as_of": f.as_of.isoformat(),
        "source": f.source,
        "sector": f.sector,
        "industry": f.industry,
        "business_summary": f.business_summary,
        "market_cap": f.market_cap,
        "trailing_pe": f.trailing_pe,
        "forward_pe": f.forward_pe,
        "price_to_book": f.price_to_book,
        "peg_ratio": f.peg_ratio,
        "return_on_equity": f.return_on_equity,
        "profit_margin": f.profit_margin,
        "operating_margin": f.operating_margin,
        "revenue_growth": f.revenue_growth,
        "earnings_growth": f.earnings_growth,
        "debt_to_equity": f.debt_to_equity,
        "current_ratio": f.current_ratio,
        "dividend_yield": f.dividend_yield,
        "beta": f.beta,
        "eps_trailing": f.eps_trailing,
        "eps_forward": f.eps_forward,
        "target_mean": f.target_mean,
        "target_high": f.target_high,
        "target_low": f.target_low,
        "recommendation_mean": f.recommendation_mean,
        "recommendation_key": f.recommendation_key,
        "analyst_count": f.analyst_count,
        "eps_growth_estimate": f.eps_growth_estimate,
        "revenue_growth_estimate": f.revenue_growth_estimate,
        "fcf_last_q": f.fcf_last_q,
        "fcf_prior_q": f.fcf_prior_q,
        "net_debt": f.net_debt,
        "net_debt_change": f.net_debt_change,
        "f_score": f.f_score,
        "f_score_computed": f.f_score_computed,
        "oper_margin_yoy_bps": f.oper_margin_yoy_bps,
        "oper_margin_last_q": f.oper_margin_last_q,
        "profit_margin_last_q": f.profit_margin_last_q,
        "margin_basis": f.margin_basis,
        "margin_period": f.margin_period.isoformat() if f.margin_period else None,
        "extraordinary_item": f.extraordinary_item,
        "revenue_last_q": f.revenue_last_q,
        "revenue_prior_q": f.revenue_prior_q,
        "revenue_growth_last_q": f.revenue_growth_last_q,
        "revenue_growth_prior_q": f.revenue_growth_prior_q,
        "revenue_growth_quarters": list(f.revenue_growth_quarters),
        "guidance_vs_consensus": f.guidance_vs_consensus,
        "eps_consensus_60d_ago": f.eps_consensus_60d_ago,
        "eps_revision_60d": f.eps_revision_60d,
        "eps_revision_current_year": f.eps_revision_current_year,
        "eps_revision_next_year": f.eps_revision_next_year,
        "eps_revision_current_year_suppressed": f.eps_revision_current_year_suppressed,
        "eps_revision_next_year_suppressed": f.eps_revision_next_year_suppressed,
        "analyst_up_60d": f.analyst_up_60d,
        "analyst_down_60d": f.analyst_down_60d,
        "analyst_up_30d": f.analyst_up_30d,
        "analyst_down_30d": f.analyst_down_30d,
        "last_analyst_revision_date": (
            f.last_analyst_revision_date.isoformat() if f.last_analyst_revision_date else None
        ),
        "target_change_60d": f.target_change_60d,
        "target_as_of": f.target_as_of.isoformat() if f.target_as_of else None,
        "earnings_timing": f.earnings_timing,
        "earnings_date": f.earnings_date.isoformat() if f.earnings_date else None,
        "earnings_date_estimated": f.earnings_date_estimated,
        "earnings_implied_move": f.earnings_implied_move,
        "earnings_avg_move": f.earnings_avg_move,
        "last_report_date": f.last_report_date.isoformat() if f.last_report_date else None,
        "last_report_type": f.last_report_type,
        "last_report_url": f.last_report_url,
    }


def _whole(value: object) -> int | None:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _from_payload(data: Mapping[str, object]) -> Fundamentals:
    def num(key: str) -> float | None:
        value = data.get(key)
        return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None

    def text(key: str) -> str | None:
        value = data.get(key)
        return value if isinstance(value, str) and value else None

    raw_date = data.get("as_of")
    as_of = dt.date.fromisoformat(raw_date) if isinstance(raw_date, str) else dt.date.today()

    def integer(key: str) -> int | None:
        value = data.get(key)
        return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None

    def day(key: str) -> dt.date | None:
        value = data.get(key)
        if isinstance(value, str) and value:
            try:
                return dt.date.fromisoformat(value)
            except ValueError:
                return None
        return None

    count = data.get("analyst_count")
    return Fundamentals(
        ticker=str(data.get("ticker", "")),
        as_of=as_of,
        source=str(data.get("source", "cache")),
        sector=text("sector"),
        industry=text("industry"),
        business_summary=text("business_summary"),
        market_cap=num("market_cap"),
        trailing_pe=num("trailing_pe"),
        forward_pe=num("forward_pe"),
        price_to_book=num("price_to_book"),
        peg_ratio=num("peg_ratio"),
        return_on_equity=num("return_on_equity"),
        profit_margin=num("profit_margin"),
        operating_margin=num("operating_margin"),
        revenue_growth=num("revenue_growth"),
        earnings_growth=num("earnings_growth"),
        debt_to_equity=num("debt_to_equity"),
        current_ratio=num("current_ratio"),
        dividend_yield=num("dividend_yield"),
        beta=num("beta"),
        eps_trailing=num("eps_trailing"),
        eps_forward=num("eps_forward"),
        target_mean=num("target_mean"),
        target_high=num("target_high"),
        target_low=num("target_low"),
        recommendation_mean=num("recommendation_mean"),
        recommendation_key=text("recommendation_key"),
        analyst_count=int(count) if isinstance(count, (int, float)) and not isinstance(count, bool) else None,
        eps_growth_estimate=num("eps_growth_estimate"),
        revenue_growth_estimate=num("revenue_growth_estimate"),
        fcf_last_q=num("fcf_last_q"),
        fcf_prior_q=num("fcf_prior_q"),
        net_debt=num("net_debt"),
        net_debt_change=num("net_debt_change"),
        f_score=_whole(data.get("f_score")),
        f_score_computed=_whole(data.get("f_score_computed")),
        oper_margin_yoy_bps=num("oper_margin_yoy_bps"),
        oper_margin_last_q=num("oper_margin_last_q"),
        profit_margin_last_q=num("profit_margin_last_q"),
        margin_basis=text("margin_basis"),
        margin_period=day("margin_period"),
        extraordinary_item=_flag(data, "extraordinary_item"),
        revenue_last_q=num("revenue_last_q"),
        revenue_prior_q=num("revenue_prior_q"),
        revenue_growth_last_q=num("revenue_growth_last_q"),
        revenue_growth_prior_q=num("revenue_growth_prior_q"),
        revenue_growth_quarters=_floats(data.get("revenue_growth_quarters")),
        guidance_vs_consensus=num("guidance_vs_consensus"),
        eps_consensus_60d_ago=num("eps_consensus_60d_ago"),
        eps_revision_60d=num("eps_revision_60d"),
        eps_revision_current_year=num("eps_revision_current_year"),
        eps_revision_next_year=num("eps_revision_next_year"),
        eps_revision_current_year_suppressed=_flag(data, "eps_revision_current_year_suppressed") is True,
        eps_revision_next_year_suppressed=_flag(data, "eps_revision_next_year_suppressed") is True,
        analyst_up_60d=integer("analyst_up_60d"),
        analyst_down_60d=integer("analyst_down_60d"),
        analyst_up_30d=integer("analyst_up_30d"),
        analyst_down_30d=integer("analyst_down_30d"),
        last_analyst_revision_date=day("last_analyst_revision_date"),
        target_change_60d=num("target_change_60d"),
        target_as_of=day("target_as_of"),
        earnings_timing=text("earnings_timing"),
        earnings_date=day("earnings_date"),
        earnings_date_estimated=_flag(data, "earnings_date_estimated") is True,
        earnings_implied_move=num("earnings_implied_move"),
        earnings_avg_move=num("earnings_avg_move"),
        last_report_date=day("last_report_date"),
        last_report_type=text("last_report_type"),
        last_report_url=text("last_report_url"),
    )


def _merge_target_history(
    previous: Mapping[str, object] | None,
    f: Fundamentals,
    day: dt.date,
) -> list[dict[str, object]]:
    """Historial diario del objetivo medio para poder medir su cambio a 60 dias.

    yfinance solo publica el objetivo **actual**, asi que el cambio a 60 dias se
    reconstruye con lo que este cache fue guardando. Sin 60 dias de historia el
    campo queda ausente: se declara, no se inventa.
    """
    entries: dict[str, float] = {}
    raw = previous.get("_target_history") if isinstance(previous, Mapping) else None
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, Mapping):
                continue
            value = item.get("target")
            stamp = item.get("date")
            if isinstance(stamp, str) and isinstance(value, (int, float)) and not isinstance(value, bool):
                entries[stamp] = float(value)
    if f.target_mean is not None and f.target_mean > 0.0:
        entries[day.isoformat()] = f.target_mean
    ordered = sorted(entries.items())[-120:]
    return [{"date": stamp, "target": value} for stamp, value in ordered]


def _target_change_60d(history: Sequence[Mapping[str, object]], day: dt.date) -> float | None:
    """Objetivo de hace >= 60 dias mas cercano, contra el actual (fraccion)."""
    if not history:
        return None
    cutoff = day - dt.timedelta(days=60)
    current = history[-1]
    current_value = current.get("target")
    current_day = current.get("date")
    if not isinstance(current_value, (int, float)) or not isinstance(current_day, str):
        return None
    if current_day != day.isoformat():
        return None
    old_value: float | None = None
    old_day = ""
    for item in history:
        stamp = item.get("date")
        value = item.get("target")
        if not isinstance(stamp, str) or not isinstance(value, (int, float)):
            continue
        try:
            moment = dt.date.fromisoformat(stamp)
        except ValueError:
            continue
        if moment <= cutoff and stamp > old_day:
            old_day = stamp
            old_value = float(value)
    if old_value is None or old_value <= 0.0:
        return None
    return float(current_value) / old_value - 1.0


@dataclass(frozen=True, slots=True)
class _Cached:
    as_of: dt.date
    payload: Mapping[str, object]

    @property
    def current_schema(self) -> bool:
        return self.payload.get("schema") == CACHE_SCHEMA


class YahooFundamentalsProvider:
    """Fundamentales con cache JSON por dia.

    ``offline=True`` no toca la red: devuelve lo cacheado (aunque este vencido) o
    un ``Fundamentals.missing`` declarado. Nunca levanta por un ticker sin datos.
    """

    def __init__(
        self,
        cache_dir: Path,
        *,
        fetcher: InfoFetcher = yfinance_info,
        ttl_days: int = DEFAULT_TTL_DAYS,
        offline: bool = False,
    ) -> None:
        self.cache_dir = cache_dir
        self._fetcher = fetcher
        self._ttl_days = max(0, ttl_days)
        self._offline = offline

    def _path(self, ticker: str) -> Path:
        safe = ticker.strip().upper().replace("/", "_").replace("\\", "_").replace("..", "_")
        return self.cache_dir / f"{safe}.json"

    def _read(self, ticker: str) -> _Cached | None:
        path = self._path(ticker)
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict) or "as_of" not in data:
            return None
        try:
            as_of = dt.date.fromisoformat(str(data["as_of"]))
        except ValueError:
            return None
        return _Cached(as_of=as_of, payload=data)

    def _write(self, f: Fundamentals, history: Sequence[Mapping[str, object]] = ()) -> None:
        path = self._path(f.ticker)
        path.parent.mkdir(parents=True, exist_ok=True)
        handle_fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
        tmp = Path(tmp_name)
        try:
            with os.fdopen(handle_fd, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(_payload(f, history), ensure_ascii=False))
            os.replace(tmp, path)
        except OSError:
            tmp.unlink(missing_ok=True)
            raise

    def get(self, ticker: str, *, today: dt.date | None = None) -> Fundamentals:
        """Fundamentales del ticker, frescos si se puede, cacheados si no."""
        day = today or dt.date.today()
        cached = self._read(ticker)
        if cached is not None and cached.current_schema and (day - cached.as_of).days <= self._ttl_days:
            return _from_payload(cached.payload)
        if self._offline:
            if cached is not None:
                return _from_payload(cached.payload)
            return Fundamentals.missing(ticker, "offline:sin cache", day)
        raw = self._fetcher(_yahoo_symbol(ticker))
        if raw is None:
            if cached is not None:
                return _from_payload(cached.payload)
            return Fundamentals.missing(ticker, "yfinance:info", day)
        parsed = fundamentals_from_info(ticker, raw, today=day)
        if parsed.is_empty and cached is not None:
            return _from_payload(cached.payload)
        history = _merge_target_history(cached.payload if cached is not None else None, parsed, day)
        change = _target_change_60d(history, day)
        if change is not None:
            parsed = replace(parsed, target_change_60d=change)
        self._write(parsed, history)
        return parsed

    def get_many(self, tickers: Sequence[str], *, today: dt.date | None = None) -> dict[str, Fundamentals]:
        """Fundamentales de varios tickers, en el orden pedido y sin duplicar."""
        out: dict[str, Fundamentals] = {}
        for ticker in dict.fromkeys(tickers):
            out[ticker] = self.get(ticker, today=today)
        return out


#: Yahoo usa guion donde el ticker usa punto (mismo mapeo que ``data_yahoo``).
def _yahoo_symbol(ticker: str) -> str:
    symbol = ticker.strip().upper()
    if symbol.endswith(".US"):
        symbol = symbol[: -len(".US")]
    return {"BRK.B": "BRK-B", "BF.B": "BF-B"}.get(symbol, symbol)


def provider_from_project(cache_dir: Path | None = None, *, offline: bool = False) -> YahooFundamentalsProvider:
    """Provider con la cache en ``cache/fundamentals`` al lado del proyecto."""
    root = cache_dir if cache_dir is not None else Path(__file__).resolve().parents[3] / "cache" / "fundamentals"
    return YahooFundamentalsProvider(root, offline=offline)
