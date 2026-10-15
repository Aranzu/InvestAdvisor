"""Traduce ``PROMPT PORTAFOLIO`` a codigo puro: niveles, disparadores y ejes.

El prompt original le pedia a la IA calcular a mano el nivel de revision, el nivel de
alarma (Chandelier), el R:R de la zona tactica, los disparadores T1-T6, los motivos
M1-M7 y los ejes E1-E4. Eso consume tokens y, peor, no es reproducible entre corridas.

Este modulo hace esa aritmetica una sola vez, con umbrales que salen de
``strategy.review`` (nada cocido en el codigo) y sin I/O: recibe campos y devuelve
hechos. Es **informativo**: no dispara ordenes ni cambia las reglas del motor. Donde el
dato no existe (guia trimestral, revisiones de EPS a 60 dias, eventos binarios) el
resultado lo declara como no evaluable en vez de inventar un disparador.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass

from swing.core.fundamentals import AxisRatings
from swing.core.models import ReviewAxesConfig, ReviewConfig

#: Nivel de precio que NO se puede calcular por falta de ATR (o precio invalido).
_NO_LEVEL = "sin nivel"


def _num(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return None if math.isnan(number) else number
    return None


def _field(fields: Mapping[str, object], key: str) -> float | None:
    return _num(fields.get(key))


def _fmt(value: float | None) -> str:
    return "-" if value is None else f"{value:.2f}"


# --------------------------------------------------------------------------- #
# Niveles (nivel de revision, nivel de alarma, zona tactica, objetivo)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Levels:
    """Niveles de la revision. Son ALERTAS, no ordenes.

    ``review`` es la SMA50 menos ``review_atr_mult`` ATR; ``alarm`` es el Chandelier
    (maximo de 20 sesiones menos ``alarm_atr_mult`` ATR) acotado por el piso
    (``alarm_floor_atr`` ATR bajo el precio) y el tope (``alarm_max_distance_pct``).
    """

    review: float | None = None
    review_perforated: bool = False
    """``True`` si el precio ya quedo bajo la SMA50: el nivel de revision no vale."""
    review_distance_pct: float | None = None
    alarm: float | None = None
    alarm_distance_pct: float | None = None
    alarm_rule: str = ""
    """Regla efectiva: Chandelier, piso, tope o sin nivel."""
    risk_pct: float | None = None
    """Riesgo al nivel de alarma como % del patrimonio (usa el valor post-venta)."""
    tactical_zone: float | None = None
    tactical_rr: float | None = None
    objective: float | None = None
    objective_state: str = "sin objetivo"
    """``lejos``/``cerca``/``alcanzado``/``superado``/``sin objetivo``."""
    objective_valid: bool = False
    """``True`` si N analistas alcanza el minimo: el objetivo es de referencia."""
    catastrophe_floor: float | None = None
    """Piso de catastrofe (15-20 % bajo el precio): la unica orden real admitida."""
    stop_raise_due: bool = False
    """``True`` si el avance alcanzo ``level_raise_r``: corresponde subir el stop a breakeven."""


def compute_levels(
    fields: Mapping[str, object],
    *,
    price: float | None,
    value_clp: float | None = None,
    patrimonio_clp: float | None = None,
    weight_invested_pct: float | None = None,
    position_max_pct: float | None = None,
    earnings_sessions: float | None = None,
    progress_r: float | None = None,
    policy: ReviewConfig | None,
) -> Levels:
    """Calcula los niveles de v4 a partir de los campos del ``stats``.

    Sin politica (``strategy.review`` ausente) devuelve un ``Levels`` vacio para no
    inventar umbrales en el codigo.
    """
    if policy is None or price is None or price <= 0.0:
        return Levels()
    cfg = policy.levels
    atr = _field(fields, "atr")
    sma50 = _field(fields, "sma50")
    high_20 = _field(fields, "high_20")

    review: float | None = None
    review_perforated = False
    review_distance: float | None = None
    if sma50 is not None and sma50 > 0.0 and atr is not None and atr > 0.0:
        review = sma50 - cfg.review_atr_mult * atr
        review_perforated = price < sma50
        if not review_perforated and review < price:
            review_distance = (price - review) / price * 100.0

    alarm: float | None = None
    alarm_rule = _NO_LEVEL
    alarm_distance: float | None = None
    if high_20 is not None and high_20 > 0.0 and atr is not None and atr > 0.0:
        candidate = high_20 - cfg.alarm_atr_mult * atr
        alarm_rule = f"Chandelier {cfg.alarm_atr_mult:g}xATR (aprox Max20)"
        floor = price - cfg.alarm_floor_atr * atr
        if candidate > floor:
            candidate = floor
            alarm_rule = f"piso {cfg.alarm_floor_atr:g}xATR"
        cap = price * (1.0 - cfg.alarm_max_distance_pct / 100.0)
        if candidate < cap:
            candidate = cap
            alarm_rule = f"tope {cfg.alarm_max_distance_pct:g}% (dentro del ruido)"
        alarm = candidate
        alarm_distance = (price - alarm) / price * 100.0

    risk: float | None = None
    if (
        alarm is not None
        and value_clp is not None
        and value_clp > 0.0
        and patrimonio_clp is not None
        and patrimonio_clp > 0.0
    ):
        risk = (value_clp * (price - alarm) / price) / patrimonio_clp * 100.0

    tactical, rr = _tactical_zone(fields, price=price, alarm=alarm, min_rr=cfg.tactical_min_rr)
    objective, objective_state, objective_valid = _objective(
        fields,
        price=price,
        min_analysts=cfg.objective_min_analysts,
        plus1=policy.axes.upside_plus1_pct,
        zero=policy.axes.upside_zero_pct,
    )

    catastrophe: float | None = None
    high_weight = (
        weight_invested_pct is not None and position_max_pct is not None and weight_invested_pct > position_max_pct
    )
    near_earnings = earnings_sessions is not None and earnings_sessions <= policy.axes.earnings_minus1_sessions
    if near_earnings:
        # Con earnings encima se admite el borde lejano del rango (mas aire para el evento).
        catastrophe = price * (1.0 - cfg.catastrophe_max_pct / 100.0)
    elif high_weight:
        catastrophe = price * (1.0 - cfg.catastrophe_min_pct / 100.0)

    stop_raise_due = progress_r is not None and progress_r >= cfg.level_raise_r

    return Levels(
        review=review,
        review_perforated=review_perforated,
        review_distance_pct=review_distance,
        alarm=alarm,
        alarm_distance_pct=alarm_distance,
        alarm_rule=alarm_rule,
        risk_pct=risk,
        tactical_zone=tactical,
        tactical_rr=rr,
        objective=objective,
        objective_state=objective_state,
        objective_valid=objective_valid,
        catastrophe_floor=catastrophe,
        stop_raise_due=stop_raise_due,
    )


def _tactical_zone(
    fields: Mapping[str, object], *, price: float, alarm: float | None, min_rr: float
) -> tuple[float | None, float | None]:
    """Resistencia mas cercana sobre el precio con R:R >= ``min_rr``.

    La zona tactica es **informativa**: no es objetivo de tesis ni dispara ventas.
    """
    if alarm is None or alarm >= price:
        return None, None
    risk = price - alarm
    if risk <= 0.0:
        return None, None
    candidates = [
        value
        for value in (_field(fields, "high_20"), _field(fields, "high_50"), _field(fields, "high_52w"))
        if value is not None and value > price
    ]
    for level in sorted(candidates):
        if (level - price) / risk >= min_rr:
            return level, (level - price) / risk
    return None, None


def _objective(
    fields: Mapping[str, object],
    *,
    price: float,
    min_analysts: int,
    plus1: float,
    zero: float,
) -> tuple[float | None, str, bool]:
    target = _field(fields, "target_price")
    analysts = _field(fields, "analyst_count")
    if target is None or target <= 0.0 or analysts is None or analysts < min_analysts:
        return None, "sin objetivo", False
    upside = (target / price - 1.0) * 100.0
    if price > target:
        return target, "superado", True
    if upside <= zero:
        return target, "alcanzado", True
    if upside < plus1:
        return target, "cerca", True
    return target, "lejos", True


# --------------------------------------------------------------------------- #
# Disparadores T1-T6 (lo que el stats alcanza a medir)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Trigger:
    code: str
    detail: str


#: Disparadores que necesitan fuente primaria o serie historica que el stats no trae.
TRIGGERS_EXTERNAL: tuple[str, ...] = ("T1", "T2", "T3", "T4", "T5")
"""T1 guia, T2 desaceleracion a/a, T3 margen -300 pb a/a, T4 revision EPS 60 d, T5 evento.

