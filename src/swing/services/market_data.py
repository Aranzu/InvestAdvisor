"""Construccion de la capa de datos de mercado (velas, FX, earnings, RS).

Helpers puros de I/O que el CLI usa para armar el ``MarketSnapshot``: no deciden
nada de estrategia, solo resuelven que datos hay y declaran lo que falta.
"""

from __future__ import annotations

import datetime as dt

import typer

from swing.adapters.cache import CacheError
from swing.adapters.data_yahoo import MARKET_TZ, DataUnavailable, YahooDataProvider
from swing.core.analysis import Quote
from swing.core.calendar import SessionCalendar
from swing.core.dataquality import find_unadjusted_jumps
from swing.core.market import PriceSeries
from swing.core.reconcile import BrokerLot
from swing.core.report import PositionReport

#: Cierre de NYSE en hora local del mercado. El nucleo razona en sesiones, no en
#: horas de reloj; solo el diagnostico y la frescura necesitan esta hora.
MARKET_CLOSE_NY = dt.time(hour=16)


def last_closed_session(calendar: SessionCalendar, min_minutes_after_close: int) -> dt.date:
    """Ultima sesion con vela cerrada (D3): la de hoy solo cuenta tras el margen.

    Antes del cierre, la fila de hoy que entrega el proveedor es una vela en curso
    (a veces incoherente); usarla seria operar con datos incompletos.
    """
    now_ny = dt.datetime.now(tz=MARKET_TZ)
    last = calendar.last_session_on_or_before(now_ny.date())
    closed = dt.datetime.combine(last, MARKET_CLOSE_NY, tzinfo=MARKET_TZ) + dt.timedelta(
        minutes=min_minutes_after_close
    )
    return last if now_ny >= closed else calendar.previous_session(last)


def collect_series(
    provider: YahooDataProvider,
    tickers: list[str],
    start: dt.date,
    end: dt.date,
) -> tuple[dict[str, PriceSeries], list[str]]:
    """Velas de ``[start, end]`` por ticker; los que no llegan se declaran, no se inventan.

    Una vela contraditoria (p. ej. ``open > high``) de un ticker no puede tumbar el
    barrido de todo el universo: ese ticker queda sin datos y se declara.
    """
    series: dict[str, PriceSeries] = {}
    missing: list[str] = []
    for ticker in tickers:
        try:
            bars = provider.daily_bars(ticker, start=start, end=end)
        except (CacheError, DataUnavailable, ValueError):
            missing.append(ticker)
            continue
        if bars.is_empty:
            missing.append(ticker)
            continue
        series[ticker] = bars
    return series, missing


def warn_jumps(series: dict[str, PriceSeries]) -> None:
    """Avisa de saltos no ajustados (posibles splits) sin reescalar en vivo (D6).

    En vivo el motor **no puede decidir solo** si un salto grande es un split o una
    caida real, asi que no reescala en silencio: lo declara para que el usuario revise.
    Los indices (``^VIX``, ``^TNX``, ``^DXY``) quedan fuera: no pueden partirse, y un
    VIX que sube mas de 30 % en un dia es una subida real, no un split.
    """
    jumped = sorted(
        name
        for name, bars in series.items()
        if not name.startswith("^") and find_unadjusted_jumps(bars)
    )
    if jumped:
        typer.secho(
            f"  aviso: salto no ajustado (posible split) en {', '.join(jumped)}; "
            "en vivo no se reescala, revisar antes de operar (DATA_GAP)",
            fg=typer.colors.YELLOW,
        )


def live_fx(provider: YahooDataProvider, today: dt.date) -> float | None:
    try:
        return provider.fx_usdclp(today)
    except (CacheError, DataUnavailable, ValueError):
        return None


def live_quotes(model: PositionReport, provider: YahooDataProvider, today: dt.date) -> dict[str, Quote]:
    """Precio de cierre y earnings por ticker de hoy. Lo que falte queda sin cotizar:
    el analisis cae al precio del reporte y lo declara (fail-closed, no inventa).

    El ``ValueError`` de una vela en curso contraditoria (``open`` fuera del rango,
    tipico antes del cierre: ver ``last_closed_session``) se trata como un dato
    faltante: ese ticker queda sin cotizar en vivo, no tumba el barrido del resto.
    """
    quotes: dict[str, Quote] = {}
    start = today - dt.timedelta(days=45)
    for ticker in sorted({lot.ticker for lot in model.lots}):
        try:
            series = provider.daily_bars(ticker, start=start, end=today)
            price = series.last_close
            earnings = provider.earnings(ticker)
        except (CacheError, DataUnavailable, ValueError):
            continue
        if price is None:
            continue
        quotes[ticker] = Quote(
            price_usd=price,
            price_source="provider",
            earnings_date=earnings.date if not earnings.is_missing else None,
            earnings_known=not earnings.is_missing,
            earnings_confirmed=earnings.confirmed,
        )
    return quotes


def implied_fx(model: PositionReport) -> float:
    """FX implicito del reporte: valor CLP / (volumen x precio USD), o 1.0 sin dato."""
    numerator = sum(lot.value_clp for lot in model.lots if lot.price_usd)
    denominator = sum(lot.volume * lot.price_usd for lot in model.lots if lot.price_usd)
    return numerator / denominator if denominator > 0.0 else 1.0


def broker_lots(model: PositionReport) -> list[BrokerLot]:
    """Lotes del reporte en la forma que consume ``reconcile_positions``."""
    return [
        BrokerLot(
            id=lot.position_id,
            ticker=lot.ticker,
            volume=lot.volume,
            open_price=lot.open_price_usd,
            open_time=dt.datetime.combine(lot.open_date, dt.time.min),
            stop_loss=lot.stop_usd,
        )
        for lot in model.lots
    ]
