"""Fundamentales, proyecciones de analistas y scoring (funcion 2: screener).

Modulo **puro**: recibe los datos crudos ya normalizados por el adaptador y
devuelve (a) las puertas fundamentales que un ticker no pasa y (b) un score
compuesto ``0..100`` que mezcla valor, calidad, crecimiento y proyecciones.

Ningun umbral ni ancla vive en el codigo: todos salen de
``strategy.fundamentals``. La razon es la del resto del proyecto: un numero
cocido aqui se vuelve una segunda fuente de verdad que nadie recuerda ajustar.

El score no pretende ser una verdad financiera. Es una forma **transparente y
configurable** de ordenar candidatos para que la IA y el usuario decidan; por
eso cada bloque se calcula por separado y el informe puede mostrar sus partes.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from swing.core.models import AxisThresholds, FundamentalsConfig, Rejected
from swing.core.reasons import Reason


@dataclass(frozen=True, slots=True)
class Fundamentals:
    """Panorama fundamental y de analistas de un ticker.

    Todos los campos son opcionales: ``None`` significa "la fuente no lo trajo",
    nunca "vale cero". Un dato ausente no se inventa: o el ticker queda fuera por
    ``require_fundamentals``/``require_target``, o el score se calcula con lo que
    hay y el informe declara la cobertura.
    """

    ticker: str
    as_of: dt.date
    source: str
    sector: str | None = None
    industry: str | None = None
    business_summary: str | None = None
    market_cap: float | None = None
    trailing_pe: float | None = None
    forward_pe: float | None = None
    price_to_book: float | None = None
    peg_ratio: float | None = None
    return_on_equity: float | None = None
    profit_margin: float | None = None
    operating_margin: float | None = None
    revenue_growth: float | None = None
    earnings_growth: float | None = None
    debt_to_equity: float | None = None
    current_ratio: float | None = None
    dividend_yield: float | None = None
    beta: float | None = None
    eps_trailing: float | None = None
    eps_forward: float | None = None
    target_mean: float | None = None
    target_high: float | None = None
    target_low: float | None = None
    recommendation_mean: float | None = None
    recommendation_key: str | None = None
    analyst_count: int | None = None
    eps_growth_estimate: float | None = None
    revenue_growth_estimate: float | None = None

    @classmethod
    def missing(cls, ticker: str, source: str, on: dt.date) -> Fundamentals:
        """Placeholder declarado: la fuente no respondio o no habia dato."""
        return cls(ticker=ticker, as_of=on, source=source)

    @property
    def is_empty(self) -> bool:
        """Sin ninguna metrica util (ni valoracion ni analistas)."""
        return all(
            value is None
            for value in (
                self.trailing_pe,
                self.forward_pe,
                self.price_to_book,
                self.return_on_equity,
                self.profit_margin,
                self.revenue_growth,
                self.debt_to_equity,
                self.target_mean,
            )
        )

    @property
    def negative_equity(self) -> bool:
        """Patrimonio negativo (P/B o D/E bajo cero), tipico de recompras agresivas.

        Con patrimonio negativo P/B, D/E y ROE dejan de medir lo que dicen: un P/B
        de -20 no es "barato" ni un ROE de -300 % es "malo". Esas tres metricas se
        tratan como ausentes en vez de puntuarlas al reves.
        """
        return (self.price_to_book is not None and self.price_to_book < 0.0) or (
            self.debt_to_equity is not None and self.debt_to_equity < 0.0
        )

    def target_upside(self, price: float) -> float | None:
        """Retorno esperado al objetivo medio de analistas (fraccion, no %)."""
        if price <= 0.0 or self.target_mean is None:
            return None
        return self.target_mean / price - 1.0


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def _lower_better(value: float | None, good: float, bad: float) -> float | None:
    """1.0 en ``good`` o mejor, 0.0 en ``bad`` o peor (menor es mejor)."""
    if value is None:
        return None
    if bad <= good:
        return 1.0 if value <= good else 0.0
    return _clamp((bad - value) / (bad - good))


def _higher_better(value: float | None, good: float, bad: float) -> float | None:
    """1.0 en ``good`` o mejor, 0.0 en ``bad`` o peor (mayor es mejor)."""
    if value is None:
        return None
    if good <= bad:
        return 1.0 if value >= good else 0.0
    return _clamp((value - bad) / (good - bad))


def _mean(values: list[float | None]) -> float | None:
    present = [v for v in values if v is not None]
    if not present:
        return None
    return sum(present) / len(present)


@dataclass(frozen=True, slots=True)
class FundamentalScore:
    """Score por bloque y compuesto, todos en ``0..100`` (``None`` sin datos)."""

    value: float | None
    quality: float | None
    growth: float | None
    projections: float | None
    composite: float | None
    coverage: float
    """Fraccion (0..1) de las metricas esperadas que la fuente entrego."""
    missing: tuple[str, ...] = ()


#: Campos que entran al calculo de cobertura. No es la lista de todos los campos:
#: es la de los que el score realmente usa, para que la cobertura signifique algo.
_SCORED_FIELDS: tuple[str, ...] = (
    "forward_pe",
    "price_to_book",
    "return_on_equity",
    "profit_margin",
    "debt_to_equity",
    "revenue_growth",
    "earnings_growth",
    "eps_growth_estimate",
    "revenue_growth_estimate",
    "target_mean",
    "recommendation_mean",
)


def _pe_score(pe: float | None, good: float, bad: float) -> float | None:
    """P/E menor es mejor, pero un P/E negativo (perdidas) es lo peor, no lo mejor."""
    if pe is None:
        return None
    if pe <= 0.0:
        return 0.0
    return _lower_better(pe, good, bad)


#: Valor de un bloque sin datos: neutro. Ignorarlo dejaria que un unico bloque
#: completo (p. ej. solo valoracion) decida todo el score.
NEUTRAL_BLOCK = 0.5


def score_fundamentals(f: Fundamentals, price: float, config: FundamentalsConfig) -> FundamentalScore:
    """Calcula el score compuesto a partir de las anclas de config.

    Un bloque sin ningun dato cuenta como neutro (``NEUTRAL_BLOCK``) con su peso
    completo: la falta de informacion no premia ni castiga, pero tampoco deja que
    el resto pese de mas. Sin ningun bloque, no hay score.
    """
    a = config.score
    upside = f.target_upside(price)
    distorted = f.negative_equity
    price_to_book = None if distorted else f.price_to_book
    debt_to_equity = None if distorted else f.debt_to_equity
    return_on_equity = None if distorted else f.return_on_equity

    value = _mean(
        [
            _pe_score(f.forward_pe, a.forward_pe_good, a.forward_pe_bad),
            _lower_better(price_to_book, a.pb_good, a.pb_bad),
        ]
    )
    quality = _mean(
        [
            _higher_better(return_on_equity, a.roe_good, a.roe_bad),
            _higher_better(f.profit_margin, a.margin_good, a.margin_bad),
            _lower_better(debt_to_equity, a.debt_good, a.debt_bad),
        ]
    )
    growth = _mean(
        [
            _higher_better(f.revenue_growth, a.rev_growth_good, a.rev_growth_bad),
            _higher_better(f.earnings_growth, a.eps_growth_good, a.eps_growth_bad),
            _higher_better(f.eps_growth_estimate, a.eps_growth_good, a.eps_growth_bad),
            _higher_better(f.revenue_growth_estimate, a.rev_growth_good, a.rev_growth_bad),
        ]
    )
    projections = _mean(
        [
            _higher_better(upside, a.upside_good, a.upside_bad),
            _lower_better(f.recommendation_mean, a.rec_good, a.rec_bad),
        ]
    )

    blocks = {
        "value": value,
        "quality": quality,
        "growth": growth,
        "projections": projections,
    }
    weights = {
        "value": config.weights.value,
        "quality": config.weights.quality,
        "growth": config.weights.growth,
        "projections": config.weights.projections,
    }
    weighted = 0.0
    total_weight = 0.0
    for name, block in blocks.items():
        weighted += (NEUTRAL_BLOCK if block is None else block) * weights[name]
        total_weight += weights[name]
    has_data = any(block is not None for block in blocks.values())
    composite = weighted / total_weight * 100.0 if has_data and total_weight > 0.0 else None

    present = sum(1 for name in _SCORED_FIELDS if getattr(f, name) is not None)
    coverage = present / len(_SCORED_FIELDS)
    missing = tuple(name for name in _SCORED_FIELDS if getattr(f, name) is None)

    def pct(block: float | None) -> float | None:
        return None if block is None else block * 100.0

    return FundamentalScore(
        value=pct(value),
        quality=pct(quality),
        growth=pct(growth),
        projections=pct(projections),
        composite=composite,
        coverage=coverage,
        missing=missing,
    )


@dataclass(frozen=True, slots=True)
class AxisRatings:
    """El score de cada bloque traducido a un eje ``-2..+2`` (puntaje del informe)."""

    value: int
    quality: int
    growth: int
    projections: int
    available: tuple[bool, bool, bool, bool] = (True, True, True, True)
    """Si cada bloque (V/C/G/P) tenia datos. Un eje sin datos se imprime ``n.p.`` y no puntua."""

    @property
    def total(self) -> int:
        """Puntaje del activo: la suma de los cuatro ejes (``-8..+8``)."""
        return self.value + self.quality + self.growth + self.projections

    def as_tuple(self) -> tuple[int, int, int, int]:
        return (self.value, self.quality, self.growth, self.projections)

    def formatted(self) -> str:
        """Ejes V/C/G/P con signo, separados por ``/``; un eje sin datos se imprime ``n.p.``."""
        return " / ".join(
            format_rating(value) if ok else "n.p." for value, ok in zip(self.as_tuple(), self.available, strict=True)
        )


def _rate(block: float | None, thresholds: AxisThresholds) -> int:
    """Traduce un bloque 0..100 a un eje. ``None`` (sin datos) es neutro (0)."""
    if block is None:
        return 0
    if block >= thresholds.plus2:
        return 2
    if block >= thresholds.plus1:
        return 1
    if block >= thresholds.minus1:
        return 0
    if block >= thresholds.minus2:
        return -1
    return -2


def rate_axes(score: FundamentalScore, thresholds: AxisThresholds) -> AxisRatings:
    """Convierte un ``FundamentalScore`` en los cuatro ejes del informe de review."""
    return AxisRatings(
        value=_rate(score.value, thresholds),
        quality=_rate(score.quality, thresholds),
        growth=_rate(score.growth, thresholds),
        projections=_rate(score.projections, thresholds),
        available=(
            score.value is not None,
            score.quality is not None,
            score.growth is not None,
            score.projections is not None,
        ),
    )


def format_rating(value: int) -> str:
    """Un eje con signo explícito: ``+2``, ``0``, ``-1``."""
    return f"+{value}" if value > 0 else str(value)


@dataclass(slots=True)
class FundamentalGateResult:
    """Todas las razones fundamentales por las que un ticker no pasa."""

    rejections: tuple[Rejected, ...] = field(default_factory=tuple)

    @property
    def passes(self) -> bool:
        return not self.rejections

    def add(self, reason: Reason, detail: str) -> None:
        self.rejections = (*self.rejections, Rejected(reason=reason, detail=detail))


def check_fundamentals(
    f: Fundamentals,
    price: float,
    config: FundamentalsConfig,
) -> FundamentalGateResult:
    """Puertas fundamentales: valoracion, calidad, crecimiento y proyecciones."""
    out = FundamentalGateResult()

    if f.is_empty:
        if config.require_fundamentals:
            out.add(Reason.FUNDAMENTALS_MISSING, "sin datos fundamentales utilizables")
        return out

    def over(value: float | None, limit: float | None, name: str) -> None:
        if limit is not None and value is not None and value > limit:
            out.add(Reason.FUNDAMENTAL_GRADE, f"{name} {value:.2f} sobre el tope {limit:.2f}")

    def under(value: float | None, limit: float | None, name: str) -> None:
        if limit is not None and value is not None and value < limit:
            out.add(Reason.FUNDAMENTAL_GRADE, f"{name} {value:.3f} bajo el piso {limit:.3f}")

    def losses(value: float | None, limit: float | None, name: str) -> None:
        # Un P/E negativo no pasa "bajo el tope": son perdidas, no una ganga.
        if limit is not None and value is not None and value <= 0.0:
            out.add(Reason.FUNDAMENTAL_GRADE, f"{name} {value:.2f} negativo (perdidas)")

    losses(f.forward_pe, config.max_forward_pe, "forward_pe")
    over(f.forward_pe, config.max_forward_pe, "forward_pe")
    losses(f.trailing_pe, config.max_trailing_pe, "trailing_pe")
    over(f.trailing_pe, config.max_trailing_pe, "trailing_pe")
    # Con patrimonio negativo P/B, D/E y ROE no son comparables: no filtran.
    if not f.negative_equity:
        over(f.price_to_book, config.max_price_to_book, "price_to_book")
        over(f.debt_to_equity, config.max_debt_to_equity, "debt_to_equity")
        under(f.return_on_equity, config.min_return_on_equity, "return_on_equity")
    under(f.profit_margin, config.min_profit_margin, "profit_margin")
    under(f.revenue_growth, config.min_revenue_growth, "revenue_growth")

    upside = f.target_upside(price)
    if config.require_target and f.target_mean is None:
        out.add(Reason.FUNDAMENTALS_MISSING, "sin objetivo de analistas y require_target=true")
    if config.min_target_upside_pct is not None and upside is not None:
        if upside * 100.0 < config.min_target_upside_pct:
            out.add(
                Reason.LOW_TARGET_UPSIDE,
                f"potencial {upside * 100.0:.1f} % bajo el minimo {config.min_target_upside_pct:.1f} %",
            )
    if f.analyst_count is not None and f.analyst_count < config.min_analyst_count:
        out.add(
            Reason.FUNDAMENTAL_GRADE,
            f"{f.analyst_count} analistas bajo el minimo {config.min_analyst_count}",
        )

    return out