Se declaran como no evaluables de forma determinista; la IA los cubre con la web.
"""


def detect_triggers(fields: Mapping[str, object], *, policy: ReviewConfig | None) -> tuple[Trigger, ...]:
    """Disparadores T detectables solo con el ``stats``.

    Lo que no se puede medir sin fuente primaria (T1-T5) se deja fuera; el informe lo
    declara como cobertura pendiente en vez de fabricar un disparador.
    """
    if policy is None:
        return ()
    cfg = policy.triggers
    out: list[Trigger] = []
    debt = _field(fields, "debt_to_equity")
    current = _field(fields, "current_ratio")
    if debt is not None and current is not None and debt > cfg.debt_to_equity_max and current < cfg.current_ratio_max:
        out.append(
            Trigger(
                "T6",
                f"balance: deuda/equity {debt:.2f} > {cfg.debt_to_equity_max:g} y "
                f"liquidez corriente {current:.2f} < {cfg.current_ratio_max:g}",
            )
        )
    return tuple(out)


# --------------------------------------------------------------------------- #
# Motivos M1-M7 (los que se derivan del stats y del veredicto de la IA)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Motive:
    code: str
    detail: str


def find_motives(
    *,
    triggers: tuple[Trigger, ...],
    levels: Levels,
    fields: Mapping[str, object],
    momentum: bool,
    policy: ReviewConfig | None,
    thesis_weak: bool = False,
) -> tuple[Motive, ...]:
    """Motivos de venta admitidos, derivados de hechos del ``stats``.

    M1 tesis deteriorada, M2 objetivo alcanzado sin momentum, M3 ruptura tecnica CON
    fundamentales debiles, M4 valorizacion exigente sin respaldo, M5 pico de ciclo
    (margen alto con crecimiento en baja). M6/M7 (evento binario, riesgo regulatorio)
    necesitan fuente externa y no se inventan aqui.
    """
    if policy is None:
        return ()
    out: list[Motive] = []
    if triggers or thesis_weak:
        detalle = "; ".join(f"{t.code} {t.detail}" for t in triggers) or "tesis debilitada por la IA"
        out.append(Motive("M1", f"tesis deteriorada: {detalle}"))

    if levels.objective_valid and levels.objective_state in {"alcanzado", "superado"} and not momentum:
        out.append(Motive("M2", f"objetivo {levels.objective_state} sin momentum"))

    below200 = (_field(fields, "price_vs_sma200_pct") or 0.0) < 0.0
    rs6 = _field(fields, "rs_pct")
    rupture = below200 and rs6 is not None and rs6 < 0.0
    cfg = policy.axes
    if rupture and _weak_fundamentals_count(fields, policy) >= cfg.weak_fundamentals_min:
        out.append(Motive("M3", "ruptura tecnica (bajo SMA200 y RS 6M < 0) con fundamentales debiles"))

    forward_pe = _field(fields, "forward_pe")
    eps_next = _field(fields, "eps_next_y")
    over_target = levels.objective_valid and levels.objective_state == "superado"
    demanding = (
        forward_pe is not None and forward_pe > cfg.demanding_forward_pe and eps_next is not None and eps_next <= 0.0
    )
    if (over_target and not momentum) or demanding:
        out.append(Motive("M4", "valorizacion exigente sin respaldo"))

    oper = _field(fields, "oper_margin")
    if oper is not None and oper >= cfg.cycle_oper_margin_pct and _growth_decelerating(fields):
        out.append(Motive("M5", f"pico de ciclo: margen operativo {oper:.0f} % con crecimiento en baja"))
    return tuple(out)


def _growth_decelerating(fields: Mapping[str, object]) -> bool:
    """Crecimiento en baja: ventas o ganancias a/a planas/negativas, o EPS proximo a la baja."""
    revenue = _field(fields, "revenue_growth")
    earnings = _field(fields, "earnings_growth")
    eps_next = _field(fields, "eps_next_y")
    return any(value is not None and value <= 0.0 for value in (revenue, earnings, eps_next))


def _weak_fundamentals_count(fields: Mapping[str, object], policy: ReviewConfig) -> int:
    """Cuenta los criterios de "fundamentales debiles" de M3 medibles en el stats."""
    count = 0
    revenue = _field(fields, "revenue_growth")
    earnings = _field(fields, "earnings_growth")
    oper = _field(fields, "oper_margin")
    pfcf = _field(fields, "pfcf")
    eps_next = _field(fields, "eps_next_y")
    if revenue is not None and revenue <= 0.0:
        count += 1
    if earnings is not None and earnings <= 0.0:
        count += 1
    if oper is not None and oper <= 0.0:
        count += 1
    if pfcf is not None and pfcf <= 0.0:
        count += 1
    if eps_next is not None and eps_next <= 0.0:
        count += 1
    return count


# --------------------------------------------------------------------------- #
# Ejes E1-E4, F/M y accion implicita
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class AxisScore:
    """Ejes E1-E4 (``-2..+2``) de ``PROMPT PORTAFOLIO`` mas sus subtotales."""

    e1: int = 0
    e2: int = 0
    e3: int = 0
    e4: int = 0
    momentum: bool = False
    evaluated: tuple[bool, bool, bool, bool] = (False, False, False, False)

    @property
    def f(self) -> int:
        """Subtotal fundamental ``E1 + E2``."""
        return self.e1 + self.e2

    @property
    def m(self) -> int:
        """Subtotal de mercado y evento ``E3 + E4``."""
        return self.e3 + self.e4

    @property
    def total(self) -> int:
        return self.e1 + self.e2 + self.e3 + self.e4

    def as_tuple(self) -> tuple[int, int, int, int]:
        return (self.e1, self.e2, self.e3, self.e4)

    def formatted(self) -> str:
        return " / ".join(_signed(value) for value in self.as_tuple())


def _signed(value: int) -> str:
    return f"+{value}" if value > 0 else str(value)


def score_axes(
    *,
    fields: Mapping[str, object],
    price: float | None,
    fundamental: AxisRatings | None,
    levels: Levels,
    triggers: tuple[Trigger, ...],
    policy: ReviewConfig | None,
    thesis_broken: bool = False,
    regime_low: bool = False,
) -> AxisScore:
    """Puntua los cuatro ejes de v4 con los datos disponibles.

    E1 usa los bloques calidad y crecimiento del score fundamental (la tesis del
    negocio) y los disparadores T; E2 la valorizacion y el estado del objetivo; E3 el
    tecnico; E4 el riesgo de evento (earnings y colchon).
    """
    if policy is None:
        return AxisScore()
    cfg = policy.axes
    momentum = _is_momentum(fields)
    e1, e1_ok = _axis_e1(fundamental, triggers, thesis_broken)
    e2, e2_ok = _axis_e2(fundamental, levels, policy, fields)
    e3, e3_ok = _axis_e3(fields, price, cfg)
    e4, e4_ok = _axis_e4(fields, price, cfg, regime_low)
    return AxisScore(
        e1=e1,
        e2=e2,
        e3=e3,
        e4=e4,
        momentum=momentum,
        evaluated=(e1_ok, e2_ok, e3_ok, e4_ok),
    )


def _is_momentum(fields: Mapping[str, object]) -> bool:
    rs1 = _field(fields, "rs_1m_pct")
    rs3 = _field(fields, "rs_3m_pct")
    vs50 = _field(fields, "price_vs_sma50_pct")
    return (rs1 or 0.0) > 0.0 and (rs3 or 0.0) > 0.0 and (vs50 or 0.0) > 0.0


def _axis_e1(fundamental: AxisRatings | None, triggers: tuple[Trigger, ...], thesis_broken: bool) -> tuple[int, bool]:
    if len(triggers) >= 2 or thesis_broken:
        return -2, True
    if len(triggers) == 1:
        return -1, True
    if fundamental is None or not (fundamental.available[1] or fundamental.available[2]):
        return 0, False
    return _round_axis((fundamental.quality + fundamental.growth) / 2.0), True


def _axis_e2(
    fundamental: AxisRatings | None,
    levels: Levels,
    policy: ReviewConfig,
    fields: Mapping[str, object],
) -> tuple[int, bool]:
    cfg = policy.axes
    if levels.objective_valid:
        upside = _field(fields, "target_upside_pct")
        if levels.objective_state == "superado" or (upside is not None and upside <= cfg.upside_zero_pct):
            e2 = -1
            rs6 = _field(fields, "rs_pct")
            if levels.objective_state == "superado" and rs6 is not None and rs6 <= 0.0:
                e2 = -2
        elif upside is not None and upside >= cfg.upside_plus2_pct:
            e2 = 2
        elif upside is not None and upside >= cfg.upside_plus1_pct:
            e2 = 1
        else:
            e2 = 0
    else:
        if fundamental is None or not (fundamental.available[0] or fundamental.available[3]):
            return 0, False
        e2 = _round_axis((fundamental.value + fundamental.projections) / 2.0)
    oper = _field(fields, "oper_margin")
    if oper is not None and oper >= cfg.cycle_oper_margin_pct and _growth_decelerating(fields):
        e2 = max(-2, e2 - 1)
    return e2, True


def _axis_e3(fields: Mapping[str, object], price: float | None, cfg: ReviewAxesConfig) -> tuple[int, bool]:
    vs50 = _field(fields, "price_vs_sma50_pct")
    vs200 = _field(fields, "price_vs_sma200_pct")
    rs1 = _field(fields, "rs_1m_pct")
    rs3 = _field(fields, "rs_3m_pct")
    rs6 = _field(fields, "rs_pct")
    rsi = _field(fields, "rsi")
    if vs50 is None or vs200 is None or rsi is None:
        return 0, False
    if (
        vs50 > 0.0
        and vs200 > 0.0
        and (rs3 or 0.0) > 0.0
        and (rs6 or 0.0) > 0.0
        and cfg.rsi_momentum_low <= rsi <= cfg.rsi_momentum_high
    ):
        return 2, True
    if vs200 > 0.0:
        return 1, True
    low20 = _field(fields, "low_20")
    near_low = (
        price is not None and low20 is not None and low20 > 0.0 and price <= low20 * (1.0 + cfg.near_low_20_pct / 100.0)
    )
    if vs50 < 0.0 and (rs1 or 0.0) < 0.0 and (rs3 or 0.0) < 0.0 and (rs6 or 0.0) < 0.0 and near_low:
        return -2, True
    if vs200 < 0.0 or ((rs3 or 0.0) < 0.0 and (rs6 or 0.0) < 0.0):
        return -1, True
    return 0, True


def _axis_e4(
    fields: Mapping[str, object],
    price: float | None,
    cfg: ReviewAxesConfig,
    regime_low: bool,
) -> tuple[int, bool]:
    """E4 de v4; el +2 (catalizador positivo fechado) y el -2 (evento binario) son externos."""
    low20 = _field(fields, "low_20")
    sessions = _field(fields, "earnings_sessions")
    if sessions is None:
        return 1 if regime_low else 0, True
    cushion = ((price / low20 - 1.0) * 100.0) if price and low20 and low20 > 0.0 else None
    if sessions > cfg.earnings_plus1_sessions:
        return 1, True
    strict = sessions <= cfg.earnings_minus1_sessions
    required = cfg.earnings_cushion_pct if strict else cfg.earnings_cushion_loose_pct
    if cushion is None:
        return -1, True
    enough = cushion > required if strict else cushion >= required
    return (0 if enough else -1), True


def _round_axis(value: float) -> int:
    """Redondea un eje fraccionario ``-2..+2`` al entero mas cercano."""
    rounded = int(value + (0.5 if value >= 0 else -0.5))
    return max(-2, min(2, rounded))


def implied_action(score: AxisScore, *, policy: ReviewConfig | None) -> str:
    """Accion implicita de v4 (los puntajes generan CANDIDATOS, no deciden solos).

    Con ``F > 0`` no hay venta salvo veto ``E1 = -2`` (que pide, como minimo, una venta
    parcial). El motor sigue decidiendo los niveles reales; esto es la lectura de v4.
    """
    if policy is None:
        return ""
    cfg = policy.axes
    if score.e1 <= -2:
        return "Vender parcial (minimo)"
    if score.f > 0:
        return "Mantener"
    if score.total >= cfg.action_maintain_total:
        return "Mantener"
    if score.total <= cfg.action_sell_total:
        return "Candidato a vender" if score.f <= 0 else "Mantener con vigilancia"
    return "Mantener con vigilancia"


# --------------------------------------------------------------------------- #
# Agregado
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class PositionVerdict:
    """Todo lo que v4 derivaba a mano para una posicion: niveles, T, M, ejes y accion."""

    levels: Levels
    triggers: tuple[Trigger, ...]
    motives: tuple[Motive, ...]
    axes: AxisScore
    action: str
    external_pending: tuple[str, ...] = ()
    """Disparadores T que necesitan fuente externa (los cubre la IA con la web)."""


def compute(
    fields: Mapping[str, object],
    *,
    price: float | None,
    fundamental: AxisRatings | None,
    value_clp: float | None = None,
    patrimonio_clp: float | None = None,
    weight_invested_pct: float | None = None,
    position_max_pct: float | None = None,
    earnings_sessions: float | None = None,
    progress_r: float | None = None,
    thesis_broken: bool = False,
    thesis_weak: bool = False,
    regime_low: bool = False,
    policy: ReviewConfig | None,
) -> PositionVerdict:
    """Ensambla el veredicto determinista de v4 para una posicion."""
    levels = compute_levels(
        fields,
        price=price,
        value_clp=value_clp,
        patrimonio_clp=patrimonio_clp,
        weight_invested_pct=weight_invested_pct,
        position_max_pct=position_max_pct,
        earnings_sessions=earnings_sessions,
        progress_r=progress_r,
        policy=policy,
    )
    triggers = detect_triggers(fields, policy=policy)
    axes = score_axes(
        fields=fields,
        price=price,
        fundamental=fundamental,
        levels=levels,
        triggers=triggers,
        policy=policy,
        thesis_broken=thesis_broken,
        regime_low=regime_low,
    )
    momentum = axes.momentum
    motives = find_motives(
        triggers=triggers,
        levels=levels,
        fields=fields,
        momentum=momentum,
        policy=policy,
        thesis_weak=thesis_weak,
    )
    return PositionVerdict(
        levels=levels,
        triggers=triggers,
        motives=motives,
        axes=axes,
        action=implied_action(axes, policy=policy),
        external_pending=TRIGGERS_EXTERNAL if policy is not None else (),
    )


# --------------------------------------------------------------------------- #
# Registro de decisiones (una linea por activo, para medir a 30 y 90 dias)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class DecisionRecord:
    """Una linea del registro de decisiones (v4, seccion 9).

    Se guarda para pegar/medir en la proxima corrida y comparar niveles: la regla de
    ascenso dice que los niveles no bajan.
    """

    date: str
    ticker: str
    action: str
    price: float | None = None
    entry_usd: float | None = None
    review_level: float | None = None
    alarm_level: float | None = None
    objective: float | None = None
    motive: str = ""
    trigger: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "date": self.date,
            "ticker": self.ticker,
            "action": self.action,
            "price": self.price,
            "entry_usd": self.entry_usd,
            "review_level": self.review_level,
            "alarm_level": self.alarm_level,
            "objective": self.objective,
            "motive": self.motive,
            "trigger": self.trigger,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> DecisionRecord | None:
        ticker = data.get("ticker")
        if not isinstance(ticker, str) or not ticker:
            return None
        return cls(
            date=str(data.get("date") or ""),
            ticker=ticker,
            action=str(data.get("action") or ""),
            price=_num(data.get("price")),
            entry_usd=_num(data.get("entry_usd")),
            review_level=_num(data.get("review_level")),
            alarm_level=_num(data.get("alarm_level")),
            objective=_num(data.get("objective")),
            motive=str(data.get("motive") or ""),
            trigger=str(data.get("trigger") or ""),
        )
