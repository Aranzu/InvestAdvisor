"""Hechos de mercado como los ve el motor (spec v3.1 secciones 6 y 8).

Velas diarias **sin ajustar por dividendos** (``auto_adjust=False``), tal como pide el
spec. Es una decision con costo (D6): el precio crudo trae dentro de la serie el salto
del split y la caida del ex-dividendo, y por eso existen ``core.dataquality`` y el rebasing
del backtest. Ajustar aqui habria roto los precios de entrada ya ejecutados, que es un
precio real.

El modulo es puro y no sabe de donde vienen los datos. ``DataProvider`` es la unica
puerta: la implementan los adaptadores (``data_yahoo`` y, mas adelante, el importador de
XTB), y tanto el backtest como la produccion consumen la misma interfaz. Eso es lo que
permite que el simulador y la corrida de la noche no se separen nunca.

Las validaciones de OHLC viven en el constructor de ``Bar`` y no en la capa de calidad:
una vela con el maximo mas bajo que el cierre no es una vela dudosa, es una vela
imposible, y tiene que fallar al construirse. La calidad de datos responde otra
pregunta: si lo que tengo es *suficiente y reciente* para operar.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

import pandas as pd

#: Tolerancia de igualdad al comparar precios de providers distintos (centavos).
PRICE_EPSILON = 1e-9


@dataclass(frozen=True, slots=True)
class Bar:
    """Una vela diaria. ``date`` es la fecha de la sesion, no la de descarga."""

    date: dt.date
    open: float
    high: float
    low: float
    close: float
    volume: float

    def __post_init__(self) -> None:
        if self.open <= 0.0 or self.high <= 0.0 or self.low <= 0.0 or self.close <= 0.0:
            raise ValueError(
                f"{self.date}: precios no positivos (open={self.open} high={self.high} low={self.low} close={self.close})"
            )
        if self.volume < 0.0:
            raise ValueError(f"{self.date}: volumen negativo ({self.volume})")
        top = max(self.open, self.close)
        bottom = min(self.open, self.close)
        if self.high + PRICE_EPSILON < top or self.low - PRICE_EPSILON > bottom:
            raise ValueError(
                f"{self.date}: vela inconsistente: high {self.high:.4f} / low {self.low:.4f} "
                f"no contienen open {self.open:.4f} y close {self.close:.4f}"
            )
        if self.low > self.high:
            raise ValueError(f"{self.date}: low {self.low:.4f} mayor que high {self.high:.4f}")

    @property
    def is_flat(self) -> bool:
        """Sesion sin movimiento: open, high, low y close iguales."""
        return self.low == self.high


@dataclass(frozen=True, slots=True)
class PriceSeries:
    """Velas de un ticker, ordenadas y sin fechas repetidas.

    El orden y la unicidad se validan al construir. Series ordenadas por fecha son la
    unica forma de que ``dataquality`` pueda afirmar que la ultima vela es la ultima
    sesion sin tener que defensar contra el desorden.
    """

    ticker: str
    bars: tuple[Bar, ...] = ()

    def __post_init__(self) -> None:
        previous: dt.date | None = None
        for bar in self.bars:
            if previous is not None:
                if bar.date == previous:
                    raise ValueError(f"{self.ticker}: fecha repetida {bar.date.isoformat()}")
                if bar.date < previous:
                    raise ValueError(f"{self.ticker}: velas desordenadas en {bar.date.isoformat()}")
            previous = bar.date

    @classmethod
    def empty(cls, ticker: str) -> PriceSeries:
        return cls(ticker=ticker, bars=())

    def __len__(self) -> int:
        return len(self.bars)

    def __iter__(self) -> Iterator[Bar]:
        return iter(self.bars)

    def __getitem__(self, index: int) -> Bar:
        return self.bars[index]

    @property
    def is_empty(self) -> bool:
        return not self.bars

    @property
    def first_date(self) -> dt.date | None:
        return self.bars[0].date if self.bars else None

    @property
    def last_date(self) -> dt.date | None:
        return self.bars[-1].date if self.bars else None

    @property
    def last_close(self) -> float | None:
        return self.bars[-1].close if self.bars else None

    def has_session(self, day: dt.date) -> bool:
        return any(bar.date == day for bar in self.bars)

    def slice(self, start: dt.date | None = None, end: dt.date | None = None) -> PriceSeries:
        """Velas en ``[start, end]``; ``None`` es sin limite."""
        return PriceSeries(
            ticker=self.ticker,
            bars=tuple(b for b in self.bars if (start is None or b.date >= start) and (end is None or b.date <= end)),
        )

    def tail(self, count: int) -> PriceSeries:
        if count < 0:
            raise ValueError(f"count debe ser >= 0; recibio {count}")
        return PriceSeries(ticker=self.ticker, bars=self.bars[-count:] if count else ())

    def _column(self, attribute: str) -> pd.Series:
        """Columna como ``pd.Series`` indexada por fecha de sesion.

        Indicadores y screening trabajan con ``pd.Series`` (spec seccion 6: "los
        indicadores se calculan localmente"), asi que la conversion ocurre una sola
        vez y aqui, no en cada consumidor.
        """
        index = pd.DatetimeIndex([pd.Timestamp(bar.date) for bar in self.bars])
        return pd.Series([getattr(bar, attribute) for bar in self.bars], index=index, dtype=float, name=attribute)

    def closes(self) -> pd.Series:
        return self._column("close")

    def highs(self) -> pd.Series:
        return self._column("high")

    def lows(self) -> pd.Series:
        return self._column("low")

    def opens(self) -> pd.Series:
        return self._column("open")

    def volumes(self) -> pd.Series:
        return self._column("volume")

    def frame(self) -> pd.DataFrame:
        """Tabla OHLCV indexada por fecha, para calculos vectoriales."""
        return pd.DataFrame(
            {
                "open": self.opens(),
                "high": self.highs(),
                "low": self.lows(),
                "close": self.closes(),
                "volume": self.volumes(),
            }
        )


@dataclass(frozen=True, slots=True)
class EarningsInfo:
    """Fecha del proximo reporte, y si se sabe que se sabe (decision D1).

    ``date is None`` **no** significa "no hay reporte": significa que la fuente no
    entrego fecha. El motor no puede tratar los dos casos igual, porque uno permite
    entrar y el otro no. Por eso ``confirmed`` es explicito y ``unknown()`` es la
    forma de construir el dato faltante sin ambiguedad.
    """

    date: dt.date | None
    source: str
    confirmed: bool

    @classmethod
    def unknown(cls, source: str) -> EarningsInfo:
        return cls(date=None, source=source, confirmed=False)

    @property
    def is_missing(self) -> bool:
        return self.date is None


@runtime_checkable
class DataProvider(Protocol):
    """Lo que el motor le pide a una fuente de datos.

    Deliberadamente chico: solo lo que el playbook necesita. Agregar metodos "por si
    acaso" aqui es como se termina con dos fuentes de verdad para el mismo dato.
    """

    def daily_bars(self, ticker: str, *, start: dt.date, end: dt.date) -> PriceSeries:
        """Velas diaria sin ajustar de ``[start, end]``.

        Implementacion responsable de la cache incremental: devolver la serie pedida
        aunque la mayor parte venga de disco.
        """
        ...

    def earnings(self, ticker: str) -> EarningsInfo:
        """Proximo reporte conocido. Sin fecha devuelve ``confirmed=False`` (D1)."""
        ...

    def fx_usdclp(self, on: dt.date) -> float | None:
        """Cotizacion CLP por USD del dia. ``None`` si no hay dato utilizable."""
        ...
