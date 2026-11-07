"""Prueba de factibilidad del Paso 0b y calendario semanal de reduccion (v8).

Modulo **puro**: recibe las filas del panel ya valoradas y los limites P1-P13.
Simula que pasa con la caja, el factor y el tema si se cumplen todas las reglas,
semana a semana, sin tocar red ni disco.

La simulacion no decide nada por el usuario: hace visible el conflicto que el
prompt v8 pide declarar (por ejemplo, que reducir el factor deje la caja muy por
encima de la banda). Los recortes son del 25 % del tamano que la posicion tenia
al **inicio** de la reduccion, tal como dice la regla 5.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence

from swing.core.compliance import (
    STATUS_INHERITED_A,
    STATUS_INHERITED_B,
    ComplianceRow,
    ComplianceSummary,
)
from swing.core.models import ComplianceConfig, FactorsConfig

#: Semanas maximas que se simulan. Pasado eso el conflicto se declara, no se esconde.
MAX_WEEKS = 12

#: Temas que el prompt agrega como "semiconductores y equipos".
SEMIS_THEMES = frozenset({"semiconductors", "semiconductors_ai", "technology", "semis"})
SEMIS_LABEL = "semiconductores y equipos"


def build_feasibility(
    rows: Sequence[ComplianceRow],
    summary: ComplianceSummary,
    *,
    cfg: ComplianceConfig,
    factor_cfg: FactorsConfig | None,
    themes: Mapping[str, str],
    equity_clp: float | None,
    cash_clp: float | None,
    fx: float | None,
    today: dt.date,
) -> dict[str, object]:
    """Escenarios y calendario semanal de reduccion.

    Devuelve un mapping JSON-able con ``scenarios`` (tabla de escenarios) y
    ``calendar`` (una entrada por semana con las reducciones y el estado al cierre).
    """
    base = equity_clp if equity_clp is not None and equity_clp > 0.0 else (summary.equity_clp or 0.0)
    if base <= 0.0:
        return {}
    cash = cash_clp or 0.0
    values = {row.ticker: (row.value_clp or 0.0) for row in rows}
    prices = {row.ticker: row.price for row in rows}
    factor_names, factor_members = _factor_members(rows, factor_cfg, themes)
    semis_members = {row.ticker for row in rows if _semis(themes.get(row.ticker) or "")}
    factor_max = _factor_max(factor_cfg, cfg)
    theme_max = min(cfg.max_theme_pct, factor_cfg.max_theme_pct) if factor_cfg is not None else cfg.max_theme_pct
    p3_target = cfg.max_weight_hold_trim_pct
    priority = _priority(rows)
    risk_pcts = {row.ticker: row.risk_pct for row in rows}

    actual = _state(
        values,
        cash,
        base,
        factor_members,
        semis_members,
        factor_max,
        theme_max,
        cfg.min_cash_pct,
        cfg.cash_band_max_pct,
    )
    scenarios: list[dict[str, object]] = [
        {
            "name": "actual",
            "description": "Sin cambios.",
            **actual,
            "weeks": 0,
        }
    ]
    calendar_rows: list[dict[str, object]] = []

    # Escenario solo factor, solo tema y todo junto. P3/P4 se recorta de inmediato
    # (regla 3), por eso no consume semanas.
    for name, description, do_factor, do_theme, do_weight in (
        ("solo_factor", "Reducir el factor hasta P6 (25 % semanal).", True, False, False),
        ("solo_tema", "Reducir el mayor tema hasta P6 (25 % semanal).", False, True, False),
        ("todo", "Cumplir factor, tema y P3/P4; la caja puede superar la banda.", True, True, True),
    ):
        work = dict(values)
        work_cash = cash
        calendar, work_cash = _simulate(
            work,
            work_cash,
            base,
            priority,
            prices,
            factor_members if do_factor else set(),
            semis_members if do_theme else set(),
            factor_max,
            theme_max,
            p3_target,
            risk_pcts,
            cfg,
            do_weight,
            today,
            fx,
        )
        final = _state(
            work,
            work_cash,
            base,
            factor_members,
            semis_members,
            factor_max,
            theme_max,
            cfg.min_cash_pct,
            cfg.cash_band_max_pct,
        )
        week_numbers: list[int] = []
        for item in calendar:
            week = item.get("week")
            if isinstance(week, (int, float)):
                week_numbers.append(int(week))
        scenarios.append(
            {
                "name": name,
                "description": description,
                **final,
                "weeks": max(week_numbers, default=0),
            }
        )
        if name == "todo":
            calendar_rows = calendar

    todo = next((item for item in scenarios if item.get("name") == "todo"), None)
    note = (
        "La caja puede superar la banda mientras una regla obligue a reducir "
        "(regla 3.7 v8); el conflicto se declara, no se corrige cambiando la regla."
    )
    if todo is not None and bool(todo.get("cash_over_band")):
        cash_after = todo.get("cash_pct")
        cash_after_value = float(cash_after) if isinstance(cash_after, (int, float)) else 0.0
        note += (
            f" Con todas las reglas la caja queda en {cash_after_value:.2f} %, "
            f"sobre la banda de {cfg.cash_band_max_pct:g} %."
        )
    if todo is not None and bool(todo.get("factor_ok")) and bool(todo.get("theme_ok")):
        note += " El calendario semanal alcanza los topes P6."
    return {
        "base_p12_clp": base,
        "cash_clp": cash,
        "factor_name": ", ".join(factor_names) or "sin factor configurado",
        "factor_members": sorted(factor_members),
        "aggregate_theme": SEMIS_LABEL,
        "aggregate_theme_members": sorted(semis_members),
        "limits": {
            "factor_pct": factor_max,
            "theme_pct": theme_max,
            "cash_min_pct": cfg.min_cash_pct,
            "cash_band_max_pct": cfg.cash_band_max_pct,
            "weight_hold_pct": cfg.max_weight_hold_pct,
            "weight_trim_pct": cfg.max_weight_hold_trim_pct,
        },
        "scenarios": scenarios,
        "calendar": calendar_rows,
        "note": note,
    }


def _factor_members(
    rows: Sequence[ComplianceRow],
    factor_cfg: FactorsConfig | None,
    themes: Mapping[str, str],
) -> tuple[tuple[str, ...], set[str]]:
    if factor_cfg is None:
        return (), set()
    names: list[str] = []
    members: set[str] = set()
    for name, spec in factor_cfg.factors.items():
        names.append(name)
        for row in rows:
            theme = themes.get(row.ticker, "")
            if row.ticker in spec.tickers or (theme and theme in spec.themes):
                members.add(row.ticker)
    return tuple(names), members


def _factor_max(factor_cfg: FactorsConfig | None, cfg: ComplianceConfig) -> float:
    if factor_cfg is None:
        return cfg.max_factor_pct
    return min(cfg.max_factor_pct, factor_cfg.max_factor_pct, *(spec.max_pct for spec in factor_cfg.factors.values()))


def _semis(theme: str) -> bool:
    return theme.strip().lower() in SEMIS_THEMES


def _priority(rows: Sequence[ComplianceRow]) -> list[str]:
    """Orden de la regla 5: primero bajo/cerca del stop, luego mayor peso."""
    return [
        row.ticker
        for row in sorted(
            rows,
            key=lambda row: (
                0 if row.status in {STATUS_INHERITED_A, STATUS_INHERITED_B} else 1,
                row.stop_dist_price_pct if row.stop_dist_price_pct is not None else 999.0,
                -(row.weight_equity_pct or 0.0),
            ),
        )
    ]


def _state(
    values: Mapping[str, float],
    cash: float,
    base: float,
    factor_members: set[str],
    semis_members: set[str],
    factor_max: float,
    theme_max: float,
    cash_min: float,
    cash_max: float,
) -> dict[str, object]:
    factor = sum(values.get(ticker, 0.0) for ticker in factor_members) / base * 100.0
    theme = sum(values.get(ticker, 0.0) for ticker in semis_members) / base * 100.0
    cash_pct = cash / base * 100.0
    return {
        "factor_pct": factor,
        "factor_ok": factor <= factor_max + 1e-9,
        "factor_excess_pct": max(0.0, factor - factor_max),
        "theme_pct": theme,
        "theme_ok": theme <= theme_max + 1e-9,
        "theme_excess_pct": max(0.0, theme - theme_max),
        "cash_pct": cash_pct,
        "cash_min_pct": cash_min,
        "cash_band_max_pct": cash_max,
        "cash_ok": cash_pct >= cash_min - 1e-9,
        "cash_over_band": cash_pct > cash_max + 1e-9,
    }


def _simulate(
    values: dict[str, float],
    cash: float,
    base: float,
    priority: Sequence[str],
    prices: Mapping[str, float | None],
    factor_members: set[str],
    semis_members: set[str],
    factor_max: float,
    theme_max: float,
    p3_target: float,
    risk_pcts: Mapping[str, float | None],
    cfg: ComplianceConfig,
    do_weight: bool,
    today: dt.date,
    fx: float | None,
) -> tuple[list[dict[str, object]], float]:
    calendar: list[dict[str, object]] = []
    base_values: dict[str, float] = {}
    if do_weight:
        immediate: list[dict[str, object]] = []
        for ticker in priority:
            value = values.get(ticker, 0.0)
            if value <= 0.0:
                continue
            weight = value / base * 100.0
            p3_reduction = 0.0
            if weight > cfg.max_weight_hold_pct:
                target = base * p3_target / 100.0
                p3_reduction = max(0.0, value - target)
            risk = risk_pcts.get(ticker)
            p4_reduction = 0.0
            if risk is not None and risk > cfg.max_risk_position_pct and cfg.max_risk_position_pct > 0.0:
                target_value = value * cfg.max_risk_position_pct / risk
                p4_reduction = max(0.0, value - target_value)
            reduction = max(p3_reduction, p4_reduction)
            if reduction <= 0.0:
                continue
            reason = "P3 inmediato" if p3_reduction >= p4_reduction else "P4 inmediato"
            values[ticker] = value - reduction
            cash += reduction
            immediate.append(
                {
                    "ticker": ticker,
                    "shares": _shares(reduction, prices.get(ticker), fx),
                    "value_clp": reduction,
                    "reason": reason,
                }
            )
        if immediate:
            calendar.append(
                {
                    "week": 0,
                    "date": today.isoformat(),
                    "reductions": immediate,
                    **_state(
                        values,
                        cash,
                        base,
                        factor_members,
                        semis_members,
                        factor_max,
                        theme_max,
                        cfg.min_cash_pct,
                        cfg.cash_band_max_pct,
                    ),
                }
            )

    for week in range(1, MAX_WEEKS + 1):
        factor = sum(values.get(ticker, 0.0) for ticker in factor_members) / base * 100.0
        theme = sum(values.get(ticker, 0.0) for ticker in semis_members) / base * 100.0
        if factor <= factor_max + 1e-9 and theme <= theme_max + 1e-9:
            break
        reductions: list[dict[str, object]] = []
        for ticker in priority:
            value = values.get(ticker, 0.0)
            if value <= 0.0:
                continue
            factor = sum(values.get(t, 0.0) for t in factor_members) / base * 100.0
            theme = sum(values.get(t, 0.0) for t in semis_members) / base * 100.0
            need_factor = ticker in factor_members and factor > factor_max + 1e-9
            need_theme = ticker in semis_members and theme > theme_max + 1e-9
            if not (need_factor or need_theme):
                continue
            start = base_values.setdefault(ticker, value)
            reduction = min(value, start * 0.25)
            if reduction <= 0.0:
                continue
            values[ticker] = value - reduction
            cash += reduction
            reductions.append(
                {
                    "ticker": ticker,
                    "shares": _shares(reduction, prices.get(ticker), fx),
                    "value_clp": reduction,
                    "reason": _reason(need_factor, need_theme),
                }
            )
        if not reductions:
            break
        state = _state(
            values,
            cash,
            base,
            factor_members,
            semis_members,
            factor_max,
            theme_max,
            cfg.min_cash_pct,
            cfg.cash_band_max_pct,
        )
        calendar.append(
            {
                "week": week,
                "date": (today + dt.timedelta(days=7 * week)).isoformat(),
                "reductions": reductions,
                **state,
            }
        )
    return calendar, cash


def _reason(factor: bool, theme: bool) -> str:
    if factor and theme:
        return "factor + tema"
    return "factor" if factor else "tema"


def _shares(value_clp: float, price_usd: float | None, fx: float | None) -> float | None:
    if price_usd is None or price_usd <= 0.0 or fx is None or fx <= 0.0:
        return None
    return value_clp / (price_usd * fx)
