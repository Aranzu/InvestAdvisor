"""Panel de cumplimiento del ``PROMPT PORTAFOLIO`` v7 (P1-P11).

Modulo **puro**: recibe los hechos ya resueltos (entrada, precio, ATR, valor y el
estado previo del ledger) y devuelve los niveles y el estado de cada posicion.
No toca red, disco ni reloj.

La regla central es que **todo stop nace de la entrada**: el stop inicial se
calcula con P8 y el vigente solo sube (break-even, entrada + 1R, trailing). Nunca
se re-ancla al precio actual, que es el error que el prompt v7 corrige.
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
STATUS_INHERITED = "HEREDADA_EN_PERDIDA"
STATUS_NO_STOP = "SIN_STOP"

STATUSES = (
    STATUS_OK,
    STATUS_OVERWEIGHT,
    STATUS_OVERRISK,
    STATUS_STOPPED,
    STATUS_INHERITED,
    STATUS_NO_STOP,
)

MODE_NORMAL = "normal"
MODE_CAUTION = "cautela"
MODE_DEFENSIVE = "defensivo"
_MODE_RANK = {MODE_NORMAL: 0, MODE_CAUTION: 1, MODE_DEFENSIVE: 2, "": 0}


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    """Un stop persistido por ticker (``state/ledger.json``)."""

    ticker: str
    entry_usd: float
    first_entry: dt.date
    initial_stop: float
    current_stop: float
    take_profit: float | None = None
    transition_stop: float | None = None
    inherited: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "ticker": self.ticker,
            "entry_usd": self.entry_usd,
            "first_entry": self.first_entry.isoformat(),
            "initial_stop": self.initial_stop,
            "current_stop": self.current_stop,
            "take_profit": self.take_profit,
            "transition_stop": self.transition_stop,
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
        return cls(
            ticker=ticker,
            entry_usd=entry,
            first_entry=first,
            initial_stop=initial,
            current_stop=current,
            take_profit=_opt_float(data.get("take_profit")),
            transition_stop=_opt_float(data.get("transition_stop")),
            inherited=bool(data.get("inherited", False)),
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
    stop_xtb: float | None = None
    tp_xtb: float | None = None
    stop_xtb_differs: bool = False
    tp_xtb_differs: bool = False
    earnings_sessions: float | None = None


@dataclass(frozen=True, slots=True)
class ComplianceSummary:
    """Agregados del panel: limites P1-P11 y modo vigente."""

    open_risk_pct: float | None = None
    max_open_risk_pct: float | None = None
    cash_pct: float | None = None
    min_cash_pct: float | None = None
    cash_band_max_pct: float | None = None
    factor_pcts: Mapping[str, float] = field(default_factory=dict)
    factor_max_pct: Mapping[str, float] = field(default_factory=dict)
    theme_pcts: Mapping[str, float] = field(default_factory=dict)
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
    next_milestone: str = ""


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
        formula = "objetivo medio (N>=" + str(cfg.take_profit_min_analysts) + ")"
        if target_price > cap:
            formula = f"objetivo medio topado +{cfg.take_profit_max_upside_pct:g}%"
    if previous is not None and previous > 0.0:
        candidate = max(previous, candidate)
        if previous > cap:
            formula = "vigente (solo sube)"
    return candidate, formula


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
    high_20: float | None,
    previous: LedgerEntry | None,
    stop_xtb: float | None,
    tp_xtb: float | None,
    target_price: float | None,
    analyst_count: float | None,
    earnings_sessions: float | None,
    cfg: ComplianceConfig,
) -> ComplianceRow:
    """Niveles y estado de una posicion, sin bajar nunca el stop vigente."""
    initial = initial_stop(entry, atr, cfg)
    if initial is None or entry is None or price is None:
        return ComplianceRow(
            ticker=ticker,
            price=price,
            entry=entry,
            atr_pct=atr_pct,
            value_clp=value_clp,
            weight_invested_pct=weight_invested_pct,
            initial_stop=initial,
            current_stop=None,
            previous_stop=previous.current_stop if previous is not None else None,
            transition_stop=previous.transition_stop if previous is not None else None,
            take_profit=previous.take_profit if previous is not None else None,
            tp_formula="sin precio" if price is None else "sin ATR",
            stop_dist_price_pct=None,
            stop_dist_entry_pct=None,
            r_multiple=None,
            risk_pct=None,
            max_weight_open_pct=None,
            status=STATUS_NO_STOP,
            inherited=bool(previous.inherited) if previous is not None else False,
            stop_xtb=stop_xtb,
            tp_xtb=tp_xtb,
            stop_xtb_differs=stop_xtb is not None,
            tp_xtb_differs=tp_xtb is not None,
            earnings_sessions=earnings_sessions,
        )

    r_value = entry - initial
    r_multiple = (price - entry) / r_value if r_value > 0.0 else None
    previous_stop = previous.current_stop if previous is not None else None
    inherited = previous.inherited if previous is not None else _is_inherited(price, initial, cfg)

    current = max(previous_stop, initial) if previous_stop is not None else initial
    transition = previous.transition_stop if previous is not None else None
    if transition is not None:
        current = max(current, transition)
        inherited = False

    # El trailing solo aplica a una posicion ya adoptada (con stop registrado) y
    # que no es una heredada en perdida: hasta la decision, su nivel es el P8.
    trail = (
        None
        if previous is None or inherited
        else _trailing_stop(
            entry=entry,
            price=price,
            high_20=high_20,
            atr=atr,
            r_value=r_value,
            r_multiple=r_multiple,
            cfg=cfg,
        )
    )
    if trail is not None and trail < price:
        # Un trailing que ya esta sobre el mercado no se aplica (seria un stop
        # inmediato): el stop inicial si puede quedar arriba y eso es una heredada.
        current = max(current, trail)

    tp, tp_formula = take_profit(
        price=price,
        target_price=target_price,
        analyst_count=analyst_count,
        previous=previous.take_profit if previous is not None else None,
        cfg=cfg,
    )

    stop_dist_price = (price - current) / price * 100.0 if price > 0.0 else None
    stop_dist_entry = (entry - current) / entry * 100.0 if entry > 0.0 else None
    risk_pct = None
    if (
        value_clp is not None
        and value_clp > 0.0
        and patrimonio_clp is not None
        and patrimonio_clp > 0.0
        and stop_dist_price is not None
    ):
        risk_pct = value_clp * max(0.0, stop_dist_price) / 100.0 / patrimonio_clp * 100.0

    max_weight = None
    if stop_dist_price is not None and stop_dist_price > 0.0:
        max_weight = cfg.risk_per_open_pct / stop_dist_price * 100.0
        high_atr = atr_pct is not None and atr_pct > cfg.high_atr_pct
        p2 = cfg.max_weight_open_high_atr_pct if high_atr else cfg.max_weight_open_pct
        max_weight = min(max_weight, p2)

    status = _status(
        price=price,
        current=current,
        inherited=inherited,
        risk_pct=risk_pct,
        weight=weight_invested_pct,
        cfg=cfg,
    )
    return ComplianceRow(
        ticker=ticker,
        price=price,
        entry=entry,
        atr_pct=atr_pct,
        value_clp=value_clp,
        weight_invested_pct=weight_invested_pct,
        initial_stop=initial,
        current_stop=current,
        previous_stop=previous_stop,
        transition_stop=transition,
        take_profit=tp,
        tp_formula=tp_formula,
        stop_dist_price_pct=stop_dist_price,
        stop_dist_entry_pct=stop_dist_entry,
        r_multiple=r_multiple,
        risk_pct=risk_pct,
        max_weight_open_pct=max_weight,
        status=status,
        inherited=inherited,
        stop_xtb=stop_xtb,
        tp_xtb=tp_xtb,
        stop_xtb_differs=_differs(stop_xtb, current),
        tp_xtb_differs=_differs(tp_xtb, tp),
        earnings_sessions=earnings_sessions,
    )


def _is_inherited(price: float, initial: float, cfg: ComplianceConfig) -> bool:
    """Sin stop registrado: heredada si el precio ya lo perdio o el stop queda muy lejos."""
    if price < initial:
        return True
    return initial < price * (1.0 - cfg.inherited_stop_max_distance_pct / 100.0)


def _trailing_stop(
    *,
    entry: float,
    price: float,
    high_20: float | None,
    atr: float | None,
    r_value: float,
    r_multiple: float | None,
    cfg: ComplianceConfig,
) -> float | None:
    """Break-even a +1,5R, entrada +1R a +3R y luego max 20 sesiones - 3 ATR."""
    candidates: list[float] = []
    if r_multiple is not None:
        if r_multiple >= cfg.enhancer_r:
            candidates.append(entry + r_value)
        if r_multiple >= cfg.breakeven_r:
            candidates.append(entry)
    if high_20 is not None and atr is not None and atr > 0.0:
        candidates.append(high_20 - cfg.trailing_atr_mult * atr)
    return max(candidates) if candidates else None


def _status(
    *,
    price: float,
    current: float,
    inherited: bool,
    risk_pct: float | None,
    weight: float | None,
    cfg: ComplianceConfig,
) -> str:
    if inherited:
        return STATUS_INHERITED
    if price < current:
        return STATUS_STOPPED
    if risk_pct is not None and risk_pct > cfg.max_risk_position_pct:
        return STATUS_OVERRISK
    if weight is not None and weight > cfg.max_weight_hold_pct:
        return STATUS_OVERWEIGHT
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


def summarize(
    rows: Sequence[ComplianceRow],
    *,
    cash_pct: float | None,
    cfg: ComplianceConfig,
    factor_cfg: FactorsConfig | None = None,
    themes: Mapping[str, str] | None = None,
    regime_mode: str = "",
    drawdown_pct: float | None = None,
) -> ComplianceSummary:
    """Agrega el panel: riesgo abierto, caja, factores, temas, infracciones y modo."""
    invested = sum(row.value_clp or 0.0 for row in rows)
    open_risk = sum(row.risk_pct or 0.0 for row in rows)
    factor_pcts: dict[str, float] = {}
    factor_max: dict[str, float] = {}
    if factor_cfg is not None and invested > 0.0:
        for name, spec in factor_cfg.factors.items():
            total = 0.0
            for row in rows:
                theme = (themes or {}).get(row.ticker, "")
                if row.ticker in spec.tickers or (theme and theme in spec.themes):
                    total += row.value_clp or 0.0
            factor_pcts[name] = total / invested * 100.0
            factor_max[name] = min(spec.max_pct, factor_cfg.max_factor_pct)

    theme_pcts: dict[str, float] = {}
    if invested > 0.0:
        for row in rows:
            theme = (themes or {}).get(row.ticker) or "sin_tema"
            theme_pcts[theme] = theme_pcts.get(theme, 0.0) + (row.value_clp or 0.0) / invested * 100.0
    biggest_theme = max(theme_pcts, key=lambda name: theme_pcts[name]) if theme_pcts else ""
    biggest_pct = theme_pcts.get(biggest_theme) if biggest_theme else None

    base_theme_max = cfg.max_theme_pct
    if factor_cfg is not None:
        base_theme_max = min(base_theme_max, factor_cfg.max_theme_pct)
    infractions = sum(1 for row in rows if row.status != STATUS_OK)
    draw_mode = mode_from_drawdown(drawdown_pct, cfg)
    mode = regime_mode if _MODE_RANK.get(regime_mode, 0) >= _MODE_RANK.get(draw_mode, 0) else draw_mode
    # La amplitud se calcula con la fila del servicio (price_vs_sma200), no aca.
    return ComplianceSummary(
        open_risk_pct=open_risk,
        max_open_risk_pct=cfg.max_open_risk_pct,
        cash_pct=cash_pct,
        min_cash_pct=cfg.min_cash_pct,
        cash_band_max_pct=cfg.cash_band_max_pct,
        factor_pcts=factor_pcts,
        factor_max_pct=factor_max,
        theme_pcts=theme_pcts,
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
    )


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
