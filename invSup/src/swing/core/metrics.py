"""Metricas de la estrategia y salvaguardas (spec v3.1 seccion 13 y D8).

Aqui se define que es un numero que se puede decir en voz alta y que no:

- La expectativa (R), win rate, profit factor y SQN se calculan **sobre los
  trades normales**, sin los ``SINGLE`` (D8): por construccion un trade de una
  sola accion no puede ganar por meta parcial, y mezclarlo deprime el win rate
  comparando dos poblaciones con mecanicas distintas.
- El win rate no se reporta nunca solo: se reporta junto a la expectativa, el
  profit factor, el SQN y el drawdown. Win rate > 75 % con expectativa <= 0 es
  alarma (alerta de coherencia, seccion 13), no exito.
- El drawdown se mide sobre la curva de equity en la moneda base, no sobre el R
  promedio, porque el riesgo se dimensiona en porcentaje del equity y es ese
  numero el que el usuario siente.

El kill switch (seccion 13, v3.1) tiene tres estados:

- ``DEFENSIVE``: solo salidas hasta reactivacion manual. Con drawdown sobre el
  tope (cualquier n), o con ``n >= kill_min_trades`` y
  ``expectativa + kill_expectancy_se_mult x error_estandar < 0``.
- ``WARN``: ``n >= kill_warn_trades`` y expectativa negativa. Con n = 20 el
  error estandar ronda 0.2 R, y el azar alcanza para producir ese numero.
- ``OK``: sin avisos.
"""

from __future__ import annotations

import datetime as dt
import math
import statistics
from dataclasses import dataclass
from enum import StrEnum

import numpy as np

from swing.core.models import SafetyConfig, ValidationConfig


@dataclass(frozen=True, slots=True)
class TradeRecord:
    """Un trade cerrado, con lo que las metricas necesitan y nada mas.

    ``r`` es el multiple **neto** de costos; ``gross_r`` el bruto, para poder
    ver cuanto costaron las piernas. ``earnings_derisk`` distingue la causa para
    el KPI de la seccion 4.6 (meta <= 15 %). ``single`` separa el libro normal
    del reporte ``SINGLE`` (D8).
    """

    ticker: str
    theme: str
    entry_date: dt.date
    exit_date: dt.date
    days: int
    entry_price: float
    exit_price: float
    volume: float
    gross_r: float
    r: float
    cause: str
    earnings_derisk: bool = False
    single: bool = False


@dataclass(frozen=True, slots=True)
class Metrics:
    """Resumen de un conjunto de trades. Todo es derivado; no hay valores que
    se carguen a mano."""

    n: int
    win_rate: float
    expectancy_r: float
    gross_expectancy_r: float
    avg_win_r: float
    avg_loss_r: float
    profit_factor: float
    sqn: float
    total_r: float
    avg_days: float
    earnings_derisk_share_pct: float
    by_cause: tuple[tuple[str, int, float], ...]
    by_theme: tuple[tuple[str, int, float], ...]
    single_n: int
    single_win_rate: float
    single_expectancy_r: float

    def coherence_alarm(self) -> bool:
        """Win rate alto con expectativa <= 0: coherencia imposible (seccion 13)."""
        return self.n >= 10 and self.win_rate > 0.75 and self.expectancy_r <= 0.0


def _normal_records(records: tuple[TradeRecord, ...]) -> tuple[TradeRecord, ...]:
    return tuple(r for r in records if not r.single)


