"""Bloque determinista del informe de ``stats``: banderas, stop tecnico y alertas.

Traduce la politica de la revision manual (``strategy.briefing``) a hechos
reproducibles para que no haya que recalcularlos a mano ni delegarlos a un modelo:
banderas por posicion, stop tecnico con su regla, take profit y riesgo, mas las
alertas de concentracion de la cartera.

**No decide comprar ni vender.** El veredicto queda en la revision. Es puro (no toca
red, disco ni reloj) y best effort: lo que falte sale ``None``/``"-"``, nunca un cero
inventado. Las unidades siguen al precio de la fila (USD), de modo que stop y
soportes comparten escala.
"""

from __future__ import annotations

import math
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from swing.core.verdict import compute_levels

if TYPE_CHECKING:
    from swing.core.models import BriefingConfig, ReviewConfig, ReviewLevelsConfig
    from swing.core.stats import PositionStats
    from swing.core.verdict import Levels

#: Sufijo del take profit segun de donde salio el nivel. Para un horizonte de meses
#: el objetivo del analista y el maximo de 52 semanas son niveles de tesis; el maximo
#: de 20/50 sesiones es solo el techo del rango corto, no un objetivo de la revision.
_TP_TARGET = "objetivo medio"
_TP_BAND = "max 52S"
_TP_RANGE = "techo de rango (corto plazo)"
_TP_NONE = "sin techo claro"

#: P/B (y PEG) por encima de esto apuntan a una unidad incoherente entre fuentes.
_PB_SUSPECT = 100.0
_PEG_SUSPECT = 20.0

#: Nombre del cajon de posiciones sin tema asignado: no es un tema y no alerta.
_NO_THEME = frozenset({"sin_tema", ""})


@dataclass(frozen=True, slots=True)
class PositionBrief:
    """Hechos deterministas de una posicion para la revision manual.

    Los montos van en la moneda del precio de la fila (USD o CLP). ``stop`` es el
    stop **tecnico** de la revision, no el del motor: se identifica con ``stop_rule``.
    """

    ticker: str
    flags: tuple[str, ...] = ()
    flags_technical: tuple[str, ...] = ()
    flags_fundamental: tuple[str, ...] = ()
    flags_context: tuple[str, ...] = ()
    stop: float | None = None
    stop_dist_pct: float | None = None
    stop_rule: str = ""
    risk_pct: float | None = None
    take_profit: float | None = None
    tp_label: str = ""
    tp_distance_pct: float | None = None
    tp_rr: float | None = None
    tp_note: str = ""
    """Ajuste aplicado al TP v6 (tope, techo por R:R o excepcion declarada)."""
    sl_type: str = "A"
    """``A`` (una alerta) o ``B`` (dos alertas: vigilancia y catastrofe)."""
    alarm_1: float | None = None
    alarm_1_dist_pct: float | None = None
    alarm_1_entry_dist_pct: float | None = None
    alarm_1_rule: str = ""
    alarm_2: float | None = None
    alarm_2_dist_pct: float | None = None
    alarm_2_entry_dist_pct: float | None = None
    risk_theoretical_pct: float | None = None
    """Riesgo % del patrimonio a la alerta que manda (la 2 en Tipo B)."""
    sessions_open: float | None = None
    levels: Levels | None = None
    """Niveles de ``PROMPT PORTAFOLIO`` (revision/alarma/zona/objetivo/piso)."""


@dataclass(frozen=True, slots=True)
class Briefing:
    """El bloque "Resumen determinista": posiciones, alertas y agregados de topes."""

    positions: tuple[PositionBrief, ...] = ()
    alerts: tuple[str, ...] = ()
    position_max_pct: float | None = None
    theme_max_pct: float | None = None
    cash_pct: float | None = None
    cash_min_pct: float | None = None
    cash_max_pct: float | None = None
    semis_pct: float | None = None
    semis_max_pct: float | None = None
    ia_pct: float | None = None
    ia_max_pct: float | None = None
    liquidity_tp_pct: float | None = None
    """% de la cartera invertida con TP a menos de ``liquidity_near_tp_pct`` del precio."""
    liquidity_cash_clp: float | None = None
    """Caja (CLP) que generaria la ejecucion de todos los TP con precio conocido."""
    liquidity_near_tp_pct: float | None = None


