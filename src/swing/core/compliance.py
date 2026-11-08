"""Panel de cumplimiento del ``PROMPT PORTAFOLIO`` v9 (P1-P16).

Modulo **puro**: recibe los hechos ya resueltos (entrada, precio, ATR, valor y el
estado previo del ledger) y devuelve los niveles y el estado de cada posicion.
No toca red, disco ni reloj.

La regla central es que **todo stop nace de la entrada**: el stop inicial se
calcula con P8 y el vigente solo sube (break-even, entrada + 1R, trailing). La
unica excepcion es el stop de transicion de una heredada que se adopta (P13),
que se fija una vez y tampoco baja.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from swing.core.models import ComplianceConfig, FactorsConfig

#: Estados del panel. El orden de precedencia esta en ``plan_position``.
STATUS_OK = "OK"
STATUS_OVERWEIGHT = "SOBRE_PESO"
STATUS_OVERRISK = "SOBRE_RIESGO"
STATUS_STOPPED = "STOP_TOCADO"
STATUS_INHERITED_0 = "HEREDADA_TIPO_0"
STATUS_INHERITED_A = "HEREDADA_TIPO_A"
STATUS_INHERITED_B = "HEREDADA_TIPO_B"
STATUS_NO_STOP = "SIN_STOP"

#: Compatibilidad con el nombre viejo (v7): una heredada sin tipo se trata como A.
STATUS_INHERITED = STATUS_INHERITED_A
INHERITED_STATUS = {
    "0": STATUS_INHERITED_0,
    "A": STATUS_INHERITED_A,
    "B": STATUS_INHERITED_B,
}

STATUSES = (
    STATUS_OK,
    STATUS_OVERWEIGHT,
    STATUS_OVERRISK,
    STATUS_STOPPED,
    STATUS_INHERITED_0,
    STATUS_INHERITED_A,
    STATUS_INHERITED_B,
    STATUS_NO_STOP,
)

MODE_NORMAL = "normal"
MODE_CAUTION = "cautela"
MODE_DEFENSIVE = "defensivo"
_MODE_RANK = {MODE_NORMAL: 0, MODE_CAUTION: 1, MODE_DEFENSIVE: 2, "": 0}


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    """Un stop persistido por ticker (``state/ledger.json``).

    El ledger es tambien el registro de decisiones de las heredadas: tipo (0/A/B),
    fecha de adopcion, decision (Si/No), stop de transicion (P13), tramos del
    calendario de reduccion ya ejecutados y tipo de stop (P11).
    """

    ticker: str
    entry_usd: float
    first_entry: dt.date
    initial_stop: float
    current_stop: float
    take_profit: float | None = None
    tp_formula: str = ""
    transition_stop: float | None = None
    transition_date: dt.date | None = None
    inherited_type: str = ""
    adoption_date: dt.date | None = None
    decision: str = ""
    decision_date: dt.date | None = None
    reduction_tranches_done: tuple[str, ...] = ()
    stop_kind: str = ""
    stop_source: str = ""
    """Componente que gana el stop vigente (``inicial``, ``transicion``, ``break-even``...)."""

    @property
    def inherited(self) -> bool:
        """True si la posicion no nacio de una entrada nueva (tipos 0/A/B).

        El tipo 0 es una heredada **sana**: su stop P8 ya esta registrado y no
        requiere decision. Antes ``inherited`` excluia el tipo 0 y el panel se
        contradecia (``HEREDADA_TIPO_0`` con ``inherited: false``).
        """
        return self.inherited_type in {"0", "A", "B"}

    @property
    def stop_registered(self) -> bool:
        """True cuando la posicion tiene un stop P8 registrado.

        Lo tienen las entradas normales, las heredadas tipo 0 y las A/B ya adoptadas.
        Solo las A/B sin decision viven con un stop teorico no registrado.
        """
        return self.inherited_type not in {"A", "B"} or self.decision == "Si"

    def to_dict(self) -> dict[str, object]:
        return {
            "ticker": self.ticker,
            "entry_usd": self.entry_usd,
            "first_entry": self.first_entry.isoformat(),
            "initial_stop": self.initial_stop,
            "current_stop": self.current_stop,
            "take_profit": self.take_profit,
            "tp_formula": self.tp_formula,
            "transition_stop": self.transition_stop,
            "transition_date": self.transition_date.isoformat() if self.transition_date else None,
            "inherited_type": self.inherited_type,
            "adoption_date": self.adoption_date.isoformat() if self.adoption_date else None,
            "decision": self.decision,
            "decision_date": self.decision_date.isoformat() if self.decision_date else None,
            "reduction_tranches_done": list(self.reduction_tranches_done),
            "stop_kind": self.stop_kind,
            "stop_source": self.stop_source,
            # Campo historico: se conserva para que un lector viejo no se rompa.
            "inherited": self.inherited,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> LedgerEntry | None:
        try:
            ticker = str(data["ticker"]).strip().upper()
            entry = float(data["entry_usd"])  # type: ignore[arg-type]
            first = dt.date.fromisoformat(str(data["first_entry"]))
            initial = float(data["initial_stop"])  # type: ignore[arg-type]
            current = float(data["current_stop"])  # type: ignore[arg-type]
        except (KeyError, TypeError, ValueError):
            return None
        if not ticker or entry <= 0.0 or initial <= 0.0 or current <= 0.0:
            return None
        inherited_type = str(data.get("inherited_type") or "").strip().upper()
        if not inherited_type and bool(data.get("inherited", False)):
            inherited_type = "A"
        return cls(
            ticker=ticker,
            entry_usd=entry,
            first_entry=first,
            initial_stop=initial,
            current_stop=current,
            take_profit=_opt_float(data.get("take_profit")),
            tp_formula=str(data.get("tp_formula") or ""),
            transition_stop=_opt_float(data.get("transition_stop")),
            transition_date=_opt_date(data.get("transition_date")),
            inherited_type=inherited_type,
            adoption_date=_opt_date(data.get("adoption_date")),
            decision=str(data.get("decision") or ""),
            decision_date=_opt_date(data.get("decision_date")),
            reduction_tranches_done=_opt_str_tuple(data.get("reduction_tranches_done")),
            stop_kind=str(data.get("stop_kind") or ""),
            stop_source=str(data.get("stop_source") or ""),
        )


@dataclass(frozen=True, slots=True)
class ComplianceRow:
    """Una fila del panel: niveles anclados a la entrada y estado de cumplimiento."""

    ticker: str
    price: float | None
    entry: float | None
    atr_pct: float | None
    value_clp: float | None
    weight_invested_pct: float | None
    weight_equity_pct: float | None
    initial_stop: float | None
    current_stop: float | None
    previous_stop: float | None
    transition_stop: float | None
    take_profit: float | None
    tp_formula: str
    stop_dist_price_pct: float | None
    stop_dist_entry_pct: float | None
    r_multiple: float | None
    risk_pct: float | None
    max_weight_open_pct: float | None
    status: str
    inherited: bool = False
    inherited_type: str = ""
    stop_registered: bool = False
    """``True`` si el stop esta registrado: tipo 0 sano o heredada A/B adoptada."""
    stop_viable: bool = True
    transition_date: dt.date | None = None
    adoption_date: dt.date | None = None
    decision: str = ""
    decision_date: dt.date | None = None
    reduction_tranches_done: tuple[str, ...] = ()
    stop_kind: str = ""
    new_entry_clp: float | None = None
    new_entry_shares: float | None = None
    new_entry_pct: float | None = None
    break_even: float | None = None
    trailing_stop: float | None = None
    stop_source: str = ""
    risk_stop: float | None = None
    risk_source: str = ""
    risk_pct_theoretical: float | None = None
    """Riesgo P4 con el stop teorico; solo difiere en heredadas sin adoptar."""
    stop_xtb: float | None = None
    tp_xtb: float | None = None
    stop_xtb_differs: bool = False
    tp_xtb_differs: bool = False
    earnings_sessions: float | None = None


@dataclass(frozen=True, slots=True)
class ComplianceSummary:
    """Agregados del panel: limites P1-P16 y modo vigente."""

    open_risk_pct: float | None = None
    max_open_risk_pct: float | None = None
    cash_pct: float | None = None
    min_cash_pct: float | None = None
    cash_band_max_pct: float | None = None
    equity_clp: float | None = None
    invested_clp: float | None = None
    factor_pcts: Mapping[str, float] = field(default_factory=dict)
    factor_max_pct: Mapping[str, float] = field(default_factory=dict)
    theme_pcts: Mapping[str, float] = field(default_factory=dict)
    aggregate_theme_pcts: Mapping[str, float] = field(default_factory=dict)
    max_theme_pct: float | None = None
    biggest_theme: str = ""
    biggest_theme_pct: float | None = None
    infractions: int = 0
    mode: str = MODE_NORMAL
    breadth_pct: float | None = None
    avg_correlation: float | None = None
    beta_spy: float | None = None
    beta_qqq: float | None = None
    earnings_5: tuple[str, ...] = ()
    earnings_10: tuple[str, ...] = ()
    earnings_15: tuple[str, ...] = ()
    drawdown_pct: float | None = None
    drawdown_mode: str = MODE_NORMAL
    regime_mode: str = MODE_NORMAL
    suspicious_benchmarks: str = ""
    next_milestone: str = ""
    inherited_counts: Mapping[str, int] = field(default_factory=dict)
    decision_pending: tuple[str, ...] = ()
    overweight: tuple[str, ...] = ()
    overrisk: tuple[str, ...] = ()
    max_weight_hold_pct: float | None = None
    max_weight_trim_pct: float | None = None
    max_risk_position_pct: float | None = None
    max_sale_per_session_pct: float | None = None
    reduction_max_weeks: int | None = None


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def initial_stop(entry: float | None, atr: float | None, cfg: ComplianceConfig) -> float | None:
    """P8: ``entrada - clamp(2,5 x ATR, 8 % entrada, 18 % entrada)``."""
    if entry is None or entry <= 0.0 or atr is None or atr <= 0.0:
        return None
    distance = clamp(cfg.stop_atr_mult * atr, cfg.stop_min_pct / 100.0 * entry, cfg.stop_max_pct / 100.0 * entry)
    stop = entry - distance
    return stop if stop > 0.0 else None


def take_profit(
    *,
    price: float | None,
    target_price: float | None,
    analyst_count: float | None,
    previous: float | None,
    cfg: ComplianceConfig,
) -> tuple[float | None, str]:
    """TP realista: objetivo medio si N >= 10; tope de +30 % sobre el precio; solo sube."""
    if price is None or price <= 0.0:
        return previous, "sin precio"
    cap = price * (1.0 + cfg.take_profit_max_upside_pct / 100.0)
    candidate = cap
    formula = f"tope +{cfg.take_profit_max_upside_pct:g}%"
    if (
        target_price is not None
        and target_price > price
        and analyst_count is not None
        and analyst_count >= cfg.take_profit_min_analysts
    ):
        candidate = min(target_price, cap)
        formula = "= analistas (N>=" + str(cfg.take_profit_min_analysts) + ")"
        if target_price > cap:
            formula = f"= analistas topado +{cfg.take_profit_max_upside_pct:g}%"
    if previous is not None and previous > 0.0:
        candidate = max(previous, candidate)
        if previous > cap:
            formula = "vigente (solo sube)"
    return candidate, formula


def transition_stop(
    price: float | None, atr: float | None, cfg: ComplianceConfig
) -> tuple[float | None, bool]:
    """P13: ``precio - max(1,5 x ATR, 8 %)`` con techo de ``transition_stop_max_pct``.

    Devuelve ``(stop, viable)``. Si ``1,5 x ATR`` supera el techo no hay stop
    viable: la regla manda salir y el stop se informa igual, topado, para que se
    vea la distancia que se estaria aceptando.
    """
    if price is None or price <= 0.0 or atr is None or atr <= 0.0:
        return None, False
    raw = max(1.5 * atr, 0.08 * price)
    cap = cfg.transition_stop_max_pct / 100.0 * price
    viable = raw <= cap
    return price - min(raw, cap), viable


def inherited_type(price: float | None, initial: float | None, cfg: ComplianceConfig) -> str:
    """Clasificacion v8 de una heredada: ``0`` sana, ``A`` bajo su stop, ``B`` stop lejano."""
    if price is None or price <= 0.0 or initial is None or initial <= 0.0:
        return ""
    if price < initial:
        return "A"
    if initial < price * (1.0 - cfg.inherited_stop_max_distance_pct / 100.0):
        return "B"
    return "0"


def new_entry_size(
    *,
    price: float | None,
    stop: float | None,
    atr_pct: float | None,
    equity_clp: float | None,
    fx: float | None,
    cfg: ComplianceConfig,
) -> tuple[float | None, float | None, float | None]:
    """Tamano de una entrada nueva: ``min(P1 / distancia, P2)`` en % del patrimonio.

    Devuelve ``(CLP, acciones, % del patrimonio)``. ``P2`` baja a
    ``max_weight_open_high_atr_pct`` cuando el ATR % supera ``high_atr_pct``. Las
    acciones usan el precio en USD y el tipo de cambio CLP/USD.
    """
    if price is None or price <= 0.0 or stop is None or stop <= 0.0 or stop >= price:
        return None, None, None
    distance_pct = (price - stop) / price * 100.0
    if distance_pct <= 0.0:
        return None, None, None
    p2 = cfg.max_weight_open_high_atr_pct if (atr_pct is not None and atr_pct > cfg.high_atr_pct) else cfg.max_weight_open_pct
    pct = min(cfg.risk_per_open_pct / distance_pct * 100.0, p2)
    if equity_clp is not None and equity_clp > 0.0:
        clp = equity_clp * pct / 100.0
        shares = clp / (price * fx) if fx is not None and fx > 0.0 else None
        return clp, shares, pct
    return None, None, pct


def plan_position(
    *,
    ticker: str,
    entry: float | None,
    price: float | None,
    atr: float | None,
    atr_pct: float | None,
    value_clp: float | None,
    patrimonio_clp: float | None,
    weight_invested_pct: float | None,
    min_20: float | None,
    previous: LedgerEntry | None,
    stop_xtb: float | None,
    tp_xtb: float | None,
    target_price: float | None,
    analyst_count: float | None,
    earnings_sessions: float | None,
    cfg: ComplianceConfig,
    equity_clp: float | None = None,
    weight_equity_pct: float | None = None,
    fx: float | None = None,
    stop_kind: str = "",
) -> ComplianceRow:
    """Niveles y estado de una posicion, sin bajar nunca el stop vigente."""
    initial = initial_stop(entry, atr, cfg)
    if initial is None or entry is None or price is None:
        prev_type = previous.inherited_type if previous is not None else ""
        trans, viable = transition_stop(price, atr, cfg)
        return ComplianceRow(
            ticker=ticker,
            price=price,
            entry=entry,
            atr_pct=atr_pct,
            value_clp=value_clp,
            weight_invested_pct=weight_invested_pct,
            weight_equity_pct=weight_equity_pct,
            initial_stop=initial,
            current_stop=None,
            previous_stop=previous.current_stop if previous is not None else None,
            transition_stop=previous.transition_stop if previous is not None and previous.transition_stop is not None else trans,
            take_profit=previous.take_profit if previous is not None else None,
            tp_formula=previous.tp_formula if previous is not None and previous.tp_formula else ("sin precio" if price is None else "sin ATR"),
            stop_dist_price_pct=None,
            stop_dist_entry_pct=None,
            r_multiple=None,
            risk_pct=None,
            max_weight_open_pct=None,
            status=STATUS_NO_STOP,
            inherited=prev_type in {"0", "A", "B"},
            inherited_type=prev_type,
            stop_registered=prev_type not in {"A", "B"}
            or (previous.decision if previous is not None else "") == "Si",
            stop_viable=viable,
            transition_date=previous.transition_date if previous is not None else None,
            adoption_date=previous.adoption_date if previous is not None else None,
            decision=previous.decision if previous is not None else "",
            decision_date=previous.decision_date if previous is not None else None,
            reduction_tranches_done=previous.reduction_tranches_done if previous is not None else (),
            stop_kind=stop_kind or (previous.stop_kind if previous is not None else ""),
            stop_xtb=stop_xtb,
            tp_xtb=tp_xtb,
            stop_xtb_differs=stop_xtb is not None,
            tp_xtb_differs=tp_xtb is not None,
            earnings_sessions=earnings_sessions,
        )

    r_value = entry - initial
    r_multiple = (price - entry) / r_value if r_value > 0.0 else None
    previous_stop = previous.current_stop if previous is not None else None
    if previous is not None and previous.inherited_type:
        kind = previous.inherited_type
    elif previous is not None:
        kind = ""
    else:
        kind = inherited_type(price, initial, cfg)
    decision = previous.decision if previous is not None else ""
    adopted = decision == "Si"

    trans = previous.transition_stop if previous is not None else None
    trans_date = previous.transition_date if previous is not None else None
    if kind in {"A", "B"}:
        computed, viable = transition_stop(price, atr, cfg)
        if trans is None:
            # Propuesta: se recalcula en cada corrida hasta que se adopte (P13).
            trans = computed
        if previous is not None and previous.transition_stop is not None:
            # Ya adoptada: el stop de transicion quedo fijado y no se re-evalua.
            viable = True
    else:
        viable = True
    stop_viable = viable if kind in {"A", "B"} else True

    # --- Jerarquia unica del stop vigente (v10, seccion 4) -------------------
    # vigente = max(inicial/transicion, break-even +1,5R, entrada+1R +3R,
    #               trailing min_20 - 3xATR desde +3R)
    break_even = entry if (r_multiple is not None and r_multiple >= cfg.breakeven_r) else None
    enhancer = entry + r_value if (r_multiple is not None and r_multiple >= cfg.enhancer_r) else None
    trailing_level = (
        min_20 - cfg.trailing_atr_mult * atr if min_20 is not None and atr is not None and atr > 0.0 else None
    )
    # El trailing solo entra desde +3R; antes queda solo como referencia.
    trailing_applies = trailing_level if enhancer is not None else None

    prev_source = ""
    if previous is not None:
        prev_source = previous.stop_source or (
            "inicial" if abs(previous.current_stop - previous.initial_stop) <= 1e-9 else "previo"
        )
    # El P8 teorico de una heredada tipo A puede estar sobre el precio: al
    # adoptar manda el stop de transicion y el teorico deja de arrastrarse.
    keep_previous = previous_stop is not None and not (
        kind in {"A", "B"} and adopted and prev_source == "inicial"
    )

    base = (trans, "transicion") if (kind in {"A", "B"} and adopted and trans is not None) else (initial, "inicial")
    candidates: list[tuple[float, str]] = [base]
    if keep_previous:
        candidates.append((previous_stop, prev_source or "previo"))
    if break_even is not None:
        candidates.append((break_even, "break-even"))
    if enhancer is not None:
        candidates.append((enhancer, "+1R"))
    if trailing_applies is not None:
        candidates.append((trailing_applies, "trailing"))
    current, stop_source = max(candidates, key=lambda item: item[0])

    # P4 de una heredada: manda el stop de transicion (propuesto o fijado), no
    # el P8 teorico (v10 ap. A.2). El teorico se informa como segunda columna.
    if kind in {"A", "B"} and not adopted:
        risk_candidates: list[tuple[float, str]] = []
        if trans is not None:
            risk_candidates.append((trans, "transicion"))
        if keep_previous and prev_source in {"transicion", "break-even", "+1R", "trailing"}:
            risk_candidates.append((previous_stop, prev_source))
        if break_even is not None:
            risk_candidates.append((break_even, "break-even"))
        if enhancer is not None:
            risk_candidates.append((enhancer, "+1R"))
        if trailing_applies is not None:
            risk_candidates.append((trailing_applies, "trailing"))
        if risk_candidates:
            risk_stop, risk_source = max(risk_candidates, key=lambda item: item[0])
        else:
            risk_stop, risk_source = current, stop_source
    else:
        risk_stop, risk_source = current, stop_source

    tp, tp_formula = take_profit(
        price=price,
        target_price=target_price,
        analyst_count=analyst_count,
        previous=previous.take_profit if previous is not None else None,
        cfg=cfg,
    )

    stop_dist_price = (price - current) / price * 100.0 if price > 0.0 else None
    stop_dist_entry = (entry - current) / entry * 100.0 if entry > 0.0 else None
    risk_dist_price = (price - risk_stop) / price * 100.0 if price > 0.0 else None
    risk_pct = _risk_pct(value_clp, patrimonio_clp, risk_dist_price)
    risk_pct_theoretical = (
        _risk_pct(value_clp, patrimonio_clp, stop_dist_price) if risk_stop != current else None
    )

    max_weight = None
    if risk_dist_price is not None and risk_dist_price > 0.0:
        max_weight = cfg.risk_per_open_pct / risk_dist_price * 100.0
        high_atr = atr_pct is not None and atr_pct > cfg.high_atr_pct
        p2 = cfg.max_weight_open_high_atr_pct if high_atr else cfg.max_weight_open_pct
        max_weight = min(max_weight, p2)
    # Tamano de una entrada nueva *hoy*: el stop P8 se recalcula desde el precio
    # actual (la pregunta de adopcion es "abriria esto hoy, con stop de entrada
    # nueva"), no desde la entrada historica de la posicion.
    fresh_stop = initial_stop(price, atr, cfg)
    entry_clp, entry_shares, entry_pct = new_entry_size(
        price=price,
        stop=fresh_stop,
        atr_pct=atr_pct,
        equity_clp=equity_clp,
        fx=fx,
        cfg=cfg,
    )

    status = _status(
        price=price,
        current=current,
        inherited_type=kind,
        decision=previous.decision if previous is not None else "",
        risk_pct=risk_pct,
        weight=weight_equity_pct,
        cfg=cfg,
    )
    return ComplianceRow(
        ticker=ticker,
        price=price,
        entry=entry,
        atr_pct=atr_pct,
        value_clp=value_clp,
        weight_invested_pct=weight_invested_pct,
        weight_equity_pct=weight_equity_pct,
        initial_stop=initial,
        current_stop=current,
        previous_stop=previous_stop,
        transition_stop=trans,
        take_profit=tp,
        tp_formula=tp_formula,
        stop_dist_price_pct=stop_dist_price,
        stop_dist_entry_pct=stop_dist_entry,
        r_multiple=r_multiple,
        risk_pct=risk_pct,
        max_weight_open_pct=max_weight,
        status=status,
        inherited=kind in {"0", "A", "B"},
        inherited_type=kind,
        stop_registered=kind not in {"A", "B"} or adopted,
        stop_viable=stop_viable,
        transition_date=trans_date,
        adoption_date=previous.adoption_date if previous is not None else None,
        decision=previous.decision if previous is not None else "",
        decision_date=previous.decision_date if previous is not None else None,
        reduction_tranches_done=previous.reduction_tranches_done if previous is not None else (),
        stop_kind=stop_kind or (previous.stop_kind if previous is not None else ""),
        new_entry_clp=entry_clp,
        new_entry_shares=entry_shares,
        new_entry_pct=entry_pct,
        break_even=break_even,
        trailing_stop=trailing_level,
        stop_source=stop_source,
        risk_stop=risk_stop,
        risk_source=risk_source,
        risk_pct_theoretical=risk_pct_theoretical,
        stop_xtb=stop_xtb,
        tp_xtb=tp_xtb,
        stop_xtb_differs=_differs(stop_xtb, current),
        tp_xtb_differs=_differs(tp_xtb, tp),
        earnings_sessions=earnings_sessions,
    )


def _is_inherited(price: float, initial: float, cfg: ComplianceConfig) -> bool:
    """Compatibilidad: ``True`` si la clasificacion v8 no es tipo 0."""
    return inherited_type(price, initial, cfg) in {"A", "B"}


def _risk_pct(
    value_clp: float | None,
    patrimonio_clp: float | None,
    distance_pct: float | None,
) -> float | None:
    """P4: ``peso x distancia al stop`` como % del patrimonio."""
    if (
        value_clp is None
        or value_clp <= 0.0
        or patrimonio_clp is None
        or patrimonio_clp <= 0.0
        or distance_pct is None
    ):
        return None
    return value_clp * max(0.0, distance_pct) / 100.0 / patrimonio_clp * 100.0


def _status(
    *,
    price: float,
    current: float,
    inherited_type: str,
    decision: str,
    risk_pct: float | None,
    weight: float | None,
    cfg: ComplianceConfig,
) -> str:
    # A/B sin decision son la infraccion de la regla 1. Una vez decidida (Si/No)
    # la posicion vuelve al panel normal: se evalua stop, riesgo y peso.
    if inherited_type in {"A", "B"} and decision not in {"Si", "No"}:
        return INHERITED_STATUS[inherited_type]
    if price < current:
        return STATUS_STOPPED
    if risk_pct is not None and risk_pct > cfg.max_risk_position_pct:
        return STATUS_OVERRISK
    if weight is not None and weight > cfg.max_weight_hold_pct:
        return STATUS_OVERWEIGHT
    if inherited_type == "0":
        return STATUS_INHERITED_0
    return STATUS_OK


def _differs(reported: float | None, model: float | None) -> bool:
    if reported is None or model is None or model <= 0.0:
        return False
    return abs(reported - model) / model > 0.005


def _opt_float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _opt_date(value: object) -> dt.date | None:
    if isinstance(value, dt.date):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return dt.date.fromisoformat(value.strip())
        except ValueError:
            return None
    return None


def _opt_str_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(str(item) for item in value if item)


def summarize(
    rows: Sequence[ComplianceRow],
    *,
    cash_pct: float | None,
    cfg: ComplianceConfig,
    factor_cfg: FactorsConfig | None = None,
    themes: Mapping[str, str] | None = None,
    regime_mode: str = "",
    drawdown_pct: float | None = None,
    equity_clp: float | None = None,
) -> ComplianceSummary:
    """Agrega el panel: riesgo abierto, caja, factores, temas, infracciones y modo.

    Desde v8 la base de **todos** los pesos y topes es el patrimonio (P12), no el
    capital invertido. ``equity_clp`` es el equity XTB del encabezado (incluye caja);
    sin el se cae al valor sumado de las filas para no romper una corrida parcial.
    """
    invested = sum(row.value_clp or 0.0 for row in rows)
    base = equity_clp if equity_clp is not None and equity_clp > 0.0 else invested
    open_risk = sum(row.risk_pct or 0.0 for row in rows)
    factor_pcts: dict[str, float] = {}
    factor_max: dict[str, float] = {}
    if factor_cfg is not None and base > 0.0:
        for name, spec in factor_cfg.factors.items():
            total = 0.0
            for row in rows:
                theme = (themes or {}).get(row.ticker, "")
                if row.ticker in spec.tickers or (theme and theme in spec.themes):
                    total += row.value_clp or 0.0
            factor_pcts[name] = total / base * 100.0
            factor_max[name] = min(spec.max_pct, factor_cfg.max_factor_pct)

    theme_pcts: dict[str, float] = {}
    aggregate: dict[str, float] = {}
    if base > 0.0:
        for row in rows:
            theme = (themes or {}).get(row.ticker) or "sin_tema"
            pct = (row.value_clp or 0.0) / base * 100.0
            theme_pcts[theme] = theme_pcts.get(theme, 0.0) + pct
            if _is_semis_theme(theme):
                aggregate["semiconductores y equipos"] = aggregate.get("semiconductores y equipos", 0.0) + pct
    all_themes = {**theme_pcts, **aggregate}
    biggest_theme = max(all_themes, key=lambda name: all_themes[name]) if all_themes else ""
    biggest_pct = all_themes.get(biggest_theme) if biggest_theme else None

    base_theme_max = cfg.max_theme_pct
    if factor_cfg is not None:
        base_theme_max = min(base_theme_max, factor_cfg.max_theme_pct)
    inherited_counts: dict[str, int] = {"0": 0, "A": 0, "B": 0}
    for row in rows:
        if row.inherited_type in inherited_counts:
            inherited_counts[row.inherited_type] += 1
    pending = tuple(
        row.ticker
        for row in rows
        if row.inherited_type in {"A", "B"} and row.decision not in {"Si", "No"}
    )
    overweight = tuple(
        row.ticker
        for row in rows
        if row.weight_equity_pct is not None and row.weight_equity_pct > cfg.max_weight_hold_pct
    )
    overrisk = tuple(
        row.ticker
        for row in rows
        if row.risk_pct is not None and row.risk_pct > cfg.max_risk_position_pct
    )
    # Infracciones = reglas duras incumplidas, no decisiones pendientes. Una
    # heredada tipo A/B sin decision se informa aparte (``decision_pending``) y
    # una tipo 0 es sana: su stop P8 ya esta registrado.
    infractions = sum(
        1 for row in rows if row.status in {STATUS_STOPPED, STATUS_OVERRISK, STATUS_OVERWEIGHT}
    )
    draw_mode = mode_from_drawdown(drawdown_pct, cfg)
    mode = regime_mode if _MODE_RANK.get(regime_mode, 0) >= _MODE_RANK.get(draw_mode, 0) else draw_mode
    # La amplitud se calcula con la fila del servicio (price_vs_sma200), no aca.
    return ComplianceSummary(
        open_risk_pct=open_risk,
        max_open_risk_pct=cfg.max_open_risk_pct,
        cash_pct=cash_pct,
        min_cash_pct=cfg.min_cash_pct,
        cash_band_max_pct=cfg.cash_band_max_pct,
        equity_clp=base if base > 0.0 else None,
        invested_clp=invested,
        factor_pcts=factor_pcts,
        factor_max_pct=factor_max,
        theme_pcts=theme_pcts,
        aggregate_theme_pcts=aggregate,
        max_theme_pct=base_theme_max,
        biggest_theme=biggest_theme,
        biggest_theme_pct=biggest_pct,
        infractions=infractions,
        mode=mode or MODE_NORMAL,
        breadth_pct=None,
        earnings_5=tuple(row.ticker for row in rows if _within(row.earnings_sessions, 5)),
        earnings_10=tuple(row.ticker for row in rows if _within(row.earnings_sessions, 10)),
        earnings_15=tuple(row.ticker for row in rows if _within(row.earnings_sessions, 15)),
        drawdown_pct=drawdown_pct,
        drawdown_mode=draw_mode,
        regime_mode=regime_mode or MODE_NORMAL,
        next_milestone=_milestone(cfg),
        inherited_counts=inherited_counts,
        decision_pending=pending,
        overweight=overweight,
        overrisk=overrisk,
        max_weight_hold_pct=cfg.max_weight_hold_pct,
        max_weight_trim_pct=cfg.max_weight_hold_trim_pct,
        max_risk_position_pct=cfg.max_risk_position_pct,
        max_sale_per_session_pct=cfg.max_sale_per_session_pct,
        reduction_max_weeks=cfg.reduction_max_weeks,
    )


#: Temas que el prompt v9 agrega en "semiconductores y equipos" (seccion 6).
_SEMIS_THEMES = frozenset({"semiconductors", "semiconductors_ai", "technology", "semis"})


def _is_semis_theme(theme: str) -> bool:
    return theme.strip().lower() in _SEMIS_THEMES


def _within(sessions: float | None, limit: int) -> bool:
    return sessions is not None and 0 <= sessions <= limit


def _milestone(cfg: ComplianceConfig) -> str:
    """Hito de calendario mas cercano (P10): compras nuevas o liquidacion."""
    # El servicio pasa la fecha de hoy al render; aca solo se nombran las fechas.
    return (
        f"compras nuevas hasta {cfg.purchases_until.isoformat()}; "
        f"liquidacion escalonada {cfg.purchases_until.isoformat()} - {cfg.liquidate_until.isoformat()}"
    )


def mode_from_drawdown(drawdown_pct: float | None, cfg: ComplianceConfig) -> str:
    """P9: cautela con caida >= 8 % y defensivo con >= 15 % (ajustada por aportes)."""
    if drawdown_pct is None:
        return MODE_NORMAL
    loss = abs(min(0.0, drawdown_pct))
    if loss >= cfg.defensive_drawdown_pct:
        return MODE_DEFENSIVE
    if loss >= cfg.caution_drawdown_pct:
        return MODE_CAUTION
    return MODE_NORMAL


def breadth_pct(entries: Sequence[Mapping[str, object]]) -> float | None:
    """% de posiciones bajo su SMA200 a partir de filas con ``price_vs_sma200_pct``."""
    known = [row for row in entries if isinstance(row.get("price_vs_sma200_pct"), (int, float))]
    if not known:
        return None
    below = 0
    for row in known:
        value = row.get("price_vs_sma200_pct")
        if isinstance(value, (int, float)) and float(value) < 0.0:
            below += 1
    return below / len(known) * 100.0