def compute_metrics(records: tuple[TradeRecord, ...]) -> Metrics:
    """Metricas de la puerta sobre los trades normales, mas el reporte SINGLE."""
    normal = _normal_records(records)
    rs = np.array([r.r for r in normal], dtype="float64")
    gross = np.array([r.gross_r for r in normal], dtype="float64")
    single = tuple(r for r in records if r.single)
    single_rs = np.array([r.r for r in single], dtype="float64")

    n = len(normal)
    if n == 0:
        return Metrics(
            n=0, win_rate=0.0, expectancy_r=0.0, gross_expectancy_r=0.0,
            avg_win_r=0.0, avg_loss_r=0.0, profit_factor=0.0, sqn=0.0, total_r=0.0,
            avg_days=0.0, earnings_derisk_share_pct=0.0, by_cause=(), by_theme=(),
            single_n=len(single), single_win_rate=_win_rate(single_rs),
            single_expectancy_r=_mean(single_rs),
        )

    wins = rs[rs > 0.0]
    losses = rs[rs <= 0.0]
    gross_wins_sum = max(gross[rs > 0.0].sum().item(), 0.0)
    gross_loss_sum = -min(gross[rs <= 0.0].sum().item(), 0.0)
    pf = gross_wins_sum / gross_loss_sum if gross_loss_sum > 0.0 else float("inf")

    by_cause: dict[str, list[float]] = {}
    for r in records:
        by_cause.setdefault(r.cause, []).append(r.r)
    by_theme: dict[str, list[float]] = {}
    for r in normal:
        by_theme.setdefault(r.theme, []).append(r.r)

    derisk = sum(1 for r in normal if r.earnings_derisk)
    return Metrics(
        n=n,
        win_rate=_win_rate(rs),
        expectancy_r=_mean(rs),
        gross_expectancy_r=_mean(gross),
        avg_win_r=_mean(wins),
        avg_loss_r=_mean(losses),
        profit_factor=pf,
        sqn=_sqn(rs),
        total_r=float(rs.sum()),
        avg_days=statistics.fmean(r.days for r in normal),
        earnings_derisk_share_pct=derisk / n * 100.0,
        by_cause=tuple((cause, len(values), sum(values)) for cause, values in sorted(by_cause.items())),
        by_theme=tuple((theme, len(values), sum(values)) for theme, values in sorted(by_theme.items())),
        single_n=len(single),
        single_win_rate=_win_rate(single_rs),
        single_expectancy_r=_mean(single_rs),
    )


def _win_rate(rs: np.ndarray) -> float:
    if len(rs) == 0:
        return 0.0
    return float(np.mean(rs > 0.0))


def _mean(rs: np.ndarray) -> float:
    if len(rs) == 0:
        return 0.0
    return float(np.mean(rs))


def _sqn(rs: np.ndarray) -> float:
    if len(rs) < 2:
        return 0.0
    std = float(np.std(rs, ddof=1))
    if std <= 0.0:
        return float("inf") if _mean(rs) > 0.0 else 0.0
    return float(_mean(rs) / std * math.sqrt(len(rs)))


def max_drawdown_pct(values: tuple[float, ...] | list[float]) -> float:
    """Peor retroceso de la curva de equity respecto de su pico anterior, en %."""
    peak = 0.0
    worst = 0.0
    for value in values:
        peak = max(peak, value)
        if peak > 0.0:
            worst = max(worst, (peak - value) / peak)
    return worst * 100.0


def bootstrap_expectancy_ci(
    rs: tuple[float, ...] | list[float],
    level: float,
    *,
    rng: np.random.Generator | None = None,
) -> tuple[float, float]:
    """Intervalo de confianza bootstrap de la expectativa (seccion 11.3 #7).

    No hay formula cerrada para el error estandar de una media de R: la
    distribucion de R es bien asimetrica (ganadores grandes, perdedores de -1).
    El bootstrap de percentiles es la apuesta segura y, con 5000 remuestreos,
    el resultado se estabiliza. La semilla la decide el llamador: dos corridas
    con la misma semilla producen el mismo intervalo.
    """
    if len(rs) == 0:
        return 0.0, 0.0
    values = np.asarray(rs, dtype="float64")
    engine = rng if rng is not None else np.random.default_rng(7)
    means = np.empty(max(1000, len(values) * 50), dtype="float64")
    n = len(values)
    for i in range(len(means)):
        means[i] = np.mean(engine.choice(values, size=n, replace=True))
    alpha = (1.0 - level) / 2.0
    lower, upper = float(np.quantile(means, alpha)), float(np.quantile(means, 1.0 - alpha))
    return lower, upper


# --------------------------------------------------------------------------- #
# Kill switch (seccion 13, v3.1)
# --------------------------------------------------------------------------- #


