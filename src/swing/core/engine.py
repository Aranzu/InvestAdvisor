"""Motor puro: ``decide()`` (spec v3.1 secciones 5 y 11.3).

El motor es **una sola funcion**: ``review`` le pasa el estado y la configuracion
resueltos, y el estado de posicion (``core/state``) y las reglas de salida
(``core/exits``) son los mismos objetos que usa el resto del sistema.

Reglas de este modulo:

- **Puro.** No hay disco, red ni reloj de pared: los datos entran como objetos ya
  resueltos (``MarketSnapshot``, ``PortfolioState``, ``TradeState``) y la salida
  es ``NightPlan``. Un test bloquea red y disco sobre ``decide()``.
- **Nada se inventa.** Toda decision de entrada pasa por las seis puertas y el
  setup; toda decision de salida por la precedencia de la seccion 5.7. Un dato
  que falta produce un ``Rejected`` con su codigo, nunca una asuncion.
- **Los indicadores se calculan una vez por ticker** en ``IndicatorFrame`` y se
  indexan por posicion: el simulador recorre miles de sesiones y recalcular las
  medias moviles por dia seria desperdiciar el único recurso caro.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from dataclasses import dataclass, field, replace

import numpy as np
import pandas as pd

from swing.core.calendar import SessionCalendar
from swing.core.exits import ExitContext, ExitEvaluation, apply_exits, evaluate_exits, horizon_state
from swing.core.indicators import (
    atr,
    highest,
    lowest,
    rsi,
    sma,
)
from swing.core.market import PriceSeries
from swing.core.models import (
    BrokerConfig,
    HorizonState,
    KillState,
    RegimeState,
    Rejected,
    StrategyConfig,
    ThemesConfig,
)
from swing.core.portfolio import (
    CapTrim,
    PortfolioLimits,
    PortfolioState,
    PositionExposure,
    evaluate_entry,
    plan_cap_trims,
)
from swing.core.reasons import Reason
from swing.core.regime import effective_risk_pct, evaluate_regime, gate_entries
from swing.core.screening import (
    GateInputs,
    GateThresholds,
    Setup,
    check_gates,
    check_setup,
    in_reentry_cooldown,
)
from swing.core.sizing import SizingCaps, Tranche, size_trade, tp1_covers_costs
from swing.core.state import TradeState, TrancheState
from swing.core.stops import (
    InitialStop,
    entry_limit,
    initial_stop,
    risk_distance_with_buffer,
)

#: Ventana de la SMA200 para la pendiente (seccion 5.2, puerta G1).
SMA200_SLOPE_DAYS = 20

#: Ventanas de los indicadores por ticker (secciones 5.2 y 5.3). Son fijas por
#: playbook, no parametros de config: si cambian, cambia la estrategia.
SMA200_PERIOD = 200
SMA50_PERIOD = 50
SMA20_PERIOD = 20
HIGH20_PERIOD = 20
AVG_VOL20_PERIOD = 20
ADV_PERIOD = 50
SESSIONS_52_WEEKS = 252

#: Tolerancia "cerca de la SMA20" del setup pullback (seccion 5.3), en ATR.
NEAR_SMA20_ATR = 0.5

#: Meseta de confirmacion de volumen del setup pullback (seccion 5.3).
SETUP_MIN_VOLUME_RATIO = 1.2

#: Tema de respaldo cuando un ticker no esta en ``themes.yaml`` (seccion 5.8).
DEFAULT_THEME = "sin_tema"


# --------------------------------------------------------------------------- #
# Indicadores por ticker
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class IndicatorFrame:
    """Indicadores precalculados sobre la serie **reescalada** (D6).

    ``features_at(i)`` devuelve la foto en la barra ``i`` de la serie original:
    es la forma de pedir "los indicadores del cierre de tal sesion" sin recalcular
    nada por dia. Los campos se resuelven a ``None`` cuando el indicador aun no
    tiene historia: el motor distingue "no pasa" de "no se puede saber" (D1).
    """

    ticker: str
    close: pd.Series
    high: pd.Series
    low: pd.Series
    volume: pd.Series
    sma200: pd.Series
    sma50: pd.Series
    sma20: pd.Series
    atr: pd.Series
    rsi: pd.Series
    swing_low10: pd.Series
    high20: pd.Series
    low52: pd.Series
    high52: pd.Series
    adv_usd: pd.Series
    avg_vol20: pd.Series

    @classmethod
    def build(cls, series: PriceSeries, strategy: StrategyConfig) -> IndicatorFrame | None:
        """Arma el frame sobre la serie reescalada. ``None`` con menos de 1 vela."""
        if series.is_empty or series.first_date is None:
            return None
        closes = series.closes()
        highs = series.highs()
        lows = series.lows()
        volumes = series.volumes()
        stops = strategy.stops
        return cls(
            ticker=series.ticker,
            close=closes,
            high=highs,
            low=lows,
            volume=volumes,
            sma200=sma(closes, SMA200_PERIOD),
            sma50=sma(closes, SMA50_PERIOD),
            sma20=sma(closes, SMA20_PERIOD),
            atr=atr(highs, lows, closes, stops.atr_period),
            rsi=rsi(closes, stops.atr_period),
            swing_low10=lowest(lows, stops.swing_low_days),
            high20=highest(highs, HIGH20_PERIOD),
            low52=lowest(lows, SESSIONS_52_WEEKS),
            high52=highest(highs, SESSIONS_52_WEEKS),
            adv_usd=sma(closes * volumes, ADV_PERIOD),
            avg_vol20=sma(volumes, AVG_VOL20_PERIOD),
        )

    @property
    def n(self) -> int:
        return len(self.close)

    @property
    def last_index(self) -> int:
        return self.n - 1

    def features_at(self, i: int) -> TickerFeatures | None:
        """Foto de los indicadores en la barra ``i`` de la serie original."""
        if not 0 <= i < self.n:
            return None
        prev200 = self.sma200.iloc[i - SMA200_SLOPE_DAYS] if (i - SMA200_SLOPE_DAYS) >= 0 else np.nan
        return TickerFeatures(
            ticker=self.ticker,
            date=self.close.index[i].date(),
            close=_num(self.close.iloc[i]),
            high=_num(self.high.iloc[i]),
            volume=_num(self.volume.iloc[i]),
            sma200=_num(self.sma200.iloc[i]),
            sma200_prev20=_num(prev200),
            sma50=_num(self.sma50.iloc[i]),
            sma20=_num(self.sma20.iloc[i]),
            atr=_num(self.atr.iloc[i]),
            rsi=_num(self.rsi.iloc[i]),
            swing_low10=_num(self.swing_low10.iloc[i]),
            low52=_num(self.low52.iloc[i]),
            high52=_num(self.high52.iloc[i]),
            high20=_num(self.high20.iloc[i]),
            adv_usd=_num(self.adv_usd.iloc[i]),
            avg_vol20=_num(self.avg_vol20.iloc[i]),
        )

    def returns(self, lookback: int) -> float | None:
        """Retorno (multiplicativo) a ``lookback`` barras, si alcanza la historia."""
        i = self.n - 1
        if i - lookback < 0:
            return None
        base = self.close.iloc[i - lookback]
        if not isinstance(base, float) or base <= 0.0:
            return None
        return float(self.close.iloc[i] / base)

    def returns_skipping(self, lookback: int, skip: int) -> float | None:
        """Retorno entre ``lookback`` y ``skip`` barras atras (momentum 12-1: 252 y 21)."""
        i = self.n - 1 - skip
        if i - (lookback - skip) < 0 or lookback <= skip:
            return None
        base = self.close.iloc[i - (lookback - skip)]
        if not isinstance(base, float) or base <= 0.0:
            return None
        return float(self.close.iloc[i] / base)


def _num(value: object) -> float | None:
    if isinstance(value, (int, float)) and not pd.isna(value):
        return float(value)
    return None


@dataclass(frozen=True, slots=True)
class TickerFeatures:
    """La foto de una sesion para un ticker, tal como la usa el motor."""

    ticker: str
    date: dt.date
    close: float | None
    high: float | None
    volume: float | None
    sma200: float | None
    sma200_prev20: float | None
    sma50: float | None
    sma20: float | None
    atr: float | None
    rsi: float | None
    swing_low10: float | None
    low52: float | None
    high52: float | None
    high20: float | None
    adv_usd: float | None
    avg_vol20: float | None

    @property
    def usable(self) -> bool:
        return self.close is not None


def relative_strength_percentiles(
    frames: dict[str, IndicatorFrame],
    rs_lookback_days: int,
    skip_days: int = 0,
) -> dict[str, float]:
    """Percentil cross-sectional del retorno a ``rs_lookback_days`` (puerta G4).

    El percentil se calcula **dentro del universo** de frames: es el mismo
    universo con el que se corre y cambiarlo cambia que se compra (D2). Solo
    cuentan los tickers con historia suficiente; un ticker nuevo simplemente no
    tiene percentil y la puerta le produce ``INDICATOR_NOT_READY``.
    """
    values: dict[str, float] = {}
    for ticker, frame in frames.items():
        ret = frame.returns_skipping(rs_lookback_days, skip_days) if skip_days else frame.returns(rs_lookback_days)
        if ret is not None:
            values[ticker] = ret
    if not values:
        return {}
    ordered = sorted(values.values())
    n = len(ordered)
    out: dict[str, float] = {}
    import bisect

    for ticker, ret in values.items():
        count = bisect.bisect_right(ordered, ret)
        out[ticker] = count / n * 100.0
    return out


# --------------------------------------------------------------------------- #
# Snapshot y contexto de la decision
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class MarketSnapshot:
    """Lo que el motor necesita del mundo para decidir una corrida.

    ``frames`` son los indicadores ya calculados sobre las series reescaladas de
    cada ticker (el llamador los arma una vez). ``earnings`` es ``{ticker:
    fecha}`` con las fechas conocidas; en el backtest queda vacio y el informe
    declara que G6 y EARNINGS_DERISK no se simulan (no hay fecha point-in-time).
    """

    today: dt.date
    series: dict[str, PriceSeries]
    frames: dict[str, IndicatorFrame]
    equity_clp: float
    cash_clp: float
    fx: float
    spy_close: float | None
    spy_sma200: float | None
    vix: float | None
    earnings: dict[str, dt.date] = field(default_factory=dict)
    rs: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Alert:
    """Un aviso del informe. ``data_gaps`` y similares: nada que tocar, todo que leer."""

    reason: Reason
    detail: str


@dataclass(frozen=True, slots=True)
class EntrySignal:
    """Una entrada aceptada: el Buy Limit que se deja esta corrida.

    ``stop_target`` es el stop que se coloca **despues** de que el relleno
    ocurra (seccion 4.5: el dia del relleno no existe stop).
    """

    ticker: str
    theme: str
    limit_price: float
    stop_target: float
    risk_per_share: float
    volume: float
    tranches: tuple[Tranche, ...]
    single: bool
    capped_by: str | None
    equity_pct: float
    setup: Setup
    rs_percentile: float | None
    detail: str


@dataclass(frozen=True, slots=True)
class CandidateOutcome:
    """Resultado de evaluar un ticker esta corrida, entre o no entre."""

    ticker: str
    theme: str
    rejections: tuple[Rejected, ...] = ()
    entry: EntrySignal | None = None

    @property
    def entered(self) -> bool:
        return self.entry is not None

    @property
    def reasons(self) -> tuple[Reason, ...]:
        return tuple(r.reason for r in self.rejections)


@dataclass(frozen=True, slots=True)
class TradeExitJudgment:
    """El juicio de salida de una posicion y el estado al que queda esta corrida.

    ``state_after`` es el estado **aplicado** (tramos vendidos, stop subido):
    el simulador lo usa como el estado de manana y el informe lo muestra. No es
    la salida ejecutada: la venta se ejecuta en la apertura, ``evaluation``
    conserva la orden que hay que colocar.
    """

    trade: TradeState
    evaluation: ExitEvaluation
    state_after: TradeState

    @property
    def ticker(self) -> str:
        return self.trade.ticker

    @property
    def has_action(self) -> bool:
        return self.evaluation.primary is not None and self.evaluation.primary.execute


@dataclass(frozen=True, slots=True)
class NightPlan:
    """Todo lo que decide() concluye para una corrida."""

    date: dt.date
    regime: RegimeState
    regime_detail: str
    horizon: HorizonState
    regime_caution: bool
    equity_clp: float
    cash_clp: float
    fx: float
    exits: tuple[TradeExitJudgment, ...]
    entries: tuple[EntrySignal, ...]
    deferrals: tuple[EntrySignal, ...]
    candidates: tuple[CandidateOutcome, ...]
    cap_trims: tuple[CapTrim, ...]
    alerts: tuple[Alert, ...]
    kill: KillState = KillState.OK

    @property
    def has_actions(self) -> bool:
        return bool(self.exits or self.entries or self.cap_trims)


# --------------------------------------------------------------------------- #
# decide(): la funcion que comparte el backtest y la produccion
# --------------------------------------------------------------------------- #


def decide(
    snapshot: MarketSnapshot,
    state: PortfolioState,
    open_trades: dict[str, TradeState],
    strategy: StrategyConfig,
    broker: BrokerConfig,
    calendar: SessionCalendar,
    themes: ThemesConfig,
    *,
    universe: tuple[str, ...] | None = None,
    last_exit_by_ticker: dict[str, dt.date] | None = None,
    thesis_broken: dict[str, bool] | None = None,
    llm_sell: Mapping[str, float] | None = None,
    kill: KillState = KillState.OK,
    forced_trims: bool = True,
) -> NightPlan:
    """Decide que hacer esta corrida, una vez, con datos ya resueltos.

    El orden de trabajo es el del playbook: regime, salidas (con precedencia),
    cartera (heat, trim), y por ultimo entradas con las seis puertas. Un cambio
    en este orden cambia el resultado y por eso esta documentado aqui y hay un
    test que lo fija.

    Efectos de ``kill``: en modo defensivo no se proponen entradas (solo
    salidas). ``last_exit_by_ticker`` alimenta el enfriamiento de re-entrada
    (seccion 5.7): pasar una fecha de hoy implica que el ticker esta en
    cooldown. ``thesis_broken`` alimenta la regla 10 (THESIS_EXIT) y ``llm_sell``
    la regla 11 (LLM_SELL, ``veto_only=false``): la IA decide vender y el motor
    fija el volumen. Ambas tienen precedencia baja: una regla mecanica que se
    cumpla manda sobre la opinion de la IA.

    ``forced_trims=False`` (v11) apaga ``CAP_TRIM``: el tope de tema solo avisa y bloquea
    compras, no vende.
    """
    calendar_ctx = calendar

    # --- Regime (seccion 5.1) ---
    regime = evaluate_regime(
        snapshot.spy_close or 0.0, snapshot.spy_sma200 or 0.0, snapshot.vix or 0.0, strategy.regime.vix_max
    )
    regime_caution = regime is RegimeState.HALF
    horizon = horizon_state(snapshot.today, strategy.horizon)
    limits = PortfolioLimits.from_config(strategy.risk)

    alerts: list[Alert] = []
    risk_pct = effective_risk_pct(strategy.risk.risk_per_trade_pct, regime)

    if regime is RegimeState.NO_ENTRIES:
        alerts.append(Alert(reason=Reason.NO_ENTRIES_REGIME, detail="regimen NO_ENTRIES: solo se gestionan salidas"))

    # --- Salidas (seccion 5.7) ---
    trims_by_ticker = {trim.ticker: trim for trim in plan_cap_trims(state, limits)} if forced_trims else {}
    break30 = thesis_broken or {}
    sells30 = llm_sell or {}
    judgments: list[TradeExitJudgment] = []
    earnings_source_available = bool(snapshot.earnings)
    for ticker in sorted(open_trades):
        trade = open_trades[ticker]
        ctx = ExitContext(
            exits=strategy.exits,
            stops=strategy.stops,
            horizon=strategy.horizon,
            calendar=calendar_ctx,
            today=snapshot.today,
            earnings_date=snapshot.earnings.get(ticker),
            earnings_known=earnings_source_available,
            cap_trim=trims_by_ticker.get(ticker),
            thesis_broken=break30.get(ticker, False),
            llm_sell_fraction=sells30.get(ticker, 0.0),
        )
        evaluation = evaluate_exits(trade, ctx)
        state_after = apply_exits(trade, evaluation)
        judgments.append(TradeExitJudgment(trade=trade, evaluation=evaluation, state_after=state_after))

    # --- Entradas (secciones 5.2 a 5.6) ---
    entries: list[EntrySignal] = []
    candidates: list[CandidateOutcome] = []
    deferrals: list[EntrySignal] = []
    gate_rejection = gate_entries(regime)
    allow_entries = (
        gate_rejection is None and kill is not KillState.DEFENSIVE and horizon is not HorizonState.NO_ENTRIES
    )
    if kill is KillState.DEFENSIVE:
        alerts.append(Alert(reason=Reason.KILL_SWITCH, detail="modo defensivo: sin entradas nuevas"))
    if horizon is HorizonState.NO_ENTRIES:
        alerts.append(Alert(reason=Reason.HORIZON_TIMEOUT, detail="horizonte vencido: sin entradas nuevas"))

    gate_thresholds = GateThresholds.from_config(strategy.gates)
    excluded_tickers = {t.ticker for t in judgments}
    universe_list = tuple(universe) if universe is not None else tuple(sorted(f for f in snapshot.frames))
    universe_size = len(universe_list)
    last_exit = last_exit_by_ticker or {}

    for ticker in universe_list:
        if ticker in excluded_tickers:
            continue
        if ticker not in snapshot.frames:
            candidates.append(
                CandidateOutcome(
                    ticker=ticker,
                    theme=_theme_of(ticker, themes),
                    rejections=(Rejected(reason=Reason.DATA_GAP, detail="sin serie de velas"),),
                )
            )
            continue
        frame = snapshot.frames[ticker]
        if frame.n < 2:
            candidates.append(
                CandidateOutcome(
                    ticker=ticker,
                    theme=_theme_of(ticker, themes),
                    rejections=(Rejected(reason=Reason.INDICATOR_NOT_READY, detail="serie demasiado corta"),),
                )
            )
            continue

        rejections: list[Rejected] = []
        anchor = frame.last_index - 1
        signal_features = frame.features_at(anchor)
        latest = frame.features_at(frame.last_index)
        if signal_features is None or signal_features.usable is False:
            candidates.append(
                CandidateOutcome(
                    ticker=ticker,
                    theme=_theme_of(ticker, themes),
                    rejections=(Rejected(reason=Reason.INDICATOR_NOT_READY, detail="indicadores no disponibles"),),
                )
            )
            continue
        if latest is None or not latest.usable:
            candidates.append(
                CandidateOutcome(
                    ticker=ticker,
                    theme=_theme_of(ticker, themes),
                    rejections=(
                        Rejected(reason=Reason.INDICATOR_NOT_READY, detail="cierre de la sesion sin indicadores"),
                    ),
                )
            )
            continue

        # --- Puertas (seccion 5.2) ---
        if (
            latest.close is None
            or latest.sma200 is None
            or latest.sma200_prev20 is None
            or latest.sma50 is None
            or latest.high52 is None
            or latest.low52 is None
        ):
            candidates.append(
                CandidateOutcome(
                    ticker=ticker,
                    theme=_theme_of(ticker, themes),
                    rejections=(
                        Rejected(reason=Reason.INDICATOR_NOT_READY, detail="indicadores de tendencia incompletos"),
                    ),
                )
            )
            continue
        gate_inputs = GateInputs(
            close=latest.close,
            sma200=latest.sma200,
            sma200_slope_20=latest.sma200 - latest.sma200_prev20,
            sma50=latest.sma50,
            rs_percentile=snapshot.rs.get(ticker),
            high_52w=latest.high52,
            low_52w=latest.low52,
            avg_dollar_volume=None if latest.adv_usd is None else max(latest.adv_usd, 0.0),
            days_to_earnings=_days_to_earnings(calendar_ctx, snapshot.today, snapshot.earnings.get(ticker))
            if snapshot.earnings
            else None,
            earnings_source_available=bool(snapshot.earnings),
        )
        if not earnings_source_available:
            # El backtest de la fase 3 no tiene calendario de earnings point-in-time
            # (limitacion declarada en el informe, seccion 11.3): la puerta G6 no
            # excluye ni la regla 4 dispara. En produccion ``snapshot.earnings``
            # viene lleno y esto no ocurre. Se deja ``days_to_earnings`` fuera del
            # blackout en vez de ``None``: el ``None`` es el fail-closed de D1
            # (fuente disponible, fecha desconocida).
            gate_inputs = replace(
                gate_inputs,
                earnings_source_available=True,
                days_to_earnings=gate_thresholds.earnings_entry_blackout_sessions + 1,
            )
        gate_res = check_gates(gate_inputs, gate_thresholds, universe_size)
        if not gate_res.passes:
            candidates.append(
                CandidateOutcome(ticker=ticker, theme=_theme_of(ticker, themes), rejections=gate_res.rejections)
            )
            continue
        rejections.extend(gate_res.rejections)

        # --- Setup pullback (seccion 5.3) ---
        setup = _detect_setup(signal_features, latest, strategy)
        if isinstance(setup, Rejected):
            candidates.append(CandidateOutcome(ticker=ticker, theme=_theme_of(ticker, themes), rejections=(setup,)))
            continue

        # --- Enfriamiento de re-entrada (seccion 5.7) ---
        if in_reentry_cooldown(
            last_exit.get(ticker), snapshot.today, calendar_ctx, strategy.exits.reentry_cooldown_days
        ):
            candidates.append(
                CandidateOutcome(
                    ticker=ticker,
                    theme=_theme_of(ticker, themes),
                    rejections=(Rejected(reason=Reason.REENTRY_COOLDOWN, detail="recien se cerro por meta"),),
                )
            )
            continue

        if not allow_entries:
            candidates.append(
                CandidateOutcome(
                    ticker=ticker,
                    theme=_theme_of(ticker, themes),
                    rejections=(
                        Rejected(reason=_no_entries_reason(regime, kill, horizon), detail="sin entradas esta corrida"),
                    ),
                )
            )
            continue

        # --- Stop y R (seccion 5.4) ---
        limit_price = setup.trigger_price
        if signal_features.atr is None or signal_features.swing_low10 is None or limit_price <= 0.0:
            candidates.append(
                CandidateOutcome(
                    ticker=ticker,
                    theme=_theme_of(ticker, themes),
                    rejections=(
                        Rejected(
                            reason=Reason.INDICATOR_NOT_READY, detail="ATR, swing_low10 o precio limite invalidos"
                        ),
                    ),
                )
            )
            continue
        stop_result = initial_stop(
            limit_price=limit_price,
            atr=signal_features.atr,
            swing_low=signal_features.swing_low10,
            swing_low_buffer_atr=strategy.stops.swing_low_buffer_atr,
            atr_band=strategy.stops.atr_band,
            max_stop_distance_pct=strategy.risk.max_stop_distance_pct,
        )
        if isinstance(stop_result, Rejected):
            candidates.append(
                CandidateOutcome(ticker=ticker, theme=_theme_of(ticker, themes), rejections=(stop_result,))
            )
            continue
        assert isinstance(stop_result, InitialStop)
        stop = stop_result

        # --- Costo de ida y vuelta (seccion 5.6) ---
        tp1 = next((t for t in strategy.exits.tranches if t.tp_r is not None), None)
        if (
            tp1 is not None
            and tp1.tp_r is not None
            and not tp1_covers_costs(
                stop.risk_per_share,
                limit_price,
                float(tp1.tp_r),
                strategy.costs.round_trip_fx_cost_pct,
                strategy.exits.min_tp1_cost_multiple,
            )
        ):
            candidates.append(
                CandidateOutcome(
                    ticker=ticker,
                    theme=_theme_of(ticker, themes),
                    rejections=(
                        Rejected(
                            reason=Reason.TP1_TOO_CLOSE,
                            detail=(
                                f"meta de {tp1.name} en +{tp1.tp_r:g} R no cubre "
                                f"{strategy.exits.min_tp1_cost_multiple:g} x el round-trip de divisa"
                            ),
                        ),
                    ),
                )
            )
            continue

        # --- Reglas de cartera (secciones 5.5 y 5.8) ---
        decision = evaluate_entry(state, limits, ticker, _theme_of(ticker, themes))
        if not decision.allowed:
            candidates.append(
                CandidateOutcome(ticker=ticker, theme=_theme_of(ticker, themes), rejections=decision.rejections)
            )
            continue

        risk_distance = risk_distance_with_buffer(
            stop.risk_per_share,
            signal_features.atr,
            strategy.risk.unprotected_buffer_atr,
            broker.attach_stop_to_entry is True,
        )
        caps = SizingCaps(
            max_position_clp=state.equity_clp * strategy.risk.max_position_pct / 100.0,
            theme_headroom_clp=decision.theme_headroom_clp,
            cash_clp=decision.cash_available_clp,
        )
        sized = size_trade(
            equity_clp=state.equity_clp,
            fx=snapshot.fx,
            risk_per_trade_pct=risk_pct,
            limit_price=limit_price,
            risk_distance=risk_distance,
            caps=caps,
            tranche_specs=tuple((t.name, t.fraction, t.tp_r) for t in strategy.exits.tranches),
            whole_shares=broker.uses_whole_shares,
        )
        if isinstance(sized, Rejected):
            candidates.append(CandidateOutcome(ticker=ticker, theme=_theme_of(ticker, themes), rejections=(sized,)))
            continue

        final = evaluate_entry(state, limits, ticker, _theme_of(ticker, themes), sized.notional_clp)
        if not final.allowed:
            candidates.append(
                CandidateOutcome(ticker=ticker, theme=_theme_of(ticker, themes), rejections=final.rejections)
            )
            continue

        entries.append(
            EntrySignal(
                ticker=ticker,
                theme=_theme_of(ticker, themes),
                limit_price=limit_price,
                stop_target=stop.price,
                risk_per_share=stop.risk_per_share,
                volume=sized.volume,
                tranches=sized.tranches,
                single=sized.single_tranche,
                capped_by=sized.capped_by,
                equity_pct=sized.equity_pct,
                setup=setup,
                rs_percentile=snapshot.rs.get(ticker),
                detail=_entry_detail(sized, setup, stop),
            )
        )

    # --- Tope de entradas nuevas por corrida (seccion 4.4) ---
    entries.sort(key=lambda e: (e.rs_percentile is None, -(e.rs_percentile or 0.0), e.ticker))
    max_new = strategy.operations.max_new_entries_per_run if allow_entries else 0
    accepted = tuple(entries[:max_new])
    deferrals = entries[max_new:]

    # --- Alertas de cartera ---
    alerts.extend(_portfolio_alerts(state, limits))

    return NightPlan(
        date=snapshot.today,
        regime=regime,
        regime_detail=_regime_detail(regime, risk_pct),
        horizon=horizon,
        regime_caution=regime_caution,
        equity_clp=state.equity_clp,
        cash_clp=state.cash_clp,
        fx=snapshot.fx,
        exits=tuple(judgments),
        entries=accepted,
        deferrals=tuple(deferrals),
        candidates=tuple(candidates),
        cap_trims=tuple(trims_by_ticker.values()),
        alerts=tuple(alerts),
        kill=kill,
    )


# --------------------------------------------------------------------------- #
# Soporte de decide()
# --------------------------------------------------------------------------- #


def _theme_of(ticker: str, themes: ThemesConfig) -> str:
    return themes.themes.get(ticker, DEFAULT_THEME)


def _num_or(value: float | None, fallback: float) -> float:
    return fallback if value is None else value


def _days_to_earnings(calendar: SessionCalendar, today: dt.date, earnings: dt.date | None) -> int | None:
    if earnings is None:
        return None
    return calendar.sessions_until(earnings, today)


def _detect_setup(
    signal: TickerFeatures,
    latest: TickerFeatures,
    strategy: StrategyConfig,
) -> Setup | Rejected:
    """Pullback P1 en la barra previa + confirmacion al alza en la ultima.

    El cierre de la senal (barra P1) esta en la penultima barra, la confirmacion
    en la ultima (cierre > maximo de P1, volumen >= 1,2 x promedio). El limite de
    entrada = cierre de P1 + ``entry_limit_atr`` x ATR de P1 (seccion 16.2:
    99,25 + 0,25 x 3 = 100,00).
    """
    cfg = strategy.setups.pullback
    if signal.high20 is None or signal.close is None or signal.atr is None:
        return Rejected(reason=Reason.INDICATOR_NOT_READY, detail="indicadores del setup incompletos")
    if signal.high20 <= 0.0:
        return Rejected(reason=Reason.INDICATOR_NOT_READY, detail="high20 no positivo")
    drop = (signal.high20 - signal.close) / signal.high20 * 100.0
    in_range = cfg.drop_pct[0] <= drop <= cfg.drop_pct[1]
    between = _between_ma(signal) or _near_sma20(signal)
    rsi_ok = signal.rsi is not None and cfg.rsi[0] <= signal.rsi <= cfg.rsi[1]
    if latest.close is None:
        confirm = False
    elif cfg.confirm_prior_high:
        confirm = signal.high is not None and latest.close > signal.high
    else:
        confirm = latest.close > signal.close
    if not (in_range and between and rsi_ok and confirm):
        return Rejected(reason=Reason.NO_SETUP, detail="pullback sin confirmacion en la sesion 2")

    limit = entry_limit(signal.close, signal.atr, cfg.entry_limit_atr)
    volume_ratio = (
        (latest.volume / signal.avg_vol20) if (latest.volume and signal.avg_vol20 and signal.avg_vol20 > 0.0) else 0.0
    )
    setup = Setup(
        trigger_price=limit,
        trigger_date=signal.date.isoformat(),
        bars_since_p1=1,
        volume_ratio=volume_ratio,
    )
    rejected = check_setup(setup, SETUP_MIN_VOLUME_RATIO)
    if rejected is not None:
        return rejected
    return setup


def _between_ma(signal: TickerFeatures) -> bool:
    """Cierre entre SMA20 y SMA50 (seccion 5.3): la SMA20 mira arriba en tendencia."""
    if signal.sma20 is None or signal.sma50 is None or signal.close is None:
        return False
    if signal.sma20 <= signal.sma50:
        return False
    return signal.sma50 <= signal.close <= signal.sma20


def _near_sma20(signal: TickerFeatures) -> bool:
    """O a <= 0,5 ATR de la SMA20, la alternativa que deja entrar retrocesos pequenos."""
    if signal.sma20 is None or signal.close is None or signal.atr is None:
        return False
    return abs(signal.close - signal.sma20) <= NEAR_SMA20_ATR * signal.atr


def _regime_detail(regime: RegimeState, risk_pct: float) -> str:
    if regime is RegimeState.FULL:
        return "pleno"
    if regime is RegimeState.HALF:
        return f"medio: riesgo {risk_pct:.2f} % por trade"
    return "sin entradas"


def _no_entries_reason(regime: RegimeState, kill: KillState, horizon: HorizonState) -> Reason:
    if regime is RegimeState.NO_ENTRIES:
        return Reason.NO_ENTRIES_REGIME
    if kill is KillState.DEFENSIVE:
        return Reason.KILL_SWITCH
    if horizon is HorizonState.NO_ENTRIES:
        return Reason.HORIZON_TIMEOUT
    return Reason.NO_ENTRIES_REGIME


def _entry_detail(sized: object, setup: Setup, stop: InitialStop) -> str:
    label = "SINGLE" if getattr(sized, "single_tranche", False) else f"setup {setup.trigger_date}"
    return f"{label}; R = {stop.risk_per_share:.2f} ({stop.stop_distance_pct:.2f} %)"


def _portfolio_alerts(state: PortfolioState, limits: PortfolioLimits) -> tuple[Alert, ...]:
    from swing.core.portfolio import compute_heat, is_monthly_loss_blocked, monthly_loss_pct

    out: list[Alert] = []
    heat = compute_heat(state, limits)
    if heat.heat_pct >= limits.max_heat_pct:
        out.append(
            Alert(reason=Reason.HEAT_LIMIT, detail=f"heat {heat.heat_pct:.2f} % al tope {limits.max_heat_pct:.0f} %")
        )
    if is_monthly_loss_blocked(state, limits):
        loss = monthly_loss_pct(state) or 0.0
        out.append(Alert(reason=Reason.MONTHLY_LOSS_LIMIT, detail=f"mes en {loss:.2f} %"))
    return tuple(out)


# --------------------------------------------------------------------------- #
# Estado de posicion: construccion y avance
# --------------------------------------------------------------------------- #


def trade_after_fill(
    ticker: str,
    theme: str,
    fill_price: float,
    stop_target: float,
    volume: float,
    tranches: tuple[Tranche, ...],
    single: bool,
    entry_date: dt.date,
    atr: float,
) -> TradeState:
    """Arma el ``TradeState`` de una entrada recien ejecutada (seccion 4.5).

    El dia del relleno **no existe stop**: ``broker_stop = None`` y la regla 2
    (STOP_MISSING) lo va a pedir esa corrida. ``risk_per_share`` se deriva del
    precio real de relleno, no del limite: una entrada mas barata que el limite
    tiene mas R, y eso hay que medirlo.
    """
    if fill_price <= 0.0 or stop_target >= fill_price:
        raise ValueError(f"{ticker}: relleno {fill_price} invalido contra stop {stop_target}")
    states = tuple(TrancheState(name=t.name, volume=t.volume, tp_r=t.tp_r) for t in tranches)
    return TradeState(
        ticker=ticker,
        entry_price=fill_price,
        initial_stop=stop_target,
        stop=stop_target,
        broker_stop=None,
        volume=volume,
        close=fill_price,
        days_open=0,
        atr=atr,
        highest_n=fill_price,
        max_progress_r=0.0,
        tranches=states,
        locked=False,
        single=single,
    )


def roll_trade(
    trade: TradeState,
    frame: IndicatorFrame,
    idx: int,
    *,
    lookback_days: int = 22,
) -> TradeState:
    """Avanza una posicion una sesion: cierre, ATR, Chandelier y progreso.

    El stop no se toca aqui: solo las salidas (regla 6, RAISE_STOP) lo suben.
    ``highest_n`` es el maximo de ``runner_trail.lookback_days`` ruedas, que es la
    ventana del Chandelier; el ``days_open`` cuenta de a una sesion. ``lookback_days``
    lo pasa el llamador desde ``strategy.exits.runner_trail.lookback_days``.
    """
    if not 0 <= idx < frame.n:
        return trade
    close = _num_or(_num(frame.close.iloc[idx]), trade.close)
    atr_v = _num_or(_num(frame.atr.iloc[idx]), trade.atr)
    highest_v = trade.highest_n
    if trade.days_open >= 0:
        start = max(0, idx - lookback_days + 1)
        window = frame.high.iloc[start : idx + 1]
        if len(window) > 0 and window.notna().any():
            highest_v = max(trade.highest_n, float(window.max()))
    current_r = (close - trade.entry_price) / trade.risk_per_share
    return replace(
        trade,
        close=close,
        atr=atr_v,
        highest_n=highest_v,
        days_open=trade.days_open + 1,
        max_progress_r=max(trade.max_progress_r, current_r),
    )


def position_exposures(
    open_trades: dict[str, TradeState],
    snapshot: MarketSnapshot,
    themes: ThemesConfig,
) -> tuple[PositionExposure, ...]:
    """Traduce el libro del motor a ``PositionExposure`` para las reglas de cartera.

    ``stop_clp`` va en CLP por accion (precio x FX), igual que en los tests de
    cartera: el heat se mide en pesos siempre, simule o no.
    """
    out: list[PositionExposure] = []
    for ticker in sorted(open_trades):
        trade = open_trades[ticker]
        if trade.open_volume <= 0.0:
            continue
        out.append(
            PositionExposure(
                ticker=ticker,
                theme=_theme_of(ticker, themes),
                cost_clp=trade.entry_price * trade.open_volume * snapshot.fx,
                stop_clp=trade.stop * snapshot.fx,
                volume=trade.open_volume,
                rs_percentile=snapshot.rs.get(ticker),
            )
        )
    return tuple(out)
