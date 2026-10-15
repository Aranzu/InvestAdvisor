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
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from swing.core.verdict import compute_levels

if TYPE_CHECKING:
    from swing.core.models import BriefingConfig, ReviewConfig
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
    stop: float | None = None
    stop_dist_pct: float | None = None
    stop_rule: str = ""
    risk_pct: float | None = None
    take_profit: float | None = None
    tp_label: str = ""
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


def brief_positions(
    rows: Sequence[PositionStats],
    *,
    patrimonio_clp: float | None,
    policy: BriefingConfig,
    review: ReviewConfig | None = None,
) -> tuple[PositionBrief, ...]:
    """Hechos deterministas por posicion (banderas, stop tecnico, take profit y riesgo)."""
    return tuple(_brief(row, patrimonio_clp=patrimonio_clp, policy=policy, review=review) for row in rows)


def build(
    rows: Sequence[PositionStats],
    *,
    panel: Mapping[str, object],
    patrimonio_clp: float | None,
    policy: BriefingConfig,
    review: ReviewConfig | None = None,
) -> Briefing:
    """Arma el bloque determinista a partir de las filas ya enriquecidas y el panel."""
    positions = brief_positions(rows, patrimonio_clp=patrimonio_clp, policy=policy, review=review)
    semis = _cluster(panel, "semis")
    ia = _cluster(panel, "ia_ampliada")
    return Briefing(
        positions=positions,
        alerts=_alerts(rows, panel=panel, policy=policy),
        position_max_pct=policy.position_max_pct,
        theme_max_pct=policy.theme_max_pct,
        cash_pct=_num(panel.get("cash_pct")),
        cash_min_pct=policy.cash_min_pct,
        cash_max_pct=policy.cash_max_pct,
        semis_pct=semis.get("pct"),
        semis_max_pct=semis.get("max_pct"),
        ia_pct=ia.get("pct"),
        ia_max_pct=ia.get("max_pct"),
    )


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
) -> PositionBrief:
    context = row.context
    fields = row.fields
    price = _price(context)
    plan = _stop_plan(fields, price=price, policy=policy)
    value = _num(context.get("value_clp"))
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
        policy=review,
    )
    return PositionBrief(
        ticker=row.ticker,
        flags=_flags(context, fields, price=price, policy=policy),
        stop=plan.stop,
        stop_dist_pct=plan.distance_pct,
        stop_rule=plan.rule,
        risk_pct=risk,
        take_profit=plan.take_profit,
        tp_label=plan.tp_label,
        levels=levels,
    )


def _flags(
    context: Mapping[str, object],
    fields: Mapping[str, object],
    *,
    price: float | None,
    policy: BriefingConfig,
) -> tuple[str, ...]:
    flags: list[str] = []
    ret = _num(context.get("ret_usd_pct"))
    if ret is None:
        ret = _num(context.get("ret_clp_pct"))
    if ret is not None and ret > policy.gain_flag_pct:
        flags.append(f"ganancia>{policy.gain_flag_pct:g}%")
    if ret is not None and ret < -policy.loss_flag_pct:
        flags.append(f"perdida>{policy.loss_flag_pct:g}%")

    vs200 = _num(fields.get("price_vs_sma200_pct"))
    if vs200 is not None and vs200 < 0.0:
        flags.append("bajo SMA200")

    rsi = _num(fields.get("rsi"))
    if rsi is not None and rsi > policy.rsi_overbought:
        flags.append(f"RSI>{policy.rsi_overbought:g}")
    if rsi is not None and rsi < policy.rsi_oversold:
        flags.append(f"RSI<{policy.rsi_oversold:g}")

    atr_pct = _num(fields.get("atr_pct"))
    if atr_pct is not None and atr_pct > policy.atr_flag_pct:
        flags.append(f"ATR%>{policy.atr_flag_pct:g}")

    upside = _num(fields.get("target_upside_pct"))
    if upside is not None and upside < policy.upside_flag_pct:
        flags.append(f"upside<{policy.upside_flag_pct:g}%")

    target = _num(fields.get("target_price"))
    if price is not None and target is not None and target > 0.0 and price > target:
        flags.append("sobre objetivo")

    sessions = _num(context.get("earnings_sessions"))
    if sessions is not None and sessions <= policy.earnings_flag_sessions:
        flags.append(f"earnings<={policy.earnings_flag_sessions:g} ses")

    weight = _num(context.get("weight_invested_pct"))
    if weight is not None and weight > policy.position_max_pct:
        flags.append(f"peso>{policy.position_max_pct:g}%")

    rs = _num(fields.get("rs_pct"))
    if rs is not None and rs < 0.0:
        flags.append("RS<0")

    suspect = _suspect(fields)
    if suspect:
        flags.append(f"indicador sospechoso ({suspect})")

    return tuple(flags)


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
    take_profit: float | None
    tp_label: str


def _stop_plan(fields: Mapping[str, object], *, price: float | None, policy: BriefingConfig) -> _StopPlan:
    """Stop tecnico: soporte mas cercano bajo el precio menos el buffer de ATR."""
    if price is None or price <= 0.0:
        return _StopPlan(None, None, "sin precio", None, "")
    take_profit, tp_label = _take_profit(fields, price, policy.take_profit_min_upside_pct)
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
    return _StopPlan(stop, distance, rule, take_profit, tp_label)


def _take_profit(fields: Mapping[str, object], price: float, min_upside_pct: float) -> tuple[float | None, str]:
    """Nivel de take profit para un horizonte de meses, no el techo de la semana.

    Entre el objetivo del analista, el maximo de 52 semanas y los maximos de 50/20
    sesiones, gana el **mas cercano que iguale el recorrido minimo** de la politica.
    Si un nivel solo esta a un pelo del precio (lo comun cuando el activo cotiza
    pegado a su maximo de 20 sesiones) no es un objetivo, y el informe lo dice con
    ``sin techo claro`` en vez de ofrecer un take profit a 0.02 %.
    """
    threshold = price * (1.0 + min_upside_pct / 100.0)
    candidates = (
        (_num(fields.get("target_price")), _TP_TARGET),
        (_num(fields.get("high_52w")), _TP_BAND),
        (_num(fields.get("high_50")), _TP_RANGE),
        (_num(fields.get("high_20")), _TP_RANGE),
    )
    valid = [(value, label) for value, label in candidates if value is not None and value >= threshold]
    if not valid:
        return None, _TP_NONE
    return min(valid, key=lambda item: item[0])


def _alerts(rows: Sequence[PositionStats], *, panel: Mapping[str, object], policy: BriefingConfig) -> tuple[str, ...]:
    """Alertas informativas de concentracion y caja. Nunca son motivo de venta per se."""
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