def brief_positions(
    rows: Sequence[PositionStats],
    *,
    patrimonio_clp: float | None,
    policy: BriefingConfig,
    review: ReviewConfig | None = None,
    semis_tickers: Collection[str] = (),
) -> tuple[PositionBrief, ...]:
    """Hechos deterministas por posicion (banderas, stop tecnico, take profit y riesgo)."""
    semis = frozenset(semis_tickers)
    return tuple(
        _brief(row, patrimonio_clp=patrimonio_clp, policy=policy, review=review, semis_tickers=semis)
        for row in rows
    )


def build(
    rows: Sequence[PositionStats],
    *,
    panel: Mapping[str, object],
    patrimonio_clp: float | None,
    policy: BriefingConfig,
    review: ReviewConfig | None = None,
    semis_tickers: Collection[str] = (),
) -> Briefing:
    """Arma el bloque determinista a partir de las filas ya enriquecidas y el panel."""
    positions = brief_positions(
        rows,
        patrimonio_clp=patrimonio_clp,
        policy=policy,
        review=review,
        semis_tickers=semis_tickers,
    )
    semis = _cluster(panel, "semis")
    ia = _cluster(panel, "ia_ampliada")
    liquidity_pct, liquidity_cash = _liquidity(rows, positions, policy)
    return Briefing(
        positions=positions,
        alerts=_alerts(rows, panel=panel, policy=policy, positions=positions),
        position_max_pct=policy.position_max_pct,
        theme_max_pct=policy.theme_max_pct,
        cash_pct=_num(panel.get("cash_pct")),
        cash_min_pct=policy.cash_min_pct,
        cash_max_pct=policy.cash_max_pct,
        semis_pct=semis.get("pct"),
        semis_max_pct=semis.get("max_pct"),
        ia_pct=ia.get("pct"),
        ia_max_pct=ia.get("max_pct"),
        liquidity_tp_pct=liquidity_pct,
        liquidity_cash_clp=liquidity_cash,
        liquidity_near_tp_pct=policy.liquidity_near_tp_pct,
    )


def _liquidity(
    rows: Sequence[PositionStats],
    positions: Sequence[PositionBrief],
    policy: BriefingConfig,
) -> tuple[float | None, float | None]:
    """Mapa de liquidez: % invertido con TP cerca y caja si se ejecutan todos los TP."""
    by_ticker = {brief.ticker: brief for brief in positions}
    invested = 0.0
    near = 0.0
    cash = 0.0
    for row in rows:
        value = _num(row.context.get("value_clp"))
        if value is None or value <= 0.0:
            continue
        invested += value
        brief = by_ticker.get(row.ticker)
        if brief is None or brief.take_profit is None or brief.tp_distance_pct is None:
            continue
        if brief.tp_distance_pct <= policy.liquidity_near_tp_pct:
            near += value
        price = _price(row.context)
        if price is not None and price > 0.0:
            cash += value * brief.take_profit / price
    pct = near / invested * 100.0 if invested > 0.0 else None
    return pct, (cash if cash > 0.0 else None)


def _cluster(panel: Mapping[str, object], name: str) -> dict[str, float | None]:
    clusters = panel.get("clusters")
    if not isinstance(clusters, Mapping):
        return {}
    raw = clusters.get(name)
    if not isinstance(raw, Mapping):
        return {}
    return {"pct": _num(raw.get("pct")), "max_pct": _num(raw.get("max_pct"))}


