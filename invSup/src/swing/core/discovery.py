"""Descubrimiento de candidatos por metricas (spec 5.2, sin lista curada).

No hay watchlist: sobre un conjunto amplio (p. ej. el S&P 500 vivo) se calculan
metricas por ticker y se rankean para quedarse con un puñado de candidatos. Es un
**prefiltro barato**: aplica las puertas que no necesitan setup (tendencia, rango
de 52 semanas, liquidez y precio) y ordena por fuerza relativa. El motor
(``decide``) sigue siendo el que decide con las seis puertas y el setup completo.

Todo es puro: recibe los ``IndicatorFrame`` ya calculados y devuelve el ranking.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from swing.core.engine import IndicatorFrame
from swing.core.models import StrategyConfig

#: Etiqueta de tema por defecto si el ticker no tiene sector conocido.
DEFAULT_THEME = "sin_tema"


@dataclass(frozen=True, slots=True)
class DiscoveredCandidate:
    """Un candidato con las metricas que lo hicieron entrar al ranking."""

    ticker: str
    theme: str
    close: float
    momentum: float
    """Retorno (multiplicativo) a ``rs_lookback_days`` sesiones."""
    rs_percentile: float
    adv_usd: float
    atr_pct: float


@dataclass(frozen=True, slots=True)
class DiscoveryResult:
    """El ranking final y cuantos tickers se evaluaron/sobrevivieron."""

    candidates: tuple[DiscoveredCandidate, ...]
    evaluated: int
    kept: int


def rank_candidates(
    frames: Mapping[str, IndicatorFrame],
    themes: Mapping[str, str],
    strategy: StrategyConfig,
    *,
    top: int,
) -> DiscoveryResult:
    """Rankea el conjunto por fuerza relativa despues del prefiltro de tendencia.

    El prefiltro aplica las puertas que no requieren setup (G1/G2 tendencia,
    G3 rango de 52 semanas, G5 liquidez y precio). El setup (G P1) y el earnings
    los sigue mirando ``decide`` sobre los candidatos elegidos.
    """
    gates = strategy.gates
    lookback = gates.rs_lookback_days
    pre: list[tuple[str, float, float, float, float]] = []
    for ticker, frame in frames.items():
        feature = frame.features_at(frame.last_index)
        if feature is None or not feature.usable:
            continue
        close, sma200 = feature.close, feature.sma200
        if close is None or sma200 is None or feature.sma50 is None:
            continue
        if feature.low52 is None or feature.high52 is None or feature.sma50 < sma200:
            continue
        if close < sma200 or close < gates.min_price:
            continue
        if feature.adv_usd is None or feature.adv_usd < gates.min_avg_volume:
            continue
        if close / feature.low52 < gates.min_above_52w_low:
            continue
        if close / feature.high52 < gates.min_vs_52w_high:
            continue
        momentum = frame.returns(lookback)
        if momentum is None:
            continue
        atr = feature.atr or 0.0
        pre.append((ticker, momentum, close, feature.adv_usd, atr / close * 100.0))

    if not pre:
        return DiscoveryResult(candidates=(), evaluated=len(frames), kept=0)

    ordered = sorted(momentum for _, momentum, *_ in pre)
    size = len(ordered)
    candidates: list[DiscoveredCandidate] = []
    for ticker, momentum, close, adv_usd, atr_pct in pre:
        beaten = sum(1 for other in ordered if other <= momentum)
        candidates.append(
            DiscoveredCandidate(
                ticker=ticker,
                theme=themes.get(ticker, DEFAULT_THEME),
                close=close,
                momentum=momentum,
                rs_percentile=beaten / size * 100.0,
                adv_usd=adv_usd,
                atr_pct=atr_pct,
            )
        )
    candidates.sort(key=lambda candidate: (-candidate.rs_percentile, -candidate.momentum, candidate.ticker))
    return DiscoveryResult(candidates=tuple(candidates[:top]), evaluated=len(frames), kept=len(candidates))
