"""Puerta de validacion y veredicto del backtest (spec v3.1 seccion 11.3).

El esqueleto de las reglas vive en ``core.metrics.check_validation_metrics``;
aqui se monta el resto: el calculo del veredicto completo, la prueba aleatoria
de entradas (que necesita la serie de precios, no los trades), y el informe que
la CLI y el nightly-report van a imprimir.

La seccion 11.3 exige, en orden:

1. ``n >= min_oos_trades`` trades **fuera de muestra**. En este pipeline todo
   el walk-forward es fuera de muestra por construccion: cada decision usa solo
   la informacion disponible hasta esa noche (nada se ajusta con datos
   posteriores), asi que los trades cerrados de la corrida son la muestra OOS.
2. Expectativa > +0,25 R (sesgo de supervivencia del universo **no corregido**,
   seccion 11.1: el umbral sube de +0,15 R).
3. IC bootstrap de la expectativa con su limite inferior > 0.
4. Profit factor > 1,3 y SQN > 1,5.
5. Expectativa en el percentil >= 90 de la prueba aleatoria de entradas.
6. Robusteza en la canasta de ETFs (expectativa > 0).
7. ``EARNINGS_DERISK`` <= 15 % de los trades (en el backtest no se simula: el
   calendario de earnings no es point-in-time, ver la limitacion declarada).

Con ``n < min_oos_trades`` el veredicto es ``NO CONCLUYENTE``, que cuenta como
NO APROBADO.
"""

from __future__ import annotations

import datetime as dt
import statistics
from dataclasses import dataclass

from swing.backtest.simulator import BacktestRun
from swing.core.market import PriceSeries
from swing.core.metrics import (
    GateOutcome,
    Metrics,
    TradeRecord,
    bootstrap_expectancy_ci,
    check_validation_metrics,
    compute_metrics,
    max_drawdown_pct,
)
from swing.core.models import ValidationConfig

#: Semilla por defecto de la prueba aleatoria: dos corridas con la misma semilla
#: dan el mismo veredicto.
RANDOM_ENTRANCE_SEED = 7


@dataclass(frozen=True, slots=True)
class YearStats:
    """Un anio de la corrida, para el reporte desagregado."""

    year: int
    n: int
    expectancy_r: float
    win_rate: float
    total_r: float


@dataclass(frozen=True, slots=True)
class OperationalStats:
    """KPIs de la operativa (seccion 11.3.6): cuantas acciones por noche, etc."""

    nights: int
    nights_with_actions: int
    empty_nights_pct: float
    avg_actions: float
    max_actions: int
    avg_entries_per_night: float
    max_entries_in_one_night: int
    total_entries: int