def _brief(
    row: PositionStats,
    *,
    patrimonio_clp: float | None,
    policy: BriefingConfig,
    review: ReviewConfig | None = None,
    semis_tickers: Collection[str] = (),
) -> PositionBrief:
    context = row.context
    fields = row.fields
    price = _price(context)
    entry = _num(context.get("entry_usd"))
    value = _num(context.get("value_clp"))
    plan = _stop_plan(fields, price=price, policy=policy)
    risk: float | None = None
    if plan.stop is not None and price is not None and value is not None and patrimonio_clp and patrimonio_clp > 0.0:
        risk = (value * (price - plan.stop) / price) / patrimonio_clp * 100.0
    levels = compute_levels(
        fields,
        price=price,
        value_clp=value,
        patrimonio_clp=patrimonio_clp,
        weight_invested_pct=_num(context.get("weight_invested_pct")),
        position_max_pct=policy.position_max_pct,
        earnings_sessions=_num(context.get("earnings_sessions")),
        entry_usd=entry,
        policy=review,
    )
    flags, technical, fundamental, contextual = _flag_groups(context, fields, price=price, policy=policy)
    sl_type = _sl_type(fields, row.ticker, policy=policy, semis_tickers=semis_tickers)
    alarm_1, alarm_1_rule = _alarm_1(levels, fields, price=price, entry=entry, ret=_ret_pct(context), policy=policy, review=review)
    alarm_2 = _alarm_2(levels, fields, price=price, entry=entry, review=review) if sl_type == "B" else None
    if alarm_1 is not None and alarm_2 is not None and alarm_2 > alarm_1:
        alarm_2 = alarm_1
    risk_alert = alarm_2 if sl_type == "B" and alarm_2 is not None else alarm_1
    risk_theoretical: float | None = None
    if risk_alert is not None and price is not None and value is not None and patrimonio_clp and patrimonio_clp > 0.0:
        risk_theoretical = (value * (price - risk_alert) / price) / patrimonio_clp * 100.0
    take_profit, tp_label, tp_note = _take_profit(
        fields,
        price=price,
        sl_type=sl_type,
        alarm_1=alarm_1,
        policy=policy,
    )
    tp_distance: float | None = None
    tp_rr: float | None = None
    if take_profit is not None and price is not None and price > 0.0:
        tp_distance = (take_profit / price - 1.0) * 100.0
        if alarm_1 is not None and alarm_1 < price:
            tp_rr = (take_profit - price) / (price - alarm_1)
    if tp_rr is not None and tp_rr < policy.take_profit_min_rr:
        contextual = (*contextual, f"R:R TP<{policy.take_profit_min_rr:g}")
        flags = (*flags, f"R:R TP<{policy.take_profit_min_rr:g}")
    return PositionBrief(
        ticker=row.ticker,
        flags=flags,
        flags_technical=technical,
        flags_fundamental=fundamental,
        flags_context=contextual,
        stop=plan.stop,
        stop_dist_pct=plan.distance_pct,
        stop_rule=plan.rule,
        risk_pct=risk,
        take_profit=take_profit,
        tp_label=tp_label,
        tp_distance_pct=tp_distance,
        tp_rr=tp_rr,
        tp_note=tp_note,
        sl_type=sl_type,
        alarm_1=alarm_1,
        alarm_1_dist_pct=_distance(price, alarm_1),
        alarm_1_entry_dist_pct=_distance(entry, alarm_1),
        alarm_1_rule=alarm_1_rule,
        alarm_2=alarm_2,
        alarm_2_dist_pct=_distance(price, alarm_2),
        alarm_2_entry_dist_pct=_distance(entry, alarm_2),
        risk_theoretical_pct=risk_theoretical,
        sessions_open=_num(context.get("sessions_open")),
        levels=levels,
    )


