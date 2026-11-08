"""Curva de patrimonio ajustada por aportes (P9 del prompt v9).

Modulo **puro**: recibe las operaciones de caja, los lotes abiertos y cerrados y
las series de precios ya resueltas. Reconstruye el valor de la cuenta dia a dia y
lo convierte en **valor cuota** (retorno ponderado por tiempo), de modo que un
deposito no se confunda con rentabilidad.

La caida desde el maximo se mide sobre el valor cuota: es la cifra que dispara los
modos cautela (8 %) y defensivo (15 %).
"""

from __future__ import annotations

import bisect
import datetime as dt
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from swing.core.compliance import mode_from_drawdown
from swing.core.market import PriceSeries
from swing.core.models import ComplianceConfig
from swing.core.report import CashOp, ClosedRow, OpenLot

#: Tipos de ``Cash Operations`` que son aportes/retiros externos, no operaciones.
_EXTERNAL_FLOWS = frozenset({"Deposit", "Withdrawal", "Deposito", "Retiro"})


@dataclass(frozen=True, slots=True)
class EquityCurve:
    """Resumen de la curva ajustada por aportes (valor cuota base 100)."""

    max_unit_value: float
    current_unit_value: float
    drawdown_pct: float
    days_since_peak: int
    mode: str
    points: int
    max_equity_clp: float = 0.0
    max_equity_date: dt.date | None = None
    current_equity_clp: float = 0.0
    max_equity_adjusted_clp: float = 0.0
    missing_tickers: tuple[str, ...] = ()


def build_curve(
    *,
    cash_ops: Sequence[CashOp],
    closed: Sequence[ClosedRow],
    lots: Sequence[OpenLot],
    prices: Mapping[str, PriceSeries],
    fx_by_date: Mapping[dt.date, float] | None,
    fallback_fx: float,
    today: dt.date,
    cfg: ComplianceConfig,
) -> EquityCurve | None:
    """Reconstruye la curva; ``None`` si no hay aportes ni historia suficiente."""
    events = _cash_by_date(cash_ops)
    flows = _flow_by_date(cash_ops)
    if not events:
        return None
    start = min(events)
    if start > today:
        return None

    price_series = {ticker: _prices(series) for ticker, series in prices.items()}
    fx_series = _fx_series(fx_by_date or {}, fallback_fx)
    holdings = _holdings(closed=closed, lots=lots)

    # Se arranca en el primer aporte relevante: los depositos de 1 CLP de la
    # apertura de la cuenta no son capital y distorsionarian el valor cuota.
    start = next(
        (day for day in sorted(flows) if flows[day] >= 1_000.0),
        start,
    )
    peak = 100.0
    peak_date = start
    max_equity = 0.0
    max_equity_date = start
    current_value = 0.0
    unit_value = 100.0
    previous_value: float | None = None
    cash = 0.0
    day = start
    points = 0
    missing: set[str] = set()
    while day <= today:
        cash += events.get(day, 0.0)
        flow = flows.get(day, 0.0)
        holdings_value = 0.0
        for ticker, spans in holdings.items():
            volume = _active_volume(spans, day)
            if volume <= 0.0:
                continue
            price = _last_price(price_series.get(ticker), day)
            if price is None:
                missing.add(ticker)
                continue
            holdings_value += volume * price * _fx_at(fx_series, day, fallback_fx)
        value = cash + holdings_value

        if previous_value is not None and previous_value > 0.0:
            daily_return = (value - flow) / previous_value - 1.0
            if math.isfinite(daily_return) and daily_return > -1.0:
                unit_value *= 1.0 + daily_return
        if value > 0.0:
            points += 1
            current_value = value
            if value > max_equity:
                max_equity = value
                max_equity_date = day
            if unit_value > peak:
                peak = unit_value
                peak_date = day
        previous_value = value
        day += dt.timedelta(days=1)

    # Sin precio para una posicion abierta hoy la curva no es una caida: es una
    # serie incompleta. Devolver None evita publicar un -100 % que no ocurrio.
    for ticker, spans in holdings.items():
        if _active_volume(spans, today) > 0.0 and _last_price(price_series.get(ticker), today) is None:
            return None
    if points < 2 or peak <= 0.0:
        return None
    drawdown = (unit_value / peak - 1.0) * 100.0
    adjusted = current_value * peak / unit_value if unit_value > 0.0 else current_value
    return EquityCurve(
        max_unit_value=peak,
        current_unit_value=unit_value,
        drawdown_pct=drawdown,
        days_since_peak=(today - peak_date).days,
        mode=mode_from_drawdown(drawdown, cfg),
        points=points,
        max_equity_clp=max_equity,
        max_equity_date=max_equity_date,
        current_equity_clp=current_value,
        max_equity_adjusted_clp=adjusted,
        missing_tickers=tuple(sorted(missing)),
    )


def _cash_by_date(ops: Sequence[CashOp]) -> dict[dt.date, float]:
    out: dict[dt.date, float] = {}
    for op in ops:
        day = op.when.date()
        out[day] = out.get(day, 0.0) + op.amount_clp
    return out


def _flow_by_date(ops: Sequence[CashOp]) -> dict[dt.date, float]:
    out: dict[dt.date, float] = {}
    for op in ops:
        if op.kind not in _EXTERNAL_FLOWS:
            continue
        day = op.when.date()
        out[day] = out.get(day, 0.0) + op.amount_clp
    return out


def _holdings(*, closed: Sequence[ClosedRow], lots: Sequence[OpenLot]) -> dict[str, list[tuple[dt.date, dt.date | None, float]]]:
    out: dict[str, list[tuple[dt.date, dt.date | None, float]]] = {}
    for row in closed:
        out.setdefault(row.ticker, []).append((row.open_date, row.close_date, row.volume))
    for lot in lots:
        out.setdefault(lot.ticker, []).append((lot.open_date, None, lot.volume))
    return out


def _active_volume(spans: Sequence[tuple[dt.date, dt.date | None, float]], day: dt.date) -> float:
    total = 0.0
    for start, end, volume in spans:
        if start <= day and (end is None or day < end):
            total += volume
    return total


def _prices(series: PriceSeries) -> tuple[list[dt.date], list[float]]:
    dates: list[dt.date] = []
    closes: list[float] = []
    for bar in series.bars:
        dates.append(bar.date)
        closes.append(bar.close)
    return dates, closes


def _last_price(series: tuple[list[dt.date], list[float]] | None, day: dt.date) -> float | None:
    if series is None or not series[0]:
        return None
    dates, closes = series
    index = bisect.bisect_right(dates, day) - 1
    return closes[index] if index >= 0 else None


def _fx_series(rates: Mapping[dt.date, float], fallback: float) -> tuple[list[dt.date], list[float]]:
    dates = sorted(day for day, rate in rates.items() if rate > 0.0)
    return dates, [rates[day] for day in dates]


def _fx_at(series: tuple[list[dt.date], list[float]], day: dt.date, fallback: float) -> float:
    dates, rates = series
    if not dates:
        return fallback
    index = bisect.bisect_right(dates, day) - 1
    if index >= 0:
        return rates[index]
    return rates[0]