@dataclass(frozen=True, slots=True)
class ValidationReport:
    """Veredicto completo de la puerta, con los numeros y el informe en Markdown."""

    gate: GateOutcome
    metrics: Metrics
    oos_n: int
    oos_start: dt.date
    oos_end: dt.date
    ci_level: float
    ci_lower: float
    ci_upper: float
    random_percentile: float
    etf_expectancy: float
    bias_not_corrected: bool
    max_drawdown_pct: float
    total_return_pct: float
    final_equity_clp: float
    per_year: tuple[YearStats, ...]
    operational: OperationalStats
    tax_sensitivity: tuple[tuple[float, float], ...]
    """``(tasa, expectativa neta de impuestos)`` para ``validation.tax_sensitivity_pct``."""

    def render_markdown(self) -> str:
        """Informe del veredicto. Es lo que imprime la CLI y entra al nightly."""
        m = self.metrics
        lineas = [
            f"# Validacion del backtest: {self.gate.label}",
            "",
        ]
        if self.gate.conclusive and self.bias_not_corrected and self.gate.approved:
            lineas.append("**APROBADO CON SESGO NO CORREGIDO** (universo con sesgo de supervivencia, seccion 11.1).")
            lineas.append("")
        lineas += [
            f"- Periodo evaluado: {self.oos_start.isoformat()} a {self.oos_end.isoformat()}",
            f"- Trades fuera de muestra (cerrados): **{self.oos_n}**",
            f"- Expectativa: **{m.expectancy_r:+.3f} R** (bruta {m.gross_expectancy_r:+.3f} R)",
            f"- IC {self.ci_level:.0%} bootstrap: [{self.ci_lower:+.3f}, {self.ci_upper:+.3f}] R",
            f"- Win rate: {m.win_rate:.1%} | Profit factor: {m.profit_factor:.2f} | SQN: {m.sqn:.2f}",
            f"- Percentil prueba aleatoria: **{self.random_percentile:.1f}** (>= 90)",
            f"- Esperanza en ETFs: **{self.etf_expectancy:+.3f} R** (> 0)",
            f"- Trades por EARNINGS_DERISK: {m.earnings_derisk_share_pct:.1f} % (tope en ``validation.max_earnings_derisk_share_pct``)",
            f"- Retorno total: {self.total_return_pct:+.2f} %  |  Equity final: {self.final_equity_clp:,.0f}",
            f"- Drawdown maximo: {self.max_drawdown_pct:.2f} %",
            "",
            "## Por anio",
            "",
            "| Anio | Trades | Expectativa (R) | Win rate | R total |",
            "| --- | ---: | ---: | ---: | ---: |",
        ]
        lineas += [
            f"| {y.year} | {y.n} | {y.expectancy_r:+.3f} | {y.win_rate:.1%} | {y.total_r:+.2f} |"
            for y in self.per_year
        ]
        lineas += [
            "",
            "## Operativa",
            "",
            f"- Noches evaluadas: {self.operational.nights} ({self.operational.nights_with_actions} con acciones)",
            f"- Noches vacias: {self.operational.empty_nights_pct:.1f} %",
            f"- Acciones por noche (promedio): {self.operational.avg_actions:.2f} (max {self.operational.max_actions})",
            f"- Entradas por noche: {self.operational.avg_entries_per_night:.2f} (max {self.operational.max_entries_in_one_night})",
        ]
        if m.single_n:
            lineas += [
                "",
                "## Modo SINGLE",
                f"- Trades: {m.single_n} | Win rate: {m.single_win_rate:.1%} | Expectativa: {m.single_expectancy_r:+.3f} R",
            ]
        if self.tax_sensitivity:
            lineas += [
                "",
                "## Sensibilidad a impuestos",
                "",
                "| Tasa | Expectativa neta (R) |",
                "| --- | ---: |",
            ]
            lineas += [f"| {tasa:.0%} | {expect:+.3f} |" for tasa, expect in self.tax_sensitivity]
        lineas += [
            "",
            "## Veredicto",
            "",
        ]
        if self.gate.reasons:
            lineas += [f"- No aprobado: {r}" for r in self.gate.reasons]
        else:
            lineas.append("- Aprobado: todas las reglas de la puerta se cumplen.")
        lineas += [
            "",
            "## Limitaciones declaradas",
            "",
            "- El universo de la watchlist sufre sesgo de supervivencia **no corregido**",
            "  (seccion 11.1): el umbral de expectativa sube a +0,25 R.",
            "- El backtest corre en moneda base con fx = 1: no modela el tipo de cambio.",
            "- No hay calendario de earnings point-in-time: G6 y EARNINGS_DERISK no se",
            "  simulan (el KPI de derisk de esta corrida es el de los trades sin earnings).",
            "- Los CAP_TRIM requieren confirmacion humana en produccion y se simulan",
            "  ejecutados en el backtest.",
        ]
        return "\n".join(lineas)


def validate_run(
    run: BacktestRun,
    config: ValidationConfig,
    *,
    series: dict[str, PriceSeries],
    etf_run: BacktestRun | None = None,
    trials: int = 1000,
    seed: int = RANDOM_ENTRANCE_SEED,
) -> ValidationReport:
    """Calcula el veredicto de la puerta sobre una corrida ya resuelta.

    ``series`` es la misma de la corrida: la prueba aleatoria de entradas deriva
    los "hold aleatorios" desde las velas reales, no desde los trades. ``etf_run``
    es una corrida con la canasta de ETFs como unico universo; si no se pasa, la
    regla de robustez falla con un motivo explicito (la puerta es estricta).
    """
    oos = tuple(r for r in run.trades)
    metrics = compute_metrics(oos)
    normal_rs = [r.r for r in oos if not r.single]
    ci_lower, ci_upper = bootstrap_expectancy_ci(normal_rs, config.expectancy_ci_level)
    percentile = random_entrance_percentile(oos, series, run.start, run.end, trials=trials, seed=seed)
    etf_expectancy = _etf_expectancy(etf_run) if etf_run is not None else 0.0

    equity_values = tuple(p.equity_clp for p in run.equity)
    max_dd = max_drawdown_pct(equity_values) if equity_values else 0.0
    total_return = (run.final_equity_clp / run.equity[0].equity_clp - 1.0) * 100.0 if run.equity else 0.0

    gate = check_validation_metrics(
        metrics,
        oos_expectancy=metrics.expectancy_r,
        ci_lower=ci_lower,
        profit_factor=metrics.profit_factor,
        sqn=metrics.sqn,
        random_percentile=percentile,
        etf_expectancy=etf_expectancy,
        n_oos=len([r for r in oos if not r.single]),
        validation=config,
        bias_not_corrected=True,
    )

    per_year = _per_year(oos)
    operational = _operational(run)
    tax = tuple(
        (rate, _expectancy_after_tax(normal_rs, rate))
        for rate in config.tax_sensitivity_pct
    )
    return ValidationReport(
        gate=gate,
        metrics=metrics,
        oos_n=len(normal_rs),
        oos_start=run.start,
        oos_end=run.end,
        ci_level=config.expectancy_ci_level,
        ci_lower=ci_lower,
        ci_upper=ci_upper,
        random_percentile=percentile,
        etf_expectancy=etf_expectancy,
        bias_not_corrected=True,
        max_drawdown_pct=max_dd,
        total_return_pct=total_return,
        final_equity_clp=run.final_equity_clp,
        per_year=per_year,
        operational=operational,
        tax_sensitivity=tax,
    )