def _flag_groups(
    context: Mapping[str, object],
    fields: Mapping[str, object],
    *,
    price: float | None,
    policy: BriefingConfig,
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """Banderas en el orden historico, mas su clasificacion v6.

    Devuelve ``(todas, tecnicas, fundamentales/valorizacion, contexto)``. El orden de
    ``todas`` conserva las banderas que ya consumia el informe para no romper el JSON.
    """
    entries: list[tuple[str, str]] = []
    ret = _ret_pct(context)
    if ret is not None and ret > policy.gain_flag_pct:
        entries.append((f"ganancia>{policy.gain_flag_pct:g}%", "context"))
    if ret is not None and ret < -policy.loss_flag_pct:
        entries.append((f"perdida>{policy.loss_flag_pct:g}%", "context"))

    vs200 = _num(fields.get("price_vs_sma200_pct"))
    if vs200 is not None and vs200 < 0.0:
        entries.append(("bajo SMA200", "technical"))

    rsi = _num(fields.get("rsi"))
    if rsi is not None and rsi > policy.rsi_overbought:
        entries.append((f"RSI>{policy.rsi_overbought:g}", "technical"))
    if rsi is not None and rsi < policy.rsi_oversold:
        entries.append((f"RSI<{policy.rsi_oversold:g}", "technical"))
    rsi_pct = _num(fields.get("rsi_percentile"))
    if rsi_pct is not None and rsi_pct > policy.rsi_percentile_high:
        entries.append((f"RSI pct>{policy.rsi_percentile_high:g}", "technical"))
    if rsi_pct is not None and rsi_pct < policy.rsi_percentile_low:
        entries.append((f"RSI pct<{policy.rsi_percentile_low:g}", "technical"))

    atr_pct = _num(fields.get("atr_pct"))
    if atr_pct is not None and atr_pct > policy.atr_flag_pct:
        entries.append((f"ATR%>{policy.atr_flag_pct:g}", "technical"))
    atr_pctile = _num(fields.get("atr_pct_percentile"))
    if atr_pctile is not None and atr_pctile > policy.atr_percentile_high:
        entries.append((f"ATR% pct>{policy.atr_percentile_high:g}", "technical"))

    rs1 = _num(fields.get("rs_1m_pct"))
    rs3 = _num(fields.get("rs_3m_pct"))
    rs6 = _num(fields.get("rs_pct"))
    if rs1 is not None and rs3 is not None and rs6 is not None and rs1 > rs3 > rs6:
        entries.append(("RS acelera", "technical"))

    upside = _num(fields.get("target_upside_pct"))
    if upside is not None and upside < policy.upside_flag_pct:
        entries.append((f"upside<{policy.upside_flag_pct:g}%", "fundamental"))

    target = _num(fields.get("target_price"))
    if price is not None and target is not None and target > 0.0 and price > target:
        entries.append(("sobre objetivo", "fundamental"))

    profit_margin = _num(fields.get("profit_margin"))
    oper_margin = _num(fields.get("oper_margin"))
    if profit_margin is not None and oper_margin is not None and profit_margin > oper_margin:
        entries.append(("margen neto>operativo", "fundamental"))
    if oper_margin is not None and oper_margin > policy.cycle_oper_margin_pct:
        entries.append((f"margen oper.>{policy.cycle_oper_margin_pct:g}%", "fundamental"))
    growth = max(
        (value for value in (_num(fields.get("revenue_growth")), _num(fields.get("earnings_growth"))) if value is not None),
        default=None,
    )
    if growth is not None and growth > policy.growth_trap_pct:
        entries.append((f"crecimiento>{policy.growth_trap_pct:g}%", "fundamental"))

    sessions = _num(context.get("earnings_sessions"))
    if sessions is not None and sessions <= policy.earnings_flag_sessions:
        entries.append((f"earnings<={policy.earnings_flag_sessions:g} ses", "context"))

    weight = _num(context.get("weight_invested_pct"))
    if weight is not None and weight > policy.position_max_pct:
        entries.append((f"peso>{policy.position_max_pct:g}%", "context"))

    sessions_open = _num(context.get("sessions_open"))
    if sessions_open is not None and sessions_open < policy.min_sessions_alert:
        entries.append((f"sesiones<{policy.min_sessions_alert:g}", "context"))

    coverage_pct = _num(context.get("coverage_pct"))
    if coverage_pct is not None and coverage_pct < policy.coverage_min_pct:
        entries.append((f"cobertura<{policy.coverage_min_pct:g}%", "context"))

    if rs6 is not None and rs6 < 0.0:
        entries.append(("RS<0", "technical"))

    suspect = _suspect(fields)
    if suspect:
        entries.append((f"indicador sospechoso ({suspect})", "fundamental"))

    all_flags = tuple(flag for flag, _ in entries)
    technical = tuple(flag for flag, group in entries if group == "technical")
    fundamental = tuple(flag for flag, group in entries if group == "fundamental")
    contextual = tuple(flag for flag, group in entries if group == "context")
    return all_flags, technical, fundamental, contextual


def _ret_pct(context: Mapping[str, object]) -> float | None:
    ret = _num(context.get("ret_usd_pct"))
    return ret if ret is not None else _num(context.get("ret_clp_pct"))


def _sl_type(
    fields: Mapping[str, object],
    ticker: str,
    *,
    policy: BriefingConfig,
    semis_tickers: Collection[str],
) -> str:
    atr_pct = _num(fields.get("atr_pct"))
    if atr_pct is not None and atr_pct >= policy.sl_type_b_atr_pct:
        return "B"
    return "B" if ticker in semis_tickers else "A"


def _alarm_1(
    levels: Levels,
    fields: Mapping[str, object],
    *,
    price: float | None,
    entry: float | None,
    ret: float | None,
    policy: BriefingConfig,
    review: ReviewConfig | None,
) -> tuple[float | None, str]:
    """Alerta 1: alarma del motor, ajustada por soporte y por las reglas de ganancia."""
    alarm = levels.alarm
    if alarm is None or price is None or price <= 0.0:
        return None, levels.alarm_rule
    rule = levels.alarm_rule
    if review is not None:
        adjusted = _move_below_support(alarm, fields, price=price, levels_cfg=review.levels)
        if adjusted is not None and adjusted != alarm:
            alarm = adjusted
            rule = f"{rule} + 1% bajo soporte"
        if ret is not None and ret >= review.levels.breakeven_extra_gain_pct and entry is not None and entry > 0.0:
            floor = entry * (1.0 + review.levels.breakeven_extra_pct / 100.0)
            if alarm < floor:
                alarm = floor
                rule = f"{rule} + piso entrada+{review.levels.breakeven_extra_pct:g}%"
        elif ret is not None and ret >= review.levels.breakeven_gain_pct and entry is not None and entry > 0.0:
            if alarm < entry:
                alarm = entry
                rule = f"{rule} + piso entrada"
    return alarm, rule


def _alarm_2(
    levels: Levels,
    fields: Mapping[str, object],
    *,
    price: float | None,
    entry: float | None,
    review: ReviewConfig | None,
) -> float | None:
    """Alerta 2 (catastrofe) para Tipo B, con el ajuste de soporte de v6."""
    catastrophe = levels.catastrophe_floor
    if catastrophe is None or price is None or price <= 0.0:
        return None
    if review is not None:
        adjusted = _move_below_support(catastrophe, fields, price=price, levels_cfg=review.levels)
        if adjusted is not None:
            catastrophe = adjusted
    return catastrophe


def _move_below_support(
    level: float,
    fields: Mapping[str, object],
    *,
    price: float,
    levels_cfg: ReviewLevelsConfig,
) -> float | None:
    """Mueve una alerta a ``soporte * (1 - buffer)`` si esta a menos de la proximidad."""
    proximity = levels_cfg.sl_support_proximity_pct
    buffer = levels_cfg.sl_support_buffer_pct
    supports = [
        value
        for value in (
            _num(fields.get("low_20")),
            _num(fields.get("low_50")),
            _num(fields.get("sma200")),
        )
        if value is not None and 0.0 < value < price
    ]
    if not supports:
        return None
    support = max(supports)
    if abs(level - support) / support * 100.0 >= proximity:
        return None
    moved = support * (1.0 - buffer / 100.0)
    return moved if 0.0 < moved < price else None


def _distance(reference: float | None, level: float | None) -> float | None:
    """Distancia % de ``level`` bajo ``reference`` (negativa si queda por encima)."""
    if reference is None or reference <= 0.0 or level is None:
        return None
    return (reference - level) / reference * 100.0


def _suspect(fields: Mapping[str, object]) -> str:
    """Nomenclatura del campo con un valor implausible (unidad mezclada o serie congelada)."""
    if _num(fields.get("flat_tail_sessions")) is not None:
        return "serie plana sin volumen"
    pb = _num(fields.get("pb"))
    if pb is not None and pb > _PB_SUSPECT:
        return "P/B"
    peg = _num(fields.get("peg"))
    if peg is not None and peg != 0.0 and abs(peg) > _PEG_SUSPECT:
        return "PEG"
    ttm = _num(fields.get("eps_ttm"))
    fwd = _num(fields.get("eps_forward"))
    if ttm is not None and fwd is not None and ttm > 0.0 and fwd < 0.0:
        return "EPS fwd"
    return ""


@dataclass(frozen=True, slots=True)
class _StopPlan:
    stop: float | None
    distance_pct: float | None
    rule: str


def _stop_plan(fields: Mapping[str, object], *, price: float | None, policy: BriefingConfig) -> _StopPlan:
    """Stop tecnico: soporte mas cercano bajo el precio menos el buffer de ATR."""
    if price is None or price <= 0.0:
        return _StopPlan(None, None, "sin precio")
    atr = _num(fields.get("atr"))
    max_dist = policy.stop.max_distance_pct / 100.0

    support: float | None = None
    support_name = ""
    for name, value in (
        ("SMA50", _num(fields.get("sma50"))),
        ("SMA200", _num(fields.get("sma200"))),
        ("Min 20 ses", _num(fields.get("low_20"))),
        ("Min 50 ses", _num(fields.get("low_50"))),
        ("Min 52S", _num(fields.get("low_52w"))),
    ):
        if value is None or value <= 0.0 or value >= price:
            continue
        if (price - value) / price > max_dist:
            continue
        if support is None or value > support:
            support = value
            support_name = name

    rule = ""
    if atr is None or atr <= 0.0:
        stop: float | None = None
        rule = "sin ATR"
    elif support is not None:
        stop = support - policy.stop.buffer_atr * atr
        rule = f"{support_name} - {policy.stop.buffer_atr:g}xATR"
    else:
        stop = price - policy.stop.fallback_atr_mult * atr
        rule = f"precio - {policy.stop.fallback_atr_mult:g}xATR"

    if stop is not None and atr is not None and atr > 0.0:
        floor = price * (1.0 - max_dist)
        ceil = price - policy.stop.min_atr_mult * atr
        if stop < floor:
            # El tope de riesgo (max_distance_pct) es duro: si un ATR muy alto empuja
            # el piso de distancia mas alla del tope, el tope manda.
            stop = floor
            rule += " (clamp al 15%)"
        elif stop > ceil and ceil >= floor:
            stop = ceil
            rule += f" (clamp {policy.stop.min_atr_mult:g}xATR)"
    distance = (price - stop) / price * 100.0 if stop is not None else None
    return _StopPlan(stop, distance, rule)


def _take_profit(
    fields: Mapping[str, object],
    *,
    price: float | None,
    sl_type: str,
    alarm_1: float | None,
    policy: BriefingConfig,
) -> tuple[float | None, str, str]:
    """TP como orden real de v6, realista hacia marzo de 2027.

    Base: el objetivo de tesis/analistas cuando existe; si no, el techo tecnico mas
    cercano sobre el recorrido minimo. El nivel se acota al maximo de 52 semanas y al
    tope de recorrido del tipo de SL (``+30`` % Tipo A, ``+40`` % Tipo B). Si el R:R
    contra la Alerta 1 no alcanza ``take_profit_min_rr``, sube una vez al siguiente
    techo tecnico valido; si no hay ninguno, conserva el TP realista y declara la
    excepcion en ``tp_note``.

    Devuelve ``(precio, etiqueta de origen, nota del ajuste)``.
    """
    if price is None or price <= 0.0:
        return None, _TP_NONE, ""
    threshold = price * (1.0 + policy.take_profit_min_upside_pct / 100.0)
    max_upside_pct = (
        policy.take_profit_max_upside_b_pct if sl_type == "B" else policy.take_profit_max_upside_a_pct
    )
    cap = price * (1.0 + max_upside_pct / 100.0)
    band = _num(fields.get("high_52w"))
    if band is not None and band > price:
        cap = min(cap, band)
    if cap < threshold:
        return None, _TP_NONE, "sin techo realista bajo el maximo de 52 semanas"

    technical = [
        (value, label)
        for value, label in (
            (_num(fields.get("high_20")), _TP_RANGE),
            (_num(fields.get("high_50")), _TP_RANGE),
            (_num(fields.get("high_52w")), _TP_BAND),
        )
        if value is not None and value >= threshold
    ]
    target = _num(fields.get("target_price"))
    note = ""
    if target is not None and target >= threshold:
        take_profit = min(target, cap)
        label = _TP_TARGET
        if take_profit < target:
            note = "objetivo acotado al tope de recorrido realista"
    elif technical:
        nearest = min(technical, key=lambda item: item[0])
        take_profit = min(nearest[0], cap)
        label = nearest[1]
        if take_profit < nearest[0]:
            note = "techo acotado al tope de recorrido realista"
    else:
        return None, _TP_NONE, ""

    if alarm_1 is not None and alarm_1 < price:
        risk = price - alarm_1
        rr = (take_profit - price) / risk
        if rr < policy.take_profit_min_rr:
            higher = sorted(
                value for value, _ in technical if take_profit < value <= cap
            )
            if higher:
                next_level = higher[0]
                next_label = next(label for value, label in technical if value == next_level)
                take_profit = next_level
                label = next_label
                note = "subido al siguiente techo tecnico para cumplir el R:R"
            else:
                note = f"excepcion declarada: R:R {rr:.2f} < {policy.take_profit_min_rr:g}"
    return take_profit, label, note


def _alerts(
    rows: Sequence[PositionStats],
    *,
    panel: Mapping[str, object],
    policy: BriefingConfig,
    positions: Sequence[PositionBrief] = (),
) -> tuple[str, ...]:
    """Alertas informativas de concentracion, caja, earnings y riesgo teorico."""
    alerts: list[str] = []
    cash = _num(panel.get("cash_pct"))
    if cash is not None and cash < policy.cash_min_pct:
        alerts.append(f"caja {cash:.2f} % bajo el piso {policy.cash_min_pct:g} %")
    if cash is not None and cash > policy.cash_max_pct:
        alerts.append(f"caja {cash:.2f} % sobre el techo {policy.cash_max_pct:g} %")
    for row in rows:
        weight = _num(row.context.get("weight_invested_pct"))
        if weight is not None and weight > policy.position_max_pct:
            alerts.append(f"{row.ticker}: peso {weight:.2f} % sobre el tope {policy.position_max_pct:g} %")
    themes = panel.get("themes")
    if isinstance(themes, Mapping):
        for theme, raw in sorted(themes.items(), key=lambda kv: -(_num(kv[1]) or 0.0)):
            if theme in _NO_THEME:
                continue
            pct = _num(raw)
            if pct is not None and pct > policy.theme_max_pct:
                alerts.append(f"tema {theme}: {pct:.2f} % sobre el tope {policy.theme_max_pct:g} %")
    ia = _cluster(panel, "ia_ampliada")
    ia_pct = ia.get("pct")
    if ia_pct is not None and ia_pct > policy.ia_ampliada_max_pct:
        alerts.append(f"IA ampliada: {ia_pct:.2f} % sobre el tope {policy.ia_ampliada_max_pct:g} %")

    for window in policy.earnings_windows_sessions:
        tickers = [
            row.ticker
            for row in rows
            if (sessions := _num(row.context.get("earnings_sessions"))) is not None and sessions <= window
        ]
        if tickers:
            alerts.append(f"earnings <={window} ses: {', '.join(tickers)}")
    for brief in positions:
        risk = brief.risk_theoretical_pct
        if risk is not None and risk > policy.risk_alert_pct:
            alerts.append(
                f"{brief.ticker}: riesgo teorico {risk:.2f} % sobre {policy.risk_alert_pct:g} % del patrimonio"
            )
    return tuple(alerts)


def _price(context: Mapping[str, object]) -> float | None:
    return _num(context.get("price_usd"))


def _num(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return None if math.isnan(number) else number
    return None
