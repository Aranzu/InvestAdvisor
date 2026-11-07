"""Medicion forward de los candidatos del screener (journal, sin lookahead).

El screener registra que propuso y a que precio; con el correr de las sesiones este
modulo mide el retorno de cada candidato a 21 y 63 sesiones y lo compara con el
benchmark (``strategy.regime.index``). **No hay lookahead**: la entrada es la sesion en
que se propuso y la salida es una sesion estrictamente posterior, ya en la cache. Un
pick cuyo horizonte todavia no maduro no cuenta (no se estima ni se descarta en
silencio: aparece en el total, no en los maduros).

Modulo **puro**: recibe las ``PriceSeries`` ya cargadas y devuelve los resultados. La
persistencia del journal vive en ``services/picks_log``.
"""

from __future__ import annotations

import datetime as dt
import statistics
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from swing.core.market import PriceSeries

#: Horizontes medidos, en sesiones (aprox. un mes y un trimestre).
HORIZONS: tuple[int, ...] = (21, 63)


@dataclass(frozen=True, slots=True)
class PickRecord:
    """Un candidato tal como se propuso una noche (para medirlo despues)."""

    date: dt.date
    ticker: str
    close: float
    composite: float | None = None
    theme: str = ""


@dataclass(frozen=True, slots=True)
class PickOutcome:
    """Retorno de un pick a un horizonte, con su exceso sobre el benchmark."""

    date: dt.date
    ticker: str
    horizon: int
    ret_pct: float
    benchmark_ret_pct: float | None
    excess_pct: float | None


@dataclass(frozen=True, slots=True)
class HorizonStats:
    """Agregado de los picks maduros de un horizonte."""

    horizon: int
    matured: int
    mean_ret_pct: float | None
    median_ret_pct: float | None
    mean_excess_pct: float | None
    hit_rate_pct: float | None
    """Fraccion de picks que bate al benchmark (solo sobre los que tienen benchmark)."""


def _index_on_or_after(series: PriceSeries, day: dt.date) -> int | None:
    """Primer indice de vela en/despues de ``day`` (la sesion de entrada)."""
    for index, bar in enumerate(series.bars):
        if bar.date >= day:
            return index
    return None


def window_return(series: PriceSeries, entry_date: dt.date, horizon: int) -> float | None:
    """Retorno (fraccion) de ``horizon`` sesiones desde la entrada.

    La entrada es la primera vela en/despues de ``entry_date``; la salida, esa misma
    posicion ``+ horizon``. ``None`` si la serie no cubre la entrada o si el horizonte
    todavia no maduro (no hay vela de salida).
    """
    start = _index_on_or_after(series, entry_date)
    if start is None:
        return None
    end = start + horizon
    if end >= len(series.bars):
        return None
    entry = series.bars[start].close
    if entry <= 0.0:
        return None
    return series.bars[end].close / entry - 1.0


def evaluate(
    picks: Iterable[PickRecord],
    series_by_ticker: Mapping[str, PriceSeries],
    benchmark: PriceSeries | None,
    horizons: Sequence[int] = HORIZONS,
) -> tuple[PickOutcome, ...]:
    """Resultado de cada pick **maduro**, por horizonte, con exceso vs benchmark.

    Un pick sin serie o sin madurar no produce fila: no se inventa un retorno. Sin
    benchmark (SPY no esta en la cache) el retorno del pick igual se mide y el exceso
    queda en ``None``.
    """
    out: list[PickOutcome] = []
    for pick in picks:
        series = series_by_ticker.get(pick.ticker)
        if series is None or series.is_empty:
            continue
        for horizon in horizons:
            ret = window_return(series, pick.date, horizon)
            if ret is None:
                continue
            bench = window_return(benchmark, pick.date, horizon) if benchmark is not None else None
            out.append(
                PickOutcome(
                    date=pick.date,
                    ticker=pick.ticker,
                    horizon=horizon,
                    ret_pct=ret * 100.0,
                    benchmark_ret_pct=None if bench is None else bench * 100.0,
                    excess_pct=None if bench is None else (ret - bench) * 100.0,
                )
            )
    return tuple(out)


def summarize(outcomes: Iterable[PickOutcome], horizons: Sequence[int] = HORIZONS) -> dict[int, HorizonStats]:
    """Agrega los resultados por horizonte (media, mediana, exceso medio, bate al S&P)."""
    grouped: dict[int, list[PickOutcome]] = {horizon: [] for horizon in horizons}
    for outcome in outcomes:
        grouped.setdefault(outcome.horizon, []).append(outcome)
    stats: dict[int, HorizonStats] = {}
    for horizon, rows in grouped.items():
        rets = [row.ret_pct for row in rows]
        excess = [row.excess_pct for row in rows if row.excess_pct is not None]
        beats = [value for value in excess if value > 0.0]
        stats[horizon] = HorizonStats(
            horizon=horizon,
            matured=len(rows),
            mean_ret_pct=statistics.fmean(rets) if rets else None,
            median_ret_pct=statistics.median(rets) if rets else None,
            mean_excess_pct=statistics.fmean(excess) if excess else None,
            hit_rate_pct=(len(beats) / len(excess) * 100.0) if excess else None,
        )
    return stats