# --------------------------------------------------------------------------- #
# Prueba aleatoria de entradas (seccion 11.3 #6)
# --------------------------------------------------------------------------- #


def random_entrance_percentile(
    records: tuple[TradeRecord, ...],
    series: dict[str, PriceSeries],
    start: dt.date,
    end: dt.date,
    *,
    trials: int = 1000,
    seed: int = RANDOM_ENTRANCE_SEED,
) -> float:
    """Percentil de la expectativa real en la distribucion de entradas aleatorias.

    Por cada trade real se construye una entrada aleatoria de la misma longitud
    (``days`` sesiones) en un dia al azar de la misma serie dentro del periodo:
    una compra del mismo ticker en un dia cualquiera y su venta ``days`` sesiones
    despues. La pregunta que responde es *"¿te va mejor a ti o a alguien que
    entro al azar, mismo ticker, misma duracion?"*; si la entrada tuviera que ver
    solo con el azar del mercado, el percentil rondaria 50.

    La metrica comparada es la rentabilidad porcentual promedio por trade (mismo
    denominador en las dos poblaciones). Si una serie no llega a cubrir la
    duracion, ese trade se descarta solo dentro de ese trial; un trial con menos
    de la mitad de los trades replicables se descarta y no cuenta en el
    percentil (no alcanza a ser una muestra).
    """
    observed = [t.exit_price / t.entry_price - 1.0 for t in records if not t.single and t.entry_price > 0.0]
    if not observed:
        return 0.0
    observed_mean = statistics.fmean(observed)

    closes: dict[str, list[float]] = {}
    for t in records:
        if t.single:
            continue
        series_box = series.get(t.ticker)
        if series_box is not None and not series_box.is_empty:
            closes[t.ticker] = [bar.close for bar in series_box.bars if start <= bar.date <= end]

    rng = __import__("random").Random(seed)
    beaten = 0
    valid_trials = 0
    for _ in range(trials):
        trial: list[float] = []
        for t in records:
            if t.single:
                continue
            values = closes.get(t.ticker)
            if values is None or t.days <= 0 or len(values) <= t.days:
                continue
            i = rng.randrange(len(values) - t.days)
            trial.append(values[i + t.days] / values[i] - 1.0)
        if len(trial) < max(1, len(observed) // 2):
            continue
        valid_trials += 1
        if statistics.fmean(trial) <= observed_mean:
            beaten += 1
    if valid_trials == 0:
        return 100.0
    return beaten / valid_trials * 100.0


# --------------------------------------------------------------------------- #
# Auxiliares
# --------------------------------------------------------------------------- #


def _per_year(records: tuple[TradeRecord, ...]) -> tuple[YearStats, ...]:
    normal = [r for r in records if not r.single]
    years: dict[int, list[TradeRecord]] = {}
    for record in normal:
        years.setdefault(record.entry_date.year, []).append(record)
    out: list[YearStats] = []
    for year in sorted(years):
        group = years[year]
        rs = [r.r for r in group]
        wins = sum(1 for r in rs if r > 0.0)
        out.append(
            YearStats(
                year=year,
                n=len(group),
                expectancy_r=statistics.fmean(rs),
                win_rate=wins / len(group),
                total_r=sum(rs),
            )
        )
    return tuple(out)


def _operational(run: BacktestRun) -> OperationalStats:
    nights = run.nights
    active = [n for n in nights if n.has_actions]
    entries_per_night = [n.entries for n in nights]
    actions_per_night = [n.entries + n.deferrals + n.sells + n.stop_edits for n in nights]
    return OperationalStats(
        nights=len(nights),
        nights_with_actions=len(active),
        empty_nights_pct=100.0 - len(active) / len(nights) * 100.0 if nights else 0.0,
        avg_actions=statistics.fmean(actions_per_night) if actions_per_night else 0.0,
        max_actions=max(actions_per_night) if actions_per_night else 0,
        avg_entries_per_night=statistics.fmean(entries_per_night) if entries_per_night else 0.0,
        max_entries_in_one_night=max(entries_per_night) if entries_per_night else 0,
        total_entries=sum(n.entries for n in nights),
    )


def _etf_expectancy(etf_run: BacktestRun) -> float:
    normal = [r for r in etf_run.trades if not r.single]
    if not normal:
        return 0.0
    return statistics.fmean(r.r for r in normal)


def _expectancy_after_tax(rs: list[float], rate: float) -> float:
    """Expectativa si una fraccion ``rate`` de las ganancias se paga de impuestos."""
    if not rs:
        return 0.0
    adjusted = [(r * (1.0 - rate) if r > 0.0 else r) for r in rs]
    return statistics.fmean(adjusted)
