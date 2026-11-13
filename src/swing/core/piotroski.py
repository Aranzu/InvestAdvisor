"""F-Score de Piotroski (2000): 9 senales binarias de calidad financiera, 0 a 9.

Funcion pura sobre dos ejercicios anuales (el ultimo y el anterior). Una senal cuyos
datos faltan queda en ``None`` y no suma: ``computed`` dice cuantas se pudieron evaluar,
y un score con pocas senales calculables no debe usarse (``min_f_score_computed``).

Simplificaciones declaradas: ROA = utilidad neta / activos totales del mismo cierre
(el original usa activos iniciales) y el apalancamiento es deuda de largo plazo sobre
activos totales.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class AnnualInputs:
    """Cifras de un ejercicio anual; ``None`` = la fuente no la trajo."""

    net_income: float | None = None
    total_assets: float | None = None
    operating_cash_flow: float | None = None
    long_term_debt: float | None = None
    current_assets: float | None = None
    current_liabilities: float | None = None
    shares: float | None = None
    gross_profit: float | None = None
    revenue: float | None = None


@dataclass(frozen=True, slots=True)
class FScore:
    score: int
    """Senales cumplidas (de las calculables)."""
    computed: int
    """Senales que se pudieron evaluar (0 a 9)."""
    signals: Mapping[str, bool | None] = field(default_factory=dict)


def _ratio(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator is None or denominator <= 0.0:
        return None
    return numerator / denominator


def f_score(current: AnnualInputs, previous: AnnualInputs) -> FScore:
    """Las 9 senales; ``previous`` es el ejercicio anterior al de ``current``."""
    roa = _ratio(current.net_income, current.total_assets)
    roa_prev = _ratio(previous.net_income, previous.total_assets)
    leverage = _ratio(current.long_term_debt, current.total_assets)
    leverage_prev = _ratio(previous.long_term_debt, previous.total_assets)
    liquidity = _ratio(current.current_assets, current.current_liabilities)
    liquidity_prev = _ratio(previous.current_assets, previous.current_liabilities)
    gross = _ratio(current.gross_profit, current.revenue)
    gross_prev = _ratio(previous.gross_profit, previous.revenue)
    turnover = _ratio(current.revenue, current.total_assets)
    turnover_prev = _ratio(previous.revenue, previous.total_assets)

    def greater(a: float | None, b: float | None) -> bool | None:
        return None if a is None or b is None else a > b

    cfo = current.operating_cash_flow
    signals: dict[str, bool | None] = {
        "roa_positivo": None if roa is None else roa > 0.0,
        "cfo_positivo": None if cfo is None else cfo > 0.0,
        "roa_sube": greater(roa, roa_prev),
        "cfo_mayor_que_utilidad": None if cfo is None or current.net_income is None else cfo > current.net_income,
        # Menos apalancamiento; sin deuda en el ejercicio actual tambien cuenta.
        "apalancamiento_baja": (
            True
            if current.long_term_debt == 0.0
            else None
            if leverage is None or leverage_prev is None
            else leverage < leverage_prev
        ),
        "liquidez_sube": greater(liquidity, liquidity_prev),
        "sin_emision": None if current.shares is None or previous.shares is None else current.shares <= previous.shares,
        "margen_bruto_sube": greater(gross, gross_prev),
        "rotacion_activos_sube": greater(turnover, turnover_prev),
    }
    evaluated = [value for value in signals.values() if value is not None]
    return FScore(score=sum(1 for value in evaluated if value), computed=len(evaluated), signals=signals)
