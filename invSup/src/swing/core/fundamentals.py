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

from swing.core.models import FundamentalsConfig, Rejected
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
    "target_mean",
)


def score_fundamentals(f: Fundamentals, price: float, config: FundamentalsConfig) -> FundamentalScore:
    """Calcula el score compuesto a partir de las anclas de config."""
    a = config.score
    upside = f.target_upside(price)

    value = _mean(
        [
            _lower_better(f.forward_pe, a.forward_pe_good, a.forward_pe_bad),
            _lower_better(f.price_to_book, a.pb_good, a.pb_bad),
        ]
    )
    quality = _mean(
        [
            _higher_better(f.return_on_equity, a.roe_good, a.roe_bad),
            _higher_better(f.profit_margin, a.margin_good, a.margin_bad),
            _lower_better(f.debt_to_equity, a.debt_good, a.debt_bad),
        ]
    )
    growth = _mean(
        [
            _higher_better(f.revenue_growth, a.rev_growth_good, a.rev_growth_bad),
            _higher_better(f.earnings_growth, a.rev_growth_good, a.rev_growth_bad),
            _higher_better(f.eps_growth_estimate, a.eps_growth_good, a.eps_growth_bad),
        ]
    )
    projections = _mean([_higher_better(upside, a.upside_good, a.upside_bad)])

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
        if block is None:
            continue
        weighted += block * weights[name]
        total_weight += weights[name]
    composite = weighted / total_weight * 100.0 if total_weight > 0.0 else None

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

    over(f.forward_pe, config.max_forward_pe, "forward_pe")
    over(f.trailing_pe, config.max_trailing_pe, "trailing_pe")
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
