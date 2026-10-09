"""Screener de compra: prefiltro tecnico + fundamentales + proyecciones.

No hay watchlist curada: sobre un conjunto amplio (el S&P 500 vivo) se calculan
metricas tecnicas y fundamentales por ticker y se rankean para quedarse con un
puñado de candidatos de **compra**. El prefiltro tecnico aplica las puertas que no
necesitan setup (tendencia, rango de 52 semanas, liquidez y precio); el bloque
fundamental (`core.fundamentals`) añade valoracion, calidad, crecimiento y
objetivo de analistas; el orden final es por **score compuesto**.

Todo es puro: recibe los ``IndicatorFrame`` y los ``Fundamentals`` ya calculados y
devuelve el ranking y los rechazos. Sin datos fundamentales (p. ej. ETFs) el
screener se comporta como antes: ordena por fuerza relativa.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from swing.core.engine import IndicatorFrame
from swing.core.fundamentals import Fundamentals, check_fundamentals, score_fundamentals
from swing.core.models import Rejected, StrategyConfig
from swing.core.reasons import Reason

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
    composite: float | None = None
    """Score fundamental compuesto 0..100 (``None`` sin datos)."""
    value: float | None = None
    quality: float | None = None
    growth: float | None = None
    projections: float | None = None
    coverage: float | None = None
    """Fraccion 0..1 de metricas fundamentales disponibles."""
    target_upside: float | None = None
    """Potencial al objetivo de analistas (fraccion)."""
    fundamental_notes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RejectedCandidate:
    """Un ticker fuera del ranking, con los motivos (tecnicos o fundamentales)."""

    ticker: str
    reasons: tuple[Rejected, ...]


@dataclass(frozen=True, slots=True)
class DiscoveryResult:
    """El ranking final y cuantos tickers se evaluaron/sobrevivieron."""

    candidates: tuple[DiscoveredCandidate, ...]
    evaluated: int
    kept: int
    rejected: tuple[RejectedCandidate, ...] = ()
    scored: int = 0
    """Cuantos candidatos entraron al ranking con score fundamental."""


def rank_candidates(
    frames: Mapping[str, IndicatorFrame],
    themes: Mapping[str, str],
    strategy: StrategyConfig,
    *,
    top: int,
    fundamentals: Mapping[str, Fundamentals] | None = None,
) -> DiscoveryResult:
    """Rankea por score compuesto (si hay fundamentales) o por fuerza relativa.

    El prefiltro aplica las puertas tecnicas que no requieren setup (G1/G2, G3,
    G5 y precio). Con ``fundamentals`` se aplican ademas las puertas
    fundamentales y se ordena por score; sin ellos, por fuerza relativa.
    """
    gates = strategy.gates
    lookback = gates.rs_lookback_days
    pre: list[tuple[str, float, float, float, float]] = []
    rejected: list[RejectedCandidate] = []
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
        return DiscoveryResult(candidates=(), evaluated=len(frames), kept=0, rejected=())

    ordered = sorted(momentum for _, momentum, *_ in pre)
    size = len(ordered)
    scored = 0
    candidates: list[DiscoveredCandidate] = []
    for ticker, momentum, close, adv_usd, atr_pct in pre:
        beaten = sum(1 for other in ordered if other <= momentum)
        rs = beaten / size * 100.0
        candidate = DiscoveredCandidate(
            ticker=ticker,
            theme=themes.get(ticker, DEFAULT_THEME),
            close=close,
            momentum=momentum,
            rs_percentile=rs,
            adv_usd=adv_usd,
            atr_pct=atr_pct,
        )
        if fundamentals is not None:
            candidate, rejections = _apply_fundamentals(candidate, fundamentals.get(ticker), strategy)
            if rejections:
                rejected.append(RejectedCandidate(ticker=ticker, reasons=rejections))
                continue
            if candidate.composite is not None:
                scored += 1
        candidates.append(candidate)

    if fundamentals is not None:
        candidates.sort(
            key=lambda c: (
                -(c.composite if c.composite is not None else -1.0),
                -c.rs_percentile,
                c.ticker,
            )
        )
    else:
        candidates.sort(key=lambda candidate: (-candidate.rs_percentile, -candidate.momentum, candidate.ticker))
    return DiscoveryResult(
        candidates=tuple(candidates[:top]),
        evaluated=len(frames),
        kept=len(candidates),
        rejected=tuple(rejected),
        scored=scored,
    )


def _apply_fundamentals(
    candidate: DiscoveredCandidate,
    f: Fundamentals | None,
    strategy: StrategyConfig,
) -> tuple[DiscoveredCandidate, tuple[Rejected, ...]]:
    """Puertas + score fundamentales sobre un candidato ya prefiltrado."""
    config = strategy.fundamentals
    if f is None:
        if config.require_fundamentals:
            return candidate, (Rejected(reason=Reason.FUNDAMENTALS_MISSING, detail="sin datos fundamentales"),)
        return candidate, ()
    gate = check_fundamentals(f, candidate.close, config)
    if gate.rejections:
        return candidate, gate.rejections
    score = score_fundamentals(f, candidate.close, config)
    updated = DiscoveredCandidate(
        ticker=candidate.ticker,
        theme=candidate.theme,
        close=candidate.close,
        momentum=candidate.momentum,
        rs_percentile=candidate.rs_percentile,
        adv_usd=candidate.adv_usd,
        atr_pct=candidate.atr_pct,
        composite=score.composite,
        value=score.value,
        quality=score.quality,
        growth=score.growth,
        projections=score.projections,
        coverage=score.coverage,
        target_upside=f.target_upside(candidate.close),
        fundamental_notes=score.missing,
    )
    return updated, ()
