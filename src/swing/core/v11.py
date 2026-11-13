"""Reglas de la estrategia v11 (``docs/ESTRATEGIA_v11.md``): capa de riesgo de cuenta.

Funcion pura: recibe las filas del panel de cumplimiento ya armadas (en su forma de
diccionario, la misma que va al JSON del ``stats``) y el estado previo, y devuelve el
panel v11 y el estado nuevo. No toca disco ni red.

Alcance de esta fase: stop duro, break-even, pesos, riesgo, alarma de -20 %, numero de
posiciones, caja y tope escalonado del factor IA. **Pendientes** (necesitan datos que el
panel no trae): F-Score, ranking de momentum, trailing Chandelier, regla de promediar a
la baja (R9) y re-entrada (R11).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Collection, Mapping, Sequence
from typing import Any

from swing.core.calendar import SessionCalendar
from swing.core.models import (
    Cluster,
    Clusters,
    ComplianceConfig,
    FactorsConfig,
    StrategyConfig,
    V11Config,
)

STATUS_STOP = "STOP_DURO_TOCADO"
STATUS_OVERWEIGHT = "SOBRE_PESO"
STATUS_OVERRISK = "SOBRE_RIESGO"
STATUS_ALARM = "ALARMA_REEVALUAR"
STATUS_OK = "OK"

ANCHOR_ENTRY = "entry"
ANCHOR_TRANSITION = "transition"


def hard_stop_pct(atr_pct: float | None, cfg: V11Config) -> float:
    """Distancia del stop duro bajo el ancla: el menor entre el tope y mult x ATR %."""
    if atr_pct is None or atr_pct <= 0:
        return cfg.hard_stop_max_pct
    return min(cfg.hard_stop_max_pct, cfg.hard_stop_atr_mult * atr_pct)


def factor_cap(today: dt.date, cfg: V11Config) -> float:
    """Tope del factor IA vigente hoy (escalon mas reciente con ``since <= today``)."""
    cap = cfg.factor_schedule[0].max_pct
    for step in cfg.factor_schedule:
        if step.since <= today:
            cap = step.max_pct
    return cap


def _num(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if number == number else None  # descarta NaN


def should_persist(today: dt.date, cfg: V11Config) -> bool:
    """El ledger solo se escribe desde ``start_date``: antes, el panel es una vista previa."""
    return today >= cfg.start_date


def evaluate(
    positions: Sequence[Mapping[str, Any]],
    *,
    cfg: V11Config,
    today: dt.date,
    ledger: Mapping[str, Mapping[str, Any]],
    factor_tickers: Collection[str],
    cash_pct: float | None,
    drawdown_pct: float | None,
) -> dict[str, Any]:
    """Panel v11 por posicion y resumen, mas el ledger actualizado.

    ``ledger`` vacio = migracion: cada posicion abierta se ancla a su precio de hoy
    (stop nuevo desde hoy). Con ledger no vacio, un ticker nuevo se ancla a su entrada.
    """
    bootstrap = not ledger
    calendar = SessionCalendar.us()
    new_ledger: dict[str, dict[str, Any]] = {}
    rows: list[dict[str, Any]] = []

    for item in positions:
        ticker = str(item.get("ticker") or "")
        price = _num(item.get("price"))
        entry = _num(item.get("entry"))
        atr_pct = _num(item.get("atr_pct"))
        weight = _num(item.get("weight_equity_pct")) or 0.0
        if not ticker or price is None or price <= 0:
            # Sin precio hoy (fallo de descarga): la posicion sigue abierta, asi que su ancla y
            # su stop se conservan; si se descartaran, el stop "solo sube" se perderia.
            if ticker and ticker in ledger:
                new_ledger[ticker] = dict(ledger[ticker])
            continue

        stop_pct = hard_stop_pct(atr_pct, cfg)
        prior = dict(ledger.get(ticker) or {})
        if prior:
            anchor_kind = str(prior.get("anchor") or ANCHOR_ENTRY)
            anchor_price = float(prior["anchor_price"])
            anchor_date = str(prior.get("anchor_date") or today.isoformat())
            r_unit = float(prior["r_unit"])
            prior_stop = float(prior["hard_stop"])
            peak = max(float(prior.get("peak_price") or anchor_price), price)
            alarm_date = prior.get("alarm_date")
        else:
            use_entry = not bootstrap and entry is not None and entry > 0
            anchor_kind = ANCHOR_ENTRY if use_entry else ANCHOR_TRANSITION
            anchor_price = float(entry) if use_entry and entry is not None else price
            anchor_date = today.isoformat()
            r_unit = anchor_price * stop_pct / 100.0
            prior_stop = 0.0
            peak = max(anchor_price, price)
            alarm_date = None

        initial = anchor_price * (1.0 - stop_pct / 100.0)
        stop = max(prior_stop, initial)
        break_even = anchor_price + cfg.breakeven_r * r_unit
        if peak >= break_even:
            stop = max(stop, anchor_price)

        dist_pct = (price - stop) / price * 100.0
        risk_pct = weight * max(0.0, dist_pct) / 100.0
        ret_entry = (price / entry - 1.0) * 100.0 if entry is not None and entry > 0 else None
        max_open_weight = min(cfg.risk_per_position_pct / stop_pct * 100.0, cfg.max_weight_hold_pct)

        alarm = ret_entry is not None and ret_entry <= -cfg.alarm_loss_pct + 1e-9
        if alarm and not alarm_date:
            alarm_date = today.isoformat()
        elif not alarm:
            alarm_date = None
        alarm_sessions = (
            calendar.sessions_between(dt.date.fromisoformat(str(alarm_date)), today) if alarm_date else None
        )

        touched = price <= stop
        overweight = weight > cfg.max_weight_hold_pct
        overrisk = risk_pct > cfg.risk_per_position_pct
        flags = [
            flag
            for flag, on in (
                (STATUS_STOP, touched),
                (STATUS_OVERWEIGHT, overweight),
                (STATUS_OVERRISK, overrisk),
                (STATUS_ALARM, alarm),
            )
            if on
        ]
        rows.append(
            {
                "ticker": ticker,
                "price": price,
                "entry": entry,
                "atr_pct": atr_pct,
                "weight_equity_pct": weight,
                "ret_entry_pct": ret_entry,
                "anchor": anchor_kind,
                "anchor_price": anchor_price,
                "stop_pct": stop_pct,
                "hard_stop": stop,
                "stop_dist_pct": dist_pct,
                "risk_pct": risk_pct,
                "max_open_weight_pct": max_open_weight,
                "trim_to_pct": cfg.max_weight_hold_trim_pct if overweight else None,
                "alarm_sessions": alarm_sessions,
                "alarm_overdue": alarm_sessions is not None and alarm_sessions > cfg.alarm_review_sessions,
                "flags": flags,
                "status": flags[0] if flags else STATUS_OK,
            }
        )
        new_ledger[ticker] = {
            "anchor": anchor_kind,
            "anchor_price": anchor_price,
            "anchor_date": anchor_date,
            "r_unit": r_unit,
            "hard_stop": stop,
            "peak_price": peak,
            "alarm_date": alarm_date,
        }

    factor_set = set(factor_tickers)
    factor_pct = sum(r["weight_equity_pct"] for r in rows if r["ticker"] in factor_set)
    cap = factor_cap(today, cfg)
    open_risk = sum(r["risk_pct"] for r in rows)
    count = len(rows)
    defensive = drawdown_pct is not None and abs(drawdown_pct) >= cfg.defensive_drawdown_pct

    blocks: list[str] = []
    if count > cfg.max_positions:
        blocks.append(f"posiciones {count} > {cfg.max_positions}")
    if factor_pct > cap:
        blocks.append(f"factor {cfg.factor_name} {factor_pct:.1f}% > {cap:g}% (solo compras en el factor)")
    if open_risk > cfg.max_open_risk_pct:
        blocks.append(f"riesgo abierto {open_risk:.1f}% > {cfg.max_open_risk_pct:g}%")
    if defensive:
        blocks.append("modo defensivo por caida del patrimonio")

    summary = {
        "positions": count,
        "max_positions": cfg.max_positions,
        "open_risk_pct": open_risk,
        "max_open_risk_pct": cfg.max_open_risk_pct,
        "cash_pct": cash_pct,
        "cash_target_pct": cfg.cash_target_pct,
        "factor_name": cfg.factor_name,
        "factor_pct": factor_pct,
        "factor_cap_pct": cap,
        "drawdown_pct": drawdown_pct,
        "defensive": defensive,
        "stop_touched": [r["ticker"] for r in rows if STATUS_STOP in r["flags"]],
        "alarms": [r["ticker"] for r in rows if STATUS_ALARM in r["flags"]],
        "overweight": [r["ticker"] for r in rows if STATUS_OVERWEIGHT in r["flags"]],
        "overrisk": [r["ticker"] for r in rows if STATUS_OVERRISK in r["flags"]],
        "buy_blocks": blocks,
        "bootstrap": bootstrap,
        "pending": ["F-Score", "ranking de momentum", "trailing Chandelier", "R9 promediar", "R11 re-entrada"],
    }
    return {"positions": rows, "summary": summary, "ledger": new_ledger}


# --------------------------------------------------------------------------- #
# Unificacion de umbrales: v11.yaml manda sobre los valores v10 que se solapan
# --------------------------------------------------------------------------- #


def unify_strategy(
    strategy: StrategyConfig,
    cfg: V11Config,
    *,
    today: dt.date,
    factor_tickers: Collection[str] = (),
) -> StrategyConfig:
    """``strategy.yaml`` con los umbrales v11 aplicados (una sola fuente de verdad).

    ``model_copy`` no revalida: los valores nuevos se eligen para respetar los mismos
    limites que los validadores de ``RiskConfig`` y ``BriefingConfig``.
    """
    cap = factor_cap(today, cfg)
    risk = strategy.risk
    clusters = risk.clusters
    if clusters is not None:
        tickers = tuple(factor_tickers) or clusters.ia_ampliada.tickers
        clusters = Clusters(
            semis=Cluster(max_pct=cap, themes=clusters.semis.themes, tickers=clusters.semis.tickers),
            ia_ampliada=Cluster(max_pct=cap, themes=clusters.ia_ampliada.themes, tickers=tickers),
        )
    new_risk = risk.model_copy(
        update={
            "risk_per_trade_pct": cfg.risk_per_position_pct,
            "max_position_pct": cfg.max_weight_hold_pct,
            "max_theme_pct": cfg.max_theme_pct,
            "max_open_trades": cfg.max_positions,
            "max_heat_pct": cfg.max_open_risk_pct,
            "max_stop_distance_pct": cfg.hard_stop_max_pct,
            "cash_buffer_pct": 0.0,
            "clusters": clusters,
        }
    )
    update: dict[str, Any] = {
        "risk": new_risk,
        "legacy": strategy.legacy.model_copy(update={"max_loss_from_cost_pct": cfg.hard_stop_max_pct}),
    }
    if strategy.briefing is not None:
        update["briefing"] = strategy.briefing.model_copy(
            update={
                "position_max_pct": cfg.max_weight_hold_pct,
                "theme_max_pct": cfg.max_theme_pct,
                "ia_ampliada_max_pct": cap,
                "cash_min_pct": 0.0,
                "cash_max_pct": 100.0,
            }
        )
    return strategy.model_copy(update=update)


def unify_compliance(compliance: ComplianceConfig, cfg: V11Config, *, today: dt.date) -> ComplianceConfig:
    """``config.yaml`` (P1-P16) con los umbrales v11 aplicados.

    La caja deja de ser restriccion y el riesgo de las posiciones abiertas solo avisa (no
    genera recortes): el 2 % define el tamano al abrir.
    """
    return compliance.model_copy(
        update={
            "risk_per_open_pct": cfg.risk_per_position_pct,
            "max_weight_open_pct": cfg.max_weight_hold_pct,
            "max_weight_hold_pct": cfg.max_weight_hold_pct,
            "max_weight_hold_trim_pct": cfg.max_weight_hold_trim_pct,
            # R1 rige al abrir (risk_per_open_pct); en posiciones abiertas el riesgo solo avisa.
            "max_risk_position_pct": 100.0,
            "max_open_risk_pct": cfg.max_open_risk_pct,
            "max_factor_pct": factor_cap(today, cfg),
            "max_theme_pct": cfg.max_theme_pct,
            "min_cash_pct": 0.0,
            "cash_band_max_pct": 100.0,
            "stop_atr_mult": cfg.hard_stop_atr_mult,
            "stop_min_pct": 1.0,
            "stop_max_pct": cfg.hard_stop_max_pct,
            "defensive_drawdown_pct": cfg.defensive_drawdown_pct,
        }
    )


def unify_factors(factors: FactorsConfig, cfg: V11Config, *, today: dt.date) -> FactorsConfig:
    """``factors.yaml`` con el tope escalonado vigente para el factor IA."""
    cap = factor_cap(today, cfg)
    spec = factors.factors.get(cfg.factor_name)
    if spec is None:
        return factors
    new_factors = {**factors.factors, cfg.factor_name: spec.model_copy(update={"max_pct": cap})}
    return factors.model_copy(
        update={
            "factors": new_factors,
            "max_factor_pct": max(cap, factors.max_factor_pct),
            "max_theme_pct": cfg.max_theme_pct,
        }
    )
