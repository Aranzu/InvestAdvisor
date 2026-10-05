"""Tecnicos de una bolsa que finviz no cubre, calculados sobre las velas de Yahoo.

Por que no se usa finviz: no existe. ``finviz.com/quote.ashx?t=SQM-B`` devuelve
404; lo unico que hay es el ADR (``SQM``) en dolares, que es **otro
instrumento**: sus niveles, su ATR y sus 52 semanas estan en USD, y mezclarlos
con los precios en pesos de la Bolsa de Santiago produciria un bloque que se
contradice solo (el mismo defecto que se corrigio en QNST, documentado en el
README). El ADR no se usa.

Por que se calculan en vez de imprimirse ``N/D``: el Prompt Maestro saca el
soporte del SL de las SMAs y descuenta 3 ATR, asi que sin tecnicos la posicion no
recibe SL. Con las velas de Yahoo se pueden calcular las mismas magnitudes que
publica finviz, en la moneda de la bolsa.

**El detalle que obliga a hacerlo asi:** la serie diaria de los enlistados
locales de Yahoo no se actualiza. En ``SQM-B.SN`` la serie ``1d`` estaba
congelada en 65450 con volumen cero desde el 4 de septiembre, mientras la serie
``1h`` llegaba al 25 de septiembre con el cierre real (64.201) y volumen real.
Calcular sobre la serie congelada da ATR 0 y un RSI sin sentido, que es peor que
no publicar nada. Por eso las velas se piden de 1 hora y se agregan a diario
a mano, y por eso el resultado se valida antes de publicarse
(``compute_technicals``): si la serie viene congelada, el ATR es 0 y no se
publica, con la advertencia que corresponde.

Definiciones (las mismas que usa finviz, calculadas sobre el cierre diario):

- ``SMA20/50/200``: media simple del cierre de las ultimas 20/50/200 sesiones.
- ``ATR14``: media movil exponencial (Wilder, alpha = 1/14) del True Range, cuyo
  maximo de hoy es ``max(High - Low, |High - Close_prev|, |Low - Close_prev|)``.
- ``RSI14``: RSI de Wilder, con la misma suavizacion.
- ``52W High/Low``: maximo y minimo del rango de 252 sesiones.
- ``Volatilidad``: desviacion estandar de los retornos diarios de las ultimas 252
  sesiones; ``Volatilidad (mes)``, de las ultimas 21.
- ``Perf.``: variacion del cierre contra el de N sesiones atras (1s = 5, 1m = 21,
  3m = 63, 1a = 252). ``YTD`` se mide contra el primer cierre del ano.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime

import pandas as pd

from .finviz_source import FinvizData

logger = logging.getLogger(__name__)

# Ventanas, en sesiones. 21 ≈ un mes bursátil chileno (el mes calendario tiene
# mas sesiones por los feriados, por eso no se usan 30).
_PERF_WINDOWS = {"week": 5, "month": 21, "quarter": 63, "year": 252}
_YEAR_WINDOW = 252
_MONTH_WINDOW = 21
# Menos de esto y no hay SMA200: se prefiere N/D a un numero con 120 velas.
MIN_BARS = 200
ATR_PERIOD = 14
RSI_PERIOD = 14
CANDLE_WINDOW = 22
# Dias de calendario que puede tener la ultima vela antes de considerarla vieja.
# Un fin de semana largo o un feriado Chilean (18 de septiembre) se comen tres o
# cuatro, asi que el margen es holgado.
MAX_STALENESS_DAYS = 7
# Distancia maxima entre el precio de referencia y el ultimo cierre de la serie.
# Por encima de esto son dos precios distintos y el bloque se contradiria.
MAX_QUOTE_DIVERGENCE_PCT = 5.0
# Si las ultimas velas vinieron sin volumen, la serie esta congelada.
FROZEN_VOLUME_BARS = 5

# Como se rotula la linea de tecnicos cuando los datos no salen de finviz. Sin
# parentesis propios porque el reporte ya los mete: "Tecnicos (label, hora)".
SOURCE_LABEL = "Yahoo, calculo propio sobre velas 1h"


@dataclass
class LocalTechnicals(FinvizData):
    """Snapshot tecnico de una bolsa local, con la misma forma que el de finviz.

    Hereda de ``FinvizData`` a proposito: el bloque del reporte se arma con los
    mismos campos para todos los tickers, y lo que cambia es de donde salio cada
    numero, no el nombre del campo. Los que no se pueden calcular quedan en
    ``None`` y se anotan en ``warnings``; ``source_label`` dice de donde salieron
    los que si.
    """

    source_label: str = SOURCE_LABEL
    bars: int = 0
    last_bar: str = ""

    @property
    def is_usable(self) -> bool:
        """True si hay una serie diaria real y usable para el SL.

        El ATR es lo que el Prompt Maestro descuenta del soporte, asi que sin ATR
        no hay SL estructural. Ademas tiene que haber alguna SMA bajo el precio:
        sin ningun soporte el bloque no dice de donde sale el nivel.
        """
        return self.atr is not None and bool(self.sma_below_price)


def resample_daily(hourly: pd.DataFrame | None) -> pd.DataFrame:
    """Agrega velas de 1 hora a velas diarias.

    ``Open`` de la primera, ``High`` maximo, ``Low`` minimo, ``Close`` de la
    ultima y ``Volume`` sumando: es la sesion bursatil, no el dia calendario.
    Se descartan los dias sin velas (feriados) porque un close NaN contaminaria
    las medias.
    """
    if hourly is None or hourly.empty:
        return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])
    if not isinstance(hourly.index, pd.DatetimeIndex):
        logger.warning("El historial de velas no trae indice de fechas: no se pueden agregar a diario.")
        return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])
    columns = {c: agg for c, agg in (("Open", "first"), ("High", "max"), ("Low", "min"),
                                     ("Close", "last"), ("Volume", "sum")) if c in hourly.columns}
    if "Close" not in columns:
        return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])
    daily = hourly.resample("1D").agg(columns)
    return daily.dropna(subset=["Close"])


def _true_range(daily: pd.DataFrame) -> pd.Series:
    high, low, close = daily["High"], daily["Low"], daily["Close"]
    prev_close = close.shift(1)
    ranges = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1)
    return ranges.max(axis=1)


def _wilder(series: pd.Series, period: int) -> pd.Series:
    """Media movil de Wilder (``adjust=False``): el suavizado que usan ATR y RSI."""
    return series.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def _sma(close: pd.Series, period: int) -> float | None:
    if len(close) < period:
        return None
    return round(float(close.iloc[-period:].mean()), 4)


def _rsi(close: pd.Series, period: int = RSI_PERIOD) -> float | None:
    delta = close.diff()
    gain = _wilder(delta.clip(lower=0), period)
    loss = _wilder(-delta.clip(upper=0), period)
    if gain.empty or loss.empty:
        return None
    avg_gain, avg_loss = float(gain.iloc[-1]), float(loss.iloc[-1])
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return round(100 - 100 / (1 + rs), 4)


def _perf(close: pd.Series, sessions: int) -> float | None:
    if len(close) <= sessions:
        return None
    base = float(close.iloc[-1 - sessions])
    return round((float(close.iloc[-1]) - base) / base * 100, 2) if base else None


def _perf_ytd(close: pd.Series, index: pd.DatetimeIndex) -> float | None:
    year = index[-1].year
    mask = index.year == year
    if not mask.any():
        return None
    base = float(close[mask].iloc[0])
    return round((float(close.iloc[-1]) - base) / base * 100, 2) if base else None


def _last_date(index: pd.DatetimeIndex) -> date:
    return index[-1].date()


def _staleness_days(index: pd.DatetimeIndex) -> int:
    return (datetime.now(index[-1].tz).date() - _last_date(index)).days


def _index_of(daily: pd.DataFrame) -> pd.DatetimeIndex | None:
    """Indice de fechas del historial, si es usable.

    yfinance devuelve siempre un ``DatetimeIndex``; si alguna vez no lo hiciera,
    mejor devolverlo asi y que ``compute_technicals`` lo declare, que indexar a
    ciegas y publicar un RSI sobre otra cosa.
    """
    index = daily.index
    return index if isinstance(index, pd.DatetimeIndex) else None


def compute_technicals(
    daily: pd.DataFrame,
    quote_price: float | None,
    *,
    ticker: str = "",
    symbol: str = "",
) -> LocalTechnicals:
    """Snapshot tecnico de una bolsa local a partir de sus velas diarias.

    ``quote_price`` es el precio de referencia del mercado (``Actual:``). Se usa
    para los porcentajes contra los que el Prompt Maestro decide el SL y el TP, y
    para detectar que la serie y la cotizacion no cuentan la misma historia.
    """
    data = LocalTechnicals(ticker=ticker)
    if daily is None or daily.empty:
        data.warnings.append("Yahoo no devolvio velas para este simbolo: no hay datos tecnicos")
        return data

    index = _index_of(daily)
    if index is None:
        data.warnings.append("el historial de velas no trae indice de fechas: no hay datos tecnicos")
        return data

    data.bars = len(daily)
    data.last_bar = _last_date(index).isoformat()
    close = daily["Close"]
    price = quote_price or float(close.iloc[-1])
    data.price = round(float(price), 4)

    stale_days = _staleness_days(index)
    if stale_days > MAX_STALENESS_DAYS:
        data.warnings.append(
            f"serie de velas desactualizada: la ultima vela es del {data.last_bar} "
            f"({stale_days} dias) → no se publican SMAs, ATR ni RSI"
        )
        return data

    volume = daily["Volume"] if "Volume" in daily.columns else None
    if volume is not None and len(volume) > FROZEN_VOLUME_BARS:
        tail = volume.iloc[-FROZEN_VOLUME_BARS:]
        if float(tail.max()) == 0:
            data.warnings.append(
                f"las ultimas {FROZEN_VOLUME_BARS} velas vinieron con volumen cero: la serie esta "
                "congelada → no se publica el ATR"
            )
            return data

    last_close = float(close.iloc[-1])
    if quote_price and abs(quote_price - last_close) / quote_price * 100 > MAX_QUOTE_DIVERGENCE_PCT:
        data.warnings.append(
            f"el precio de mercado ({quote_price:g}) y el ultimo cierre de la serie ({last_close:g}) "
            f"difieren en mas de {MAX_QUOTE_DIVERGENCE_PCT:g}% → revisar la moneda o el simbolo consultado"
        )

    if len(daily) < MIN_BARS:
        data.warnings.append(
            f"solo {len(daily)} velas diarias disponibles (se necesitan {MIN_BARS} para la SMA200) "
            "→ SMA200 y las medias de rendimiento quedan N/D"
        )
    data.sma20 = _sma(close, 20)
    data.sma50 = _sma(close, 50)
    data.sma200 = _sma(close, 200)

    if len(daily) > ATR_PERIOD and {"High", "Low"} <= set(daily.columns):
        atr = _wilder(_true_range(daily), ATR_PERIOD)
        value = float(atr.iloc[-1]) if not atr.empty else 0.0
        if value > 0:
            data.atr = round(value, 4)
        else:
            data.warnings.append(
                "el ATR14 resulto 0 (serie sin rango entre sesiones) → el Prompt Maestro no puede "
                "descuntar ATR del soporte para el SL"
            )
    data.rsi = _rsi(close)

    if len(daily) > _YEAR_WINDOW and {"High", "Low"} <= set(daily.columns):
        year = daily.iloc[-_YEAR_WINDOW:]
        data.w52_high = round(float(year["High"].max()), 4)
        data.w52_low = round(float(year["Low"].min()), 4)
    # Misma convencion que finviz: la distancia del extremo al precio del bloque,
    # y no al reves. Asi el maximo sale negativo o cero y el minimo positivo, que
    # es lo que hace el signo deducible sin leer la etiqueta. La version anterior
    # dividia por el precio, lo que invierte los dos signos: en SQM-B imprimia
    # "52W High 86,900.00 (35.36%)" al lado de un Actual de 64,201, que se lee
    # como si el precio estuviera 35% bajo el maximo cuando en realidad esta 26%
    # bajo (y 64% sobre el minimo). Dos fuentes, una linea, dos bases: el bloque se
    # contradecia solo. ``scraper.validate`` recomputa estos porcentajes desde el
    # precio y el extremo para que una inversion como esa no vuelva a pasar.
    if data.w52_high is not None:
        data.w52_high_pct = round((price - data.w52_high) / data.w52_high * 100, 2)
    if data.w52_low is not None:
        data.w52_low_pct = round((price - data.w52_low) / data.w52_low * 100, 2)

    if len(close) > 1:
        prev_close = float(close.iloc[-2])
        if prev_close:
            data.change_pct = round((last_close - prev_close) / prev_close * 100, 2)
    data.perf_week = _perf(close, _PERF_WINDOWS["week"])
    data.perf_month = _perf(close, _PERF_WINDOWS["month"])
    data.perf_quarter = _perf(close, _PERF_WINDOWS["quarter"])
    data.perf_ytd = _perf_ytd(close, index)
    data.perf_year = _perf(close, _PERF_WINDOWS["year"])

    returns = close.pct_change().dropna() * 100
    if len(returns) > _YEAR_WINDOW:
        data.volatility = round(float(returns.iloc[-_YEAR_WINDOW:].std()), 2)
    if len(returns) > _MONTH_WINDOW:
        data.volatility_month = round(float(returns.iloc[-_MONTH_WINDOW:].std()), 2)

    logger.debug(
        "%s (%s): %d velas diarias hasta %s, ATR=%s, SMA20=%s",
        ticker or symbol, symbol, data.bars, data.last_bar, data.atr, data.sma20,
    )
    return data


def candle_extremes(daily: pd.DataFrame) -> tuple[float | None, float | None, str, str]:
    """Maximo y minimo de las ultimas 22 velas, con la fecha de cada uno.

    Se calculan sobre las velas **diarias ya agregadas** de la bolsa local, que no
    son las mismas que devuelve ``YahooSource`` para un ticker de Estados Unidos:
    por eso el reporte rotula la fuente de este dato. Las fechas son la sesion en
    que se hizo cada extremo, que es lo que necesita la revision de
    ``scraper.validate`` para no leer como contradiccion el atraso de la fuente
    del rango de 52 semanas.
    """
    if daily is None or daily.empty or not {"High", "Low"} <= set(daily.columns):
        return None, None, "", ""
    window = daily.iloc[-CANDLE_WINDOW:]
    if len(window) < CANDLE_WINDOW:
        return None, None, "", ""
    highs, lows = window["High"].dropna(), window["Low"].dropna()
    if highs.empty or lows.empty:
        return None, None, "", ""
    return (
        round(float(highs.max()), 4),
        round(float(lows.min()), 4),
        _bar_date(highs.idxmax()),
        _bar_date(lows.idxmin()),
    )


def _bar_date(bar: object) -> str:
    """Fecha de la vela de una etiqueta del indice, o vacio si no la trae."""
    stamp = getattr(bar, "date", None)
    return f"{stamp():%Y-%m-%d}" if callable(stamp) else ""


def candle_window(daily: pd.DataFrame) -> tuple[float | None, float | None]:
    """Maximo y minimo de las ultimas 22 velas (Chandelier Exit del Prompt Maestro)."""
    high, low, _, _ = candle_extremes(daily)
    return high, low
