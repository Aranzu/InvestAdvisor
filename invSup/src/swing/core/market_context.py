"""Contexto de mercado determinista para el informe de estadisticas.

Toma las series ya disponibles (cache o proveedor) y publica, por benchmark, el nivel,
la variacion a un mes y la distancia a su SMA200. No decide nada ni toca red: el
ensamblado vive en ``services/stats``. Los simbolos son de Yahoo.

Es el complemento determinista del bloque macro de ``review`` (que ademas sintetiza
con busqueda web): aqui solo hay numeros reproducibles.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import pandas as pd

from swing.core.indicators import sma
from swing.core.market import PriceSeries

#: Sesiones de un mes bursatil (la misma ventana que usan las materias primas).
MONTH_SESSIONS = 21

#: Periodo de la media de referencia del contexto.
SMA_WINDOW = 200


@dataclass(frozen=True, slots=True)
class MarketSpec:
    """Un benchmark de contexto: simbolo de Yahoo y etiqueta legible."""

    symbol: str
    label: str


@dataclass(frozen=True, slots=True)
class MarketInstrument:
    """Un benchmark del dia: nivel, variacion a un mes y distancia a su SMA200."""

    symbol: str
    label: str
    price: float | None = None
    chg_1m_pct: float | None = None
    sma200: float | None = None
    vs_sma200_pct: float | None = None


#: Benchmarks del contexto, en el orden en que se muestran. El dolar/CLP no entra:
#: Yahoo publica velas incoherentes en ``CLP=X`` en ventanas largas (el motor solo pide
#: 10 dias); su nivel ya vive en la cabecera del informe (``fx``).
MARKET: tuple[MarketSpec, ...] = (
    MarketSpec("^VIX", "VIX (volatilidad)"),
    MarketSpec("SPY", "S&P 500 (SPY)"),
    MarketSpec("QQQ", "Nasdaq 100 (QQQ)"),
    MarketSpec("SOXX", "Semis (SOXX)"),
    MarketSpec("ECH", "Chile (ECH, proxy)"),
    MarketSpec("^TNX", "Tasa 10Y"),
    MarketSpec("DX-Y.NYB", "Dolar (DXY)"),
)


def instrument(spec: MarketSpec, series: PriceSeries | None) -> MarketInstrument:
    """Benchmark resuelto desde su serie; ``None`` en lo que falte (best effort)."""
    base = MarketInstrument(symbol=spec.symbol, label=spec.label)
    if series is None or series.is_empty:
        return base
    price = series.last_close
    if price is None:
        return base
    closes = series.closes()
    sma200 = _last(sma(closes, SMA_WINDOW))
    return MarketInstrument(
        symbol=spec.symbol,
        label=spec.label,
        price=price,
        chg_1m_pct=_change_pct(closes, MONTH_SESSIONS),
        sma200=sma200,
        vs_sma200_pct=None if sma200 is None or sma200 <= 0.0 else (price / sma200 - 1.0) * 100.0,
    )


def _change_pct(closes: pd.Series, sessions: int) -> float | None:
    """Variacion porcentual contra el cierre de hace ``sessions`` sesiones."""
    if len(closes) <= sessions:
        return None
    then = float(closes.iloc[-1 - sessions])
    now = float(closes.iloc[-1])
    return None if then <= 0.0 else (now / then - 1.0) * 100.0


def _last(series: pd.Series | None) -> float | None:
    if series is None or len(series) == 0:
        return None
    value = float(series.iloc[-1])
    return None if math.isnan(value) else value
