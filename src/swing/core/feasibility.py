"""Prueba de factibilidad del Paso 0b y calendario unico de reduccion (v10).

Modulo **puro**: recibe las filas del panel ya valoradas y los limites P1-P16.
Simula que pasa con la caja, el factor y el tema si se cumplen todas las reglas,
semana a semana, sin tocar red ni disco.

El calendario es uno solo por activo (v10, 3.13): el tamano objetivo es el
**menor** entre adopcion, P3/P4, tope de factor/tema y P13, y la venta se calcula
una sola vez aunque sirva a varias reglas. Las ventas inmediatas de 3.3 cuentan
como primer tramo; el resto va en tramos de 25 % del tamano inicial, un tramo por
lunes. La simulacion no decide nada por el usuario: hace visible el conflicto que
el prompt pide declarar (por ejemplo, que reducir el factor deje la caja muy por
encima de la banda).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from swing.core.calendar import SessionCalendar
from swing.core.compliance import (
    STATUS_INHERITED_A,
    STATUS_INHERITED_B,
    STATUS_STOPPED,
    ComplianceRow,
    ComplianceSummary,
)
from swing.core.models import ComplianceConfig, FactorsConfig

#: Semanas maximas que se simulan. El prompt v9 fija P16 = 4; el tope de 12 evita
#: un bucle infinito si una configuracion futura pide un plazo mayor.
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
    priority = _priority(rows)

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

    # Escenario solo factor, solo tema y todo junto. El calendario unico es el de
    # "todo": adopcion + P3/P4/P13 + topes de factor/tema, con un solo objetivo por
    # activo (el menor) y tramos de 25 % del tamano inicial por lunes.
    for name, description, do_factor, do_theme, do_weight, do_adoption in (
        ("solo_factor", "Reducir el factor hasta P6 (25 % semanal).", True, False, False, False),
        ("solo_tema", "Reducir el mayor tema hasta P6 (25 % semanal).", False, True, False, False),
        (
            "todo",
            "Adopcion + P3/P4/P13 + factor/tema; la caja puede superar la banda.",
            True,
            True,
            True,
            True,
        ),
    ):
        work = dict(values)
        work_cash = cash
        calendar, work_cash = _simulate(
            work,
            work_cash,
            base,
            priority,
            prices,
            factor_members,
            semis_members,
            factor_max,
            theme_max,
            cfg,
            do_weight,
            do_factor,
            do_theme,
            do_adoption,
            rows,
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
        "Un solo calendario por activo: tamano objetivo = el menor entre adopcion, P3/P4, "
        "tope de factor/tema y P13. Las ventas inmediatas de 3.3 cuentan como primer tramo; "
        "el resto va en tramos de 25 % del tamano inicial, un tramo por lunes (P16). "
        "La caja puede superar la banda mientras una regla obligue a reducir (regla 3.7 v9); "
        "el conflicto se declara, no se corrige cambiando la regla."
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
            "max_sale_per_session_pct": cfg.max_sale_per_session_pct,
            "reduction_max_weeks": cfg.reduction_max_weeks,
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


@dataclass(frozen=True, slots=True)
class _AssetPlan:
    """Objetivo unico por activo y sus causas (v10, 3.13 y Paso 0c)."""

    ticker: str
    start: float
    immediate_target: float
    final_target: float
    immediate_causes: tuple[str, ...]
    deferred_causes: tuple[str, ...]
    priority_note: str


def _plan_assets(
    rows: Sequence[ComplianceRow],
    base: float,
    cfg: ComplianceConfig,
    *,
    priority: Sequence[str],
    factor_members: set[str],
    semis_members: set[str],
    factor_max: float,
    theme_max: float,
    do_weight: bool,
    do_factor: bool,
    do_theme: bool,
    do_adoption: bool,
) -> dict[str, _AssetPlan]:
    """Calcula un solo objetivo por activo: el menor de adopcion, P3/P4, P13 y topes.

    Antes habia dos calendarios (topes de peso/riesgo y factor/tema) que se sumaban
    en la practica. Aqui el objetivo final es el **menor** y la venta de cada activo
    se calcula una sola vez, aunque sirva a varias reglas (v10 ap. A.3).
    """
    plans: dict[str, _AssetPlan] = {}
    for row in rows:
        start = row.value_clp or 0.0
        if start <= 0.0:
            continue
        immediate_causes: list[str] = []
        deferred_causes: list[str] = []

        # P13 y stop registrado tocado: salida, no venta discrecional.
        if not row.stop_viable:
            immediate_target = 0.0
            immediate_causes.append("P13 sin stop viable")
        elif row.status == STATUS_STOPPED:
            immediate_target = 0.0
            immediate_causes.append("stop tocado")
        else:
            immediate_target = start

        if do_weight:
            if row.weight_equity_pct is not None and row.weight_equity_pct > cfg.max_weight_hold_pct:
                target = base * cfg.max_weight_hold_trim_pct / 100.0
                if target < immediate_target:
                    immediate_target = target
                    immediate_causes.append("P3")
            risk = row.risk_pct
            if (
                risk is not None
                and cfg.max_risk_position_pct > 0.0
                and risk > cfg.max_risk_position_pct
            ):
                target = start * cfg.max_risk_position_pct / risk
                if target < immediate_target:
                    immediate_target = target
                    immediate_causes.append("P4")

        adoption_target = start
        if (
            do_adoption
            and row.inherited
            and row.new_entry_clp is not None
            and 0.0 < row.new_entry_clp < start
        ):
            adoption_target = row.new_entry_clp
            if adoption_target < immediate_target:
                deferred_causes.append("adopcion")

        final_target = max(0.0, min(start, immediate_target, adoption_target))
        plans[row.ticker] = _AssetPlan(
            ticker=row.ticker,
            start=start,
            immediate_target=max(0.0, min(start, immediate_target)),
            final_target=final_target,
            immediate_causes=tuple(dict.fromkeys(immediate_causes)),
            deferred_causes=tuple(dict.fromkeys(deferred_causes)),
            priority_note=_priority_note(row),
        )

    # Topes agregados: se reparten en el orden de prioridad de 3.5 y bajan el
    # mismo objetivo, no uno nuevo. Si un activo pertenece a factor y tema, la
    # venta se cuenta una sola vez (el objetivo baja una vez).
    caps: list[tuple[set[str], float, str]] = []
    if do_factor and factor_max > 0.0:
        caps.append((factor_members, factor_max, "factor"))
    if do_theme and theme_max > 0.0:
        caps.append((semis_members, theme_max, "tema"))
    targets, cap_flags = _apply_caps(
        {ticker: plan.final_target for ticker, plan in plans.items()},
        priority,
        caps,
        base,
    )
    out: dict[str, _AssetPlan] = {}
    for ticker, plan in plans.items():
        causes = list(plan.deferred_causes)
        for label in ("factor", "tema"):
            if label in cap_flags.get(ticker, set()):
                causes.append(label)
        out[ticker] = _AssetPlan(
            ticker=plan.ticker,
            start=plan.start,
            immediate_target=plan.immediate_target,
            final_target=targets.get(ticker, plan.final_target),
            immediate_causes=plan.immediate_causes,
            deferred_causes=tuple(dict.fromkeys(causes)),
            priority_note=plan.priority_note,
        )
    return out


def _apply_caps(
    targets: dict[str, float],
    priority: Sequence[str],
    caps: Sequence[tuple[set[str], float, str]],
    base: float,
) -> tuple[dict[str, float], dict[str, set[str]]]:
    """Baja los objetivos hasta cumplir los topes agregados, en orden de prioridad."""
    flags: dict[str, set[str]] = {}
    if not caps or base <= 0.0:
        return targets, flags
    for _ in range(len(caps) + 3):
        excesses = [
            (max(0.0, sum(targets.get(ticker, 0.0) for ticker in members) / base * 100.0 - cap)
             / 100.0 * base,
             members,
             label)
            for members, cap, label in caps
        ]
        excesses.sort(key=lambda item: item[0], reverse=True)
        need, members, label = excesses[0]
        if need <= 1e-9:
            break
        for ticker in priority:
            if ticker not in members:
                continue
            cut = min(targets.get(ticker, 0.0), need)
            if cut <= 1e-9:
                continue
            targets[ticker] -= cut
            flags.setdefault(ticker, set()).add(label)
            need -= cut
            if need <= 1e-9:
                break
        else:  # no habia objetivo que recortar: evita un bucle infinito
            break
    return targets, flags


def _priority_note(row: ComplianceRow) -> str:
    """Por que esta posicion entra antes en el orden de 3.5."""
    if not row.stop_viable:
        return "sin stop viable"
    if row.status == STATUS_STOPPED:
        return "stop tocado"
    dist = row.stop_dist_price_pct
    if dist is not None and dist < 0.0:
        return "bajo stop teorico"
    if dist is not None and dist <= 3.0:
        return "cerca del stop"
    return ""


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
    cfg: ComplianceConfig,
    do_weight: bool,
    do_factor: bool,
    do_theme: bool,
    do_adoption: bool,
    rows: Sequence[ComplianceRow],
    today: dt.date,
    fx: float | None,
) -> tuple[list[dict[str, object]], float]:
    calendar: list[dict[str, object]] = []
    plans = _plan_assets(
        rows,
        base,
        cfg,
        priority=priority,
        factor_members=factor_members,
        semis_members=semis_members,
        factor_max=factor_max,
        theme_max=theme_max,
        do_weight=do_weight,
        do_factor=do_factor,
        do_theme=do_theme,
        do_adoption=do_adoption,
    )

    # Semana 0: ventas inmediatas de P3/P4/P13. Cuentan como el primer tramo.
    immediate: list[dict[str, object]] = []
    for ticker in priority:
        plan = plans.get(ticker)
        if plan is None:
            continue
        value = values.get(ticker, 0.0)
        reduction = max(0.0, value - plan.immediate_target)
        if reduction <= 1e-9:
            continue
        values[ticker] = value - reduction
        cash += reduction
        immediate.append(
            _reduction(plan, reduction, prices, fx, plan.immediate_causes)
        )
    if immediate:
        calendar.append(
            {
                "week": 0,
                "date": _week_date(today, 0).isoformat(),
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

    # P16: un tramo por lunes, 25 % del tamano inicial como maximo, hasta el
    # objetivo final (que ya incorpora adopcion, P3/P4/P13 y topes).
    max_weeks = max(1, min(MAX_WEEKS, int(cfg.reduction_max_weeks)))
    for week in range(1, max_weeks + 1):
        reductions: list[dict[str, object]] = []
        for ticker in priority:
            plan = plans.get(ticker)
            if plan is None:
                continue
            value = values.get(ticker, 0.0)
            remaining = value - plan.final_target
            if remaining <= 1e-9:
                continue
            reduction = min(remaining, plan.start * 0.25)
            if reduction <= 1e-9:
                continue
            values[ticker] = value - reduction
            cash += reduction
            reductions.append(_reduction(plan, reduction, prices, fx, plan.deferred_causes))
        if not reductions:
            break
        # Si no hubo venta inmediata, el primer tramo cae en la proxima sesion
        # (el primer lunes); si la hubo, esa venta ya fue el primer tramo.
        date_week = week if immediate else week - 1
        calendar.append(
            {
                "week": week,
                "date": _week_date(today, date_week).isoformat(),
                "reductions": reductions,
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
    return calendar, cash


def _reduction(
    plan: _AssetPlan,
    value_clp: float,
    prices: Mapping[str, float | None],
    fx: float | None,
    causes: Sequence[str],
) -> dict[str, object]:
    reason = " + ".join(causes) if causes else "ajuste"
    # La nota de prioridad puede repetir la causa (p. ej. "sin stop viable" y
    # "P13 sin stop viable"): si ya esta contenida, no se repite.
    note = plan.priority_note
    if note and note.lower() in reason.lower():
        note = ""
    why = "; ".join(part for part in (note, reason) if part)
    return {
        "ticker": plan.ticker,
        "shares": _shares(value_clp, prices.get(plan.ticker), fx),
        "value_clp": value_clp,
        "reason": reason,
        "why": why,
    }


def _week_date(today: dt.date, week: int) -> dt.date:
    """Lunes de ejecucion: primera sesion desde hoy, y luego +7 dias por semana.

    Se ancla al primer lunes y se avanza en dias de calendario, no en 5 ruedas:
    con un feriado (Thanksgiving) el conteo de ruedas corria el tramo a martes.
    """
    calendar = SessionCalendar.us()
    first = calendar.next_session(today)
    while first.weekday() != 0:
        first = calendar.next_session(first)
    target = first + dt.timedelta(days=7 * week)
    if calendar.is_session(target):
        return target
    return calendar.next_session(target - dt.timedelta(days=1))


def _shares(value_clp: float, price_usd: float | None, fx: float | None) -> float | None:
    if price_usd is None or price_usd <= 0.0 or fx is None or fx <= 0.0:
        return None
    return value_clp / (price_usd * fx)