class KillState(StrEnum):
    """Estados de la salvaguarda: defensivo (solo salidas), aviso o normal."""

    DEFENSIVE = "DEFENSIVE"
    WARN = "WARN"
    OK = "OK"


def kill_switch_state(
    n_closed: int,
    expectancy_r: float,
    se_r: float,
    drawdown_pct: float,
    safety: SafetyConfig,
) -> KillState:
    """Estado de la salvaguarda con las reglas de la seccion 13 (v3.1).

    El drawdown es el disparador principal: supera el tope con **cualquier**
    numero de trades. La expectativa solo dispara con n suficiente: con n = 20
    (``kill_warn_trades``) y expectativa < 0 solo se alarma, porque el error
    estandar de una media de R ronda 0.2 y el azar alcanza; con n = 40
    (``kill_min_trades``) y ``expectativa + se < 0`` se pasa a modo defensivo.
    """
    if drawdown_pct >= safety.kill_max_drawdown_pct:
        return KillState.DEFENSIVE
    if n_closed >= safety.kill_min_trades and expectancy_r + safety.kill_expectancy_se_mult * se_r < 0.0:
        return KillState.DEFENSIVE
    if n_closed >= safety.kill_warn_trades and expectancy_r < 0.0:
        return KillState.WARN
    return KillState.OK


# --------------------------------------------------------------------------- #
# Puerta de validacion (seccion 11.3): el esqueleto vive aca y el veredicto
# completo en backtest.validation, que ademas produce el informe.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class GateOutcome:
    approved: bool
    conclusive: bool
    reasons: tuple[str, ...]

    @property
    def label(self) -> str:
        if not self.conclusive:
            return "NO CONCLUYENTE"
        return "APROBADO" if self.approved else "NO APROBADO"


def check_validation_metrics(
    metrics: Metrics,
    oos_expectancy: float,
    ci_lower: float,
    profit_factor: float,
    sqn: float,
    random_percentile: float,
    etf_expectancy: float,
    n_oos: int,
    validation: ValidationConfig,
    *,
    bias_not_corrected: bool,
) -> GateOutcome:
    """Aplica las reglas de la puerta sobre metricas ya calculadas (seccion 11.3).

    Con sesgo no corregido el umbral de expectativa sube a +0,25 R (seccion
    11.1), y el resultado se declara en el informe con la leyenda
    "APROBADO CON SESGO NO CORREGIDO". Sin ``n >= min_oos_trades`` el veredicto
    es ``NO CONCLUYENTE``, que cuenta como NO APROBADO (seccion 11.3 #7).
    """
    reasons: list[str] = []
    conclusive = True

    if n_oos < validation.min_oos_trades:
        conclusive = False
        reasons.append(
            f"trades fuera de muestra {n_oos} < {validation.min_oos_trades}: no concluyente"
        )

    required = 0.25 if bias_not_corrected else 0.15
    if oos_expectancy <= required:
        reasons.append(f"expectativa {oos_expectancy:+.3f} R <= {required:+.2f} R")
    if profit_factor <= 1.3:
        reasons.append(f"profit factor {profit_factor:.2f} <= 1.3")
    if sqn <= 1.5:
        reasons.append(f"SQN {sqn:.2f} <= 1.5")
    if ci_lower <= 0.0:
        reasons.append(f"limite inferior del IC {ci_lower:+.3f} R <= 0")
    if random_percentile < 90.0:
        reasons.append(f"percentil en la prueba aleatoria {random_percentile:.1f} < 90")
    if etf_expectancy <= 0.0:
        reasons.append(f"robustez en ETFs {etf_expectancy:+.3f} R <= 0")
    if metrics.earnings_derisk_share_pct > validation.max_earnings_derisk_share_pct:
        reasons.append(
            f"trades cerrados por EARNINGS_DERISK {metrics.earnings_derisk_share_pct:.1f} % "
            f"> {validation.max_earnings_derisk_share_pct:.0f} %"
        )

    return GateOutcome(approved=not reasons and conclusive, conclusive=conclusive, reasons=tuple(reasons))
