"""Indicadores tecnicos (spec v3.1 seccion 16.1).

Sin I/O: reciben series y devuelven series. La entrada y la salida comparten
indice y el prefijo es NaN hasta que hay historia suficiente, de modo que un
indicador immature nunca se puede confundir con un valor calculado.

Convenciones fijadas aca para que los tests sean calculables a mano:

- ``TR_0 = H_0 - L_0`` y ``TR_t = max(H_t - L_t, |H_t - C_(t-1)|, |L_t - C_(t-1)|)``
- Suavizado de Wilder de periodo ``n``: la semilla es la media de las ``n``
  primeras observaciones y despues ``X_t = (X_(t-1) * (n-1) + x_t) / n``.
  Para el ATR la semilla queda en el indice ``n - 1``; para el RSI, que pierde
  la primera observacion por ser una diferencia, queda en el indice ``n``.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

__all__ = [
    "atr",
    "highest",
    "lowest",
    "rsi",
    "sma",
    "true_range",
    "wilder_smooth",
]


def _as_float(values: pd.Series, name: str) -> np.ndarray:
    if not isinstance(values, pd.Series):
        raise TypeError(f"{name} debe ser una pd.Series, recibio {type(values).__name__}")
    return values.to_numpy(dtype="float64")


def _check_aligned(**series: pd.Series) -> None:
    indexes = {name: set(series.index) for name, series in series.items() if isinstance(series, pd.Series)}
    if len(indexes) > 1:
        first_name, first = next(iter(indexes.items()))
        for name, index in list(indexes.items())[1:]:
            if index != first:
                raise ValueError(f"las series '{first_name}' y '{name}' no comparten el mismo indice")


def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    """True Range. La primera barra usa ``H - L`` porque no hay cierre previo."""
    _check_aligned(high=high, low=low, close=close)
    high_v = _as_float(high, "high")
    low_v = _as_float(low, "low")
    close_v = _as_float(close, "close")
    if not (len(high_v) == len(low_v) == len(close_v)):
        raise ValueError("high, low y close deben tener la misma cantidad de barras")

    prev_close = np.empty_like(close_v)
    prev_close[0] = np.nan
    prev_close[1:] = close_v[:-1]

    hl = high_v - low_v
    hc = np.abs(high_v - prev_close)
    lc = np.abs(low_v - prev_close)
    tr = np.fmax(np.fmax(hl, hc), lc)
    tr[0] = hl[0]
    return pd.Series(tr, index=close.index, name="true_range")


def wilder_smooth(series: pd.Series, period: int) -> pd.Series:
    """Suavizado de Wilder: semilla = media de las ``period`` primeras, luego
    ``X_t = (X_(t-1) * (period - 1) + x_t) / period``.

    Escribe NaN en las ``period - 1`` primeras posiciones.
    """
    if period < 1:
        raise ValueError(f"period debe ser >= 1; recibio {period}")
    values = _as_float(series, "series")
    n = len(values)
    out = np.full(n, np.nan, dtype="float64")
    # Menos observaciones que el periodo no alcanzan ni para la semilla: todo NaN
    # en vez de un numero inventado.
    if n < period:
        return pd.Series(out, index=series.index, name=series.name)

    seed = values[:period]
    if np.isnan(seed).any():
        raise ValueError(
            f"las primeras {period} observaciones contienen NaN: la semilla de Wilder no se puede calcular"
        )
    acc = float(seed.sum())
    out[period - 1] = acc / period
    for i in range(period, n):
        out[i] = (out[i - 1] * (period - 1) + values[i]) / period
    return pd.Series(out, index=series.index, name=series.name)


def atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    """ATR de Wilder. La semilla queda en el indice ``period - 1``."""
    return wilder_smooth(true_range(high, low, close), period)


def sma(series: pd.Series, period: int) -> pd.Series:
    """Media simple, NaN hasta tener ``period`` observaciones."""
    if period < 1:
        raise ValueError(f"period debe ser >= 1; recibio {period}")
    return series.rolling(window=period, min_periods=period).mean()


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """RSI de Wilder (suavizado, no el corte de Wilder de 1978).

    Conviene para el setup ``pullback`` porque el criterio es "el momentum se
    enfrio", no "el momentum esta en sobrecompra/extremos".
    """
    if period < 1:
        raise ValueError(f"period debe ser >= 1; recibio {period}")
    close_v = _as_float(close, "close")
    if len(close_v) == 0:
        return pd.Series([], dtype="float64", index=close.index, name="rsi")

    delta = np.diff(close_v)
    gain = np.clip(delta, 0.0, None)
    loss = np.clip(-delta, 0.0, None)

    # Sin el primer elemento (la diferencia contra la barra previa no existe):
    # la semilla del suavizado cae en el indice ``period`` de la serie original.
    idx = close.index[1:]
    avg_gain = wilder_smooth(pd.Series(gain, index=idx), period)
    avg_loss = wilder_smooth(pd.Series(loss, index=idx), period)

    gv = avg_gain.to_numpy(dtype="float64")
    lv = avg_loss.to_numpy(dtype="float64")
    rs = np.divide(gv, lv, out=np.full_like(gv, np.nan), where=lv > 0.0)
    values = 100.0 - 100.0 / (1.0 + rs)
    # Sin perdidas el RSI es 100 (sube-only); sin Variationes es 50 por convenio.
    values = np.where((lv == 0.0) & (gv > 0.0), 100.0, values)
    values = np.where((lv == 0.0) & (gv == 0.0), 50.0, values)
    values = np.where(np.isnan(gv) | np.isnan(lv), np.nan, values)

    out = np.full(len(close_v), np.nan, dtype="float64")
    out[1:] = values
    return pd.Series(out, index=close.index, name="rsi")


def highest(series: pd.Series, period: int) -> pd.Series:
    """Maximo rodante, NaN hasta tener ``period`` observaciones."""
    return series.rolling(window=period, min_periods=period).max()


def lowest(series: pd.Series, period: int) -> pd.Series:
    """Minimo rodante, NaN hasta tener ``period`` observaciones."""
    return series.rolling(window=period, min_periods=period).min()
