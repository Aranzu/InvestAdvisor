"""Screener de compra: prefiltro tecnico + fundamentales + proyecciones.

No hay watchlist curada: sobre un conjunto amplio (el S&P 500 vivo) se calculan
metricas tecnicas y fundamentales por ticker y se rankean para quedarse con un
puñado de candidatos de **compra**. El prefiltro tecnico aplica las puertas que no
necesitan setup (tendencia, rango de 52 semanas, liquidez y precio); el bloque
fundamental (`core.fundamentals`) añade valoracion, calidad, crecimiento y
objetivo de analistas; el orden final es por **score compuesto**, que mezcla el
score fundamental con la fuerza relativa segun ``screener.momentum_weight``.

La fuerza relativa es el percentil del retorno **en todo el universo**, no entre
los que sobrevivieron al prefiltro: medirla solo entre ganadores la infla y la
vuelve incomparable de una corrida a otra.

Todo es puro: recibe los ``IndicatorFrame`` y los ``Fundamentals`` ya calculados y
devuelve el ranking y los rechazos. Sin datos fundamentales (p. ej. ETFs) el
screener se comporta como antes: ordena por fuerza relativa.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from swing.core.engine import IndicatorFrame, relative_strength_percentiles
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
    """Score final 0..100: fundamental mezclado con fuerza relativa (``None`` sin datos)."""
    fundamental: float | None = None
    """Score fundamental puro 0..100, antes de mezclar el momentum."""
    value: float | None = None
    quality: float | None = None
    growth: float | None = None
    projections: float | None = None
    coverage: float | None = None
    """Fraccion 0..1 de metricas fundamentales disponibles."""
    target_upside: float | None = None
    """Potencial al objetivo de analistas (fraccion)."""
    fundamental_notes: tuple[str, ...] = ()
    data_quality: str = "sin_datos"
    """``ok`` con cobertura suficiente, ``sin_datos`` si no hubo fundamentales."""


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
    min_score: float = 0.0,
) -> DiscoveryResult:
    """Rankea por score compuesto (si hay fundamentales) o por fuerza relativa.

    El prefiltro aplica las puertas tecnicas que no requieren setup (G1/G2, G3,
    G5 y precio) y el piso de fuerza relativa del screener. Con ``fundamentals``
    se aplican ademas las puertas fundamentales y se ordena por score; sin ellos,
    por fuerza relativa. ``min_score`` se aplica **antes** de recortar a ``top``:
    asi el corte no deja afuera candidatos validos que estaban mas abajo.
    """
    rs_by_ticker = relative_strength_percentiles(dict(frames), strategy.gates.rs_lookback_days)
    pre = _prefilter(frames, strategy, rs_by_ticker)
    rejected: list[RejectedCandidate] = []
    if not pre:
        return DiscoveryResult(candidates=(), evaluated=len(frames), kept=0, rejected=())

    scored = 0
    candidates: list[DiscoveredCandidate] = []
    for ticker, momentum, close, adv_usd, atr_pct in pre:
        candidate = DiscoveredCandidate(
            ticker=ticker,
            theme=themes.get(ticker, DEFAULT_THEME),
            close=close,
            momentum=momentum,
            rs_percentile=rs_by_ticker[ticker],
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
                if candidate.composite < min_score:
                    continue
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
    if fundamentals is not None:
        candidates = _one_per_company(candidates, fundamentals)
    return DiscoveryResult(
        candidates=tuple(candidates[:top]),
        evaluated=len(frames),
        kept=len(candidates),
        rejected=tuple(rejected),
        scored=scored,
    )


def _one_per_company(
    ranked: list[DiscoveredCandidate],
    fundamentals: Mapping[str, Fundamentals],
) -> list[DiscoveredCandidate]:
    """Una sola clase de accion por empresa (GOOG/GOOGL, FOX/FOXA, NWS/NWSA).

    Dos clases de la misma empresa comparten fundamentales y descripcion del
    negocio: dejarlas las dos ocupa dos lugares del recorte con la misma apuesta
    (y duplica la concentracion). Se queda la mejor rankeada; la identidad sale
    del resumen del negocio, que Yahoo repite textual entre clases.
    """
    seen: set[str] = set()
    out: list[DiscoveredCandidate] = []
    for candidate in ranked:
        f = fundamentals.get(candidate.ticker)
        key = (f.business_summary or "").strip().lower() if f is not None else ""
        if key:
            if key in seen:
                continue
            seen.add(key)
        out.append(candidate)
    return out


def technical_survivors(frames: Mapping[str, IndicatorFrame], strategy: StrategyConfig) -> tuple[str, ...]:
    """Tickers que pasan el prefiltro tecnico (para pedir fundamentales solo de ellos)."""
    rs_by_ticker = relative_strength_percentiles(dict(frames), strategy.gates.rs_lookback_days)
    return tuple(ticker for ticker, *_ in _prefilter(frames, strategy, rs_by_ticker))


def earnings_gate_rejection(
    sessions: int | None,
    *,
    blackout: int,
    source_available: bool,
    fail_closed: bool,
) -> Rejected | None:
    """Puerta G6 para una entrada del screener: el mismo juicio que el motor.

    Tres casos, en el orden del motor (``core.screening``):

    - Fecha conocida dentro del blackout -> ``EARNINGS_NEAR``.
    - Sin fecha, con la fuente consultada y ``fail_closed`` -> ``INDICATOR_NOT_READY``:
      el motor rechaza la entrada antes que asumir una fecha que no tiene.
    - Fuente no disponible (p. ej. ``--offline``) -> no se puede saber: se deja pasar
      y el informe declara el hueco (``-`` en ``earn (ses)``).

    Sin ``fail_closed`` una fecha ausente con la fuente viva tampoco filtra: el
    screener recomienda y el motor vuelve a mirar G6 al armar la entrada.
    """
    if sessions is not None:
        if sessions <= blackout:
            return Rejected(
                reason=Reason.EARNINGS_NEAR,
                detail=f"earnings en {sessions} sesiones, horizonte {blackout}",
            )
        return None
    if source_available and fail_closed:
        return Rejected(reason=Reason.INDICATOR_NOT_READY, detail="calendario de earnings no disponible")
    return None


def _prefilter(
    frames: Mapping[str, IndicatorFrame],
    strategy: StrategyConfig,
    rs_by_ticker: Mapping[str, float],
) -> list[tuple[str, float, float, float, float]]:
    """``(ticker, momentum, close, adv_usd, atr_pct)`` de lo que pasa el tecnico."""
    gates = strategy.gates
    floor = strategy.screener.min_rs_percentile
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
        max_extension = strategy.screener.max_pct_above_sma50
        if max_extension is not None and feature.sma50 > 0.0:
            extension = (close / feature.sma50 - 1.0) * 100.0
            if extension > max_extension:
                continue
        momentum = frame.returns(gates.rs_lookback_days)
        rs = rs_by_ticker.get(ticker)
        if momentum is None or rs is None:
            continue
        if floor is not None and rs < floor:
            continue
        atr = feature.atr or 0.0
        pre.append((ticker, momentum, close, feature.adv_usd, atr / close * 100.0))
    return pre


def _apply_fundamentals(
    candidate: DiscoveredCandidate,
    f: Fundamentals | None,
    strategy: StrategyConfig,
) -> tuple[DiscoveredCandidate, tuple[Rejected, ...]]:
    """Puertas + score fundamentales sobre un candidato ya prefiltrado."""
    config = strategy.fundamentals
    required = config.require_fundamentals or strategy.screener.require_fundamentals
    if f is None:
        if required:
            return candidate, (Rejected(reason=Reason.FUNDAMENTALS_MISSING, detail="sin datos fundamentales"),)
        return candidate, ()
    if required and f.is_empty:
        return candidate, (Rejected(reason=Reason.FUNDAMENTALS_MISSING, detail="sin datos fundamentales utilizables"),)
    gate = check_fundamentals(f, candidate.close, config)
    if gate.rejections:
        return candidate, gate.rejections
    score = score_fundamentals(f, candidate.close, config)
    if score.composite is not None and score.coverage < strategy.screener.min_coverage:
        return candidate, (
            Rejected(
                reason=Reason.FUNDAMENTALS_MISSING,
                detail=(
                    f"cobertura de fundamentales {score.coverage * 100:.0f}% "
                    f"< {strategy.screener.min_coverage * 100:.0f}%"
                ),
            ),
        )
    weight = strategy.screener.momentum_weight
    composite = None if score.composite is None else (1.0 - weight) * score.composite + weight * candidate.rs_percentile
    updated = DiscoveredCandidate(
        ticker=candidate.ticker,
        theme=candidate.theme,
        close=candidate.close,
        momentum=candidate.momentum,
        rs_percentile=candidate.rs_percentile,
        adv_usd=candidate.adv_usd,
        atr_pct=candidate.atr_pct,
        composite=composite,
        fundamental=score.composite,
        value=score.value,
        quality=score.quality,
        growth=score.growth,
        projections=score.projections,
        coverage=score.coverage,
        target_upside=f.target_upside(candidate.close),
        fundamental_notes=score.missing,
        data_quality="ok" if score.composite is not None else "sin_datos",
    )
    return updated, ()


def theme_allowed(theme: str, counts: Mapping[str, int], max_per_theme: int | None) -> bool:
    """``True`` si el tema todavia tiene cupo en el recorte final.

    ``sin_tema`` no se capa: agrupa tickers de sectores desconocidos (watchlist sin
    mapa de temas) y caparlos dejaria el recorte vacio sin explicar nada.
    """
    if max_per_theme is None or theme == DEFAULT_THEME:
        return True
    return counts.get(theme, 0) < max_per_theme
