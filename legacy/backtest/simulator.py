"""Simulador walk-forward del backtest (spec v3.1 seccion 11.2).

Lleva el estado rueda a rueda con **exactamente** las mismas funciones que la
produccion: ``decide`` decide la corrida, ``apply_exits`` y ``trade_after_fill``
producen el estado de la manana, y las funciones de relleno de ``core.orders``
(seccion 11.2) convierten los tickets en ejecuciones. La equivalencia
simulacion/planificacion se sostiene en eso: si el backtest y el planner
divergen, no es por la semantica, es por la implementacion de uno de los dos.

El dinero corre en **moneda base y con ``fx = 1``**: el backtest no modela el
tipo de cambio real (limitacion declarada en el informe). El equity se mide
sobre la suma de caja y el valor de las posiciones abiertas al cierre de cada
sesion; el drawdown del kill switch se mide sobre esa curva. Las series de
``series`` y ``spy`` deben arrancar bien antes de ``start``: el simulador
necesita ~220 barras de historia de SPY para que el regimen tenga cola.

Dos supuestos declarados en el informe:

- Los ``CAP_TRIM`` (venta por concentracion, seccion 5.8) se **ejecutan** en la
  apertura siguiente sin confirmacion humana. En produccion requieren
  confirmacion; en el backtest no hay nadie que confirme y no modelar la venta
  produciria carteras clavadas sobre el tope de tema para siempre.
- El kill switch (seccion 13) se aplica en vivo dentro de la corrida: con el
  drawdown o la expectativa de remuestreo en ``DEFENSIVE`` no se proponen
  entradas nuevas, igual que en produccion. Es la unica forma de que la
  salvaguarda no sea un chequeo de fin de corrida sino parte de la operativa
  evaluada.
"""

from __future__ import annotations

import datetime as dt
import math
import statistics
from dataclasses import dataclass, replace

from swing.core.calendar import SessionCalendar
from swing.core.engine import (
    IndicatorFrame,
    MarketSnapshot,
    TradeExitJudgment,
    decide,
    position_exposures,
    relative_strength_percentiles,
    roll_trade,
    trade_after_fill,
)
from swing.core.market import PriceSeries
from swing.core.metrics import KillState, TradeRecord, kill_switch_state
from swing.core.models import BrokerConfig, StrategyConfig, ThemesConfig
from swing.core.orders import (
    buy_limit_fill,
    sell_limit_fill,
    sell_market_fill,
    sell_stop_fill,
)
from swing.core.portfolio import PortfolioState
from swing.core.reasons import Action, ExitCause, Reason
from swing.core.sizing import Tranche
from swing.core.state import TradeState

#: Historia minima de SPY (barras) para que una sesion sea decidible: la sma200
#: mas el slope de 20 barras necesita 201, y dejamos margen.
WARMUP_BARS = 220

#: Causas que cuentan como "cierre por meta" para el enfriamiento de re-entrada
#: (seccion 5.7): el runner que cierra tras vender el tramo A tambien es una
#: salida por meta (el trade completo quedo en positivo).
META_CLOSURE_CAUSES = frozenset({ExitCause.TP1, ExitCause.RUNNER_TRAIL})

#: Un slope negativo/NaN de las series cortadas se trata como 0 para no romper
#: el armado del trade; el tope real lo pone ``GatesConfig``.
_NAN_ZERO = 0.0


@dataclass(frozen=True, slots=True)
class SimOptions:
    """Opciones de una corrida. Lo que no esta aqui es estrategia (strategy.yaml)."""

    initial_equity_clp: float
    fx: float = 1.0
    apply_kill_switch: bool = True
    cap_trims_auto: bool = True


@dataclass(frozen=True, slots=True)
class EquityPoint:
    """Valor de la cartera al cierre de una sesion."""

    date: dt.date
    equity_clp: float


@dataclass(frozen=True, slots=True)
class NightStub:
    """Una corrida decidida, para los KPIs de operativa (seccion 11.3)."""

    date: dt.date
    regime: str
    kill: str
    entries: int
    deferrals: int
    sells: int
    stop_edits: int
    has_actions: bool


@dataclass(frozen=True, slots=True)
class BacktestRun:
    """Resultado de una corrida: equity, corridas de operacion y trades cerrados.

    ``trades`` solo trae trades **cerrados dentro de la corrida**. Las
    posiciones que quedan abiertas al final se cuentan en ``open_at_end`` y la
    validacion las excluye del calculo: un trade sin desenlace no es una
    observacion de la esperanza de la estrategia.
    """

    start: dt.date
    end: dt.date
    equity: tuple[EquityPoint, ...]
    nights: tuple[NightStub, ...]
    trades: tuple[TradeRecord, ...]
    open_at_end: int
    final_equity_clp: float


@dataclass(frozen=True, slots=True)
class _PendBuy:
    """Un Buy Limit dejado de corrida para la apertura de ``target``."""

    target: dt.date
    ticker: str
    theme: str
    limit: float
    stop_target: float
    volume: float
    tranches: tuple[Tranche, ...]
    single: bool


@dataclass(frozen=True, slots=True)
class _PendSell:
    """Una venta decidida de corrida para la apertura de ``target``.

    ``market`` distingue la cola a mercado del Sell Limit de respaldo
    (seccion 4.3). ``state_after`` es el estado que el motor calculo para
    despues de ejecutar; se aplica unicamente si el relleno ocurre.
    """

    target: dt.date
    ticker: str
    volume: float
    market: bool
    limit: float | None
    state_after: TradeState
    new_stop: float | None
    reason: Reason
    exit_cause: ExitCause | None


@dataclass(frozen=True, slots=True)
class _PendStop:
    """Una edicion del Sell Stop (RAISE_STOP, STOP_MISSING, STOP_MISMATCH)."""

    target: dt.date
    ticker: str
    state_after: TradeState
    stop: float


@dataclass(slots=True)
class _LiveTrade:
    """Una posicion abierta con la contabilidad de lo ya vendido por tramos."""

    state: TradeState
    theme: str
    entry_date: dt.date
    sold_volume: float = 0.0
    realized: float = 0.0
    """Cobrado bruto por las ventas parciales; el precio de salida ponderado
    del trade completo se deriva de (``realized`` + relleno final)."""


class _Simulator:
    """Estado interno de una corrida. No se reutiliza entre corridas."""

    def __init__(
        self,
        strategy: StrategyConfig,
        broker: BrokerConfig,
        themes: ThemesConfig,
        calendar: SessionCalendar,
        series: dict[str, PriceSeries],
        spy: PriceSeries,
        vix: PriceSeries,
        universe: tuple[str, ...],
        start: dt.date,
        end: dt.date,
        options: SimOptions,
    ) -> None:
        self.strategy = strategy
        self.broker = broker
        self.themes = themes
        self.calendar = calendar
        self.series = series
        self.spy = spy
        self.vix = vix
        self.universe = universe
        self.start = start
        self.end = end
        self.options = options

        self.frames: dict[str, IndicatorFrame] = {}
        self.idx_by_ticker: dict[str, dict[dt.date, int]] = {}
        self.spy_frame: IndicatorFrame | None = None
        self.spy_close: dict[dt.date, float] = {}
        self.spy_sma200: dict[dt.date, float] = {}
        self.vix_close: dict[dt.date, float] = {}
        self._precompute()

        self.cash: float = options.initial_equity_clp
        self.book: dict[str, _LiveTrade] = {}
        self.pend_buys: list[_PendBuy] = []
        self.pend_sells: list[_PendSell] = []
        self.pend_stops: list[_PendStop] = []
        self.closed: list[TradeRecord] = []
        self.last_exit_by_ticker: dict[str, dt.date] = {}
        self.nights: list[NightStub] = []
        self.equity: list[EquityPoint] = []
        self.month_start_equity: float = options.initial_equity_clp
        self.prev_month: tuple[int, int] | None = None
        self.peak_equity: float = options.initial_equity_clp

    # ------------------------------------------------------------------ #
    # Preparacion
    # ------------------------------------------------------------------ #

    def _precompute(self) -> None:
        for ticker, series in self.series.items():
            if series.is_empty:
                continue
            frame = IndicatorFrame.build(series, self.strategy)
            if frame is None:
                continue
            self.frames[ticker] = frame
            self.idx_by_ticker[ticker] = {bar.date: i for i, bar in enumerate(series.bars)}
        if not self.spy.is_empty:
            spy_frame = IndicatorFrame.build(self.spy, self.strategy)
            if spy_frame is not None:
                self.spy_frame = spy_frame
                self.spy_close = {bar.date: bar.close for bar in self.spy.bars}
                for i, bar in enumerate(self.spy.bars):
                    value = spy_frame.sma200.iloc[i]
                    if isinstance(value, float) and not (value != value):
                        self.spy_sma200[bar.date] = value
        self.vix_close = {bar.date: bar.close for bar in self.vix.bars}

    def _bar(self, ticker: str, day: dt.date) -> tuple[float, float, float, float] | None:
        """OHLC del ticker en ``day`` en la moneda de la simulacion (x fx)."""
        mapping = self.idx_by_ticker.get(ticker)
        if mapping is None or day not in mapping:
            return None
        open_v, high, low, close = 0.0, 0.0, 0.0, 0.0
        for bar in self.series[ticker].bars:
            if bar.date == day:
                open_v, high, low, close = bar.open, bar.high, bar.low, bar.close
                break
        fx = self.options.fx
        return open_v * fx, high * fx, low * fx, close * fx

    def _frame_upto(self, ticker: str, day: dt.date) -> IndicatorFrame | None:
        """Frame cortado hasta ``day``.

        Los indicadores son causales: los valores en la barra j no dependen de
        las barras posteriores, asi que una seri recortada tiene los mismos
        numeros en cada indice que la serie completa. Es el truco que permite
        reutilizar el frame precalculado en vez de re-calcular indicadores por
        dia (el costo de una corrida seria otra orden de magnitud).
        """
        mapping = self.idx_by_ticker.get(ticker)
        frame = self.frames.get(ticker)
        if mapping is None or frame is None or day not in mapping:
            return None
        j = mapping[day]
        if j == frame.last_index:
            return frame
        return IndicatorFrame(
            ticker=frame.ticker,
            close=frame.close.iloc[: j + 1],
            high=frame.high.iloc[: j + 1],
            low=frame.low.iloc[: j + 1],
            volume=frame.volume.iloc[: j + 1],
            sma200=frame.sma200.iloc[: j + 1],
            sma50=frame.sma50.iloc[: j + 1],
            sma20=frame.sma20.iloc[: j + 1],
            atr=frame.atr.iloc[: j + 1],
            rsi=frame.rsi.iloc[: j + 1],
            swing_low10=frame.swing_low10.iloc[: j + 1],
            high20=frame.high20.iloc[: j + 1],
            low52=frame.low52.iloc[: j + 1],
            high52=frame.high52.iloc[: j + 1],
            adv_usd=frame.adv_usd.iloc[: j + 1],
            avg_vol20=frame.avg_vol20.iloc[: j + 1],
        )

    # ------------------------------------------------------------------ #
    # Ciclo diario
    # ------------------------------------------------------------------ #

    def run(self) -> BacktestRun:
        for day in self.calendar.sessions_in_range(self.start, self.end):
            self._process_session(day)
        return BacktestRun(
            start=self.start,
            end=self.end,
            equity=tuple(self.equity),
            nights=tuple(self.nights),
            trades=tuple(self.closed),
            open_at_end=sum(1 for live in self.book.values() if live.state.open_volume > 0.0),
            final_equity_clp=self.equity[-1].equity_clp if self.equity else self.options.initial_equity_clp,
        )

    def _ready(self, day: dt.date) -> bool:
        """True cuando hay barras y sma200 de SPY para decidir la corrida de ``day``."""
        if self.spy_frame is None or day not in self.spy_close:
            return False
        if day not in self.spy_sma200:
            return False
        idx = next((i for i, bar in enumerate(self.spy.bars) if bar.date == day), None)
        if idx is None or idx < WARMUP_BARS:
            return False
        return True

    def _process_session(self, day: dt.date) -> None:
        if not self._ready(day):
            return

        # --- Apertura: ordenes colocadas la corrida anterior ---
        self._process_open(day)

        # --- Intradia: Sell Stop del broker ---
        for ticker in sorted(self.book):
            self._process_broker_stop(ticker, day)

        # --- Cierre: avanzar cada posicion una sesion ---
        for ticker in sorted(self.book):
            live = self.book[ticker]
            j = self.idx_by_ticker[ticker].get(day)
            if j is None:
                continue
            live.state = roll_trade(
                live.state,
                self.frames[ticker],
                j,
                lookback_days=self.strategy.exits.runner_trail.lookback_days,
            )

        self._mark_to_market(day)
        self._update_peak()
        self._track_month(day)
        self._night(day)

    # ------------------------------------------------------------------ #
    # Apertura e intradia
    # ------------------------------------------------------------------ #

    def _process_open(self, day: dt.date) -> None:
        # Ventas antes que compras: vender libera caja para el mismo dia.
        sells = [p for p in self.pend_sells if p.target == day]
        self.pend_sells = [p for p in self.pend_sells if p.target != day]
        for pend in sells:
            live = self.book.get(pend.ticker)
            if live is None or live.state.open_volume <= 0.0:
                continue
            bar = self._bar(pend.ticker, day)
            if bar is None:
                continue  # sin vela no hay relleno; se reevalua de corrida (seccion 4.3)
            day_open, day_high, _low, _close = bar
            fill: float | None
            if pend.market:
                fill = sell_market_fill(day_open)
            elif pend.limit is not None:
                fill = sell_limit_fill(day_open, day_high, pend.limit)
            else:  # pragma: no cover - no deberia llegar un pend sin precio ni mercado
                fill = None
            if fill is None:
                continue
            self._sell_executed(pend, fill, day)

        stops = [p for p in self.pend_stops if p.target == day]
        self.pend_stops = [p for p in self.pend_stops if p.target != day]
        for stop_pend in stops:
            live = self.book.get(stop_pend.ticker)
            if live is None or live.state.open_volume <= 0.0:
                continue
            previous = live.state.broker_stop
            target = max(stop_pend.stop, stop_pend.state_after.stop)
            if previous is not None:
                target = max(target, previous)  # un stop nunca baja
            live.state = replace(stop_pend.state_after, broker_stop=target)

        buys = [p for p in self.pend_buys if p.target == day]
        self.pend_buys = [p for p in self.pend_buys if p.target != day]
        for buy_pend in buys:
            if buy_pend.ticker in self.book:
                continue
            bar = self._bar(buy_pend.ticker, day)
            if bar is None:
                continue
            day_open, day_high, day_low, _close = bar
            fill = buy_limit_fill(day_open, day_low, day_high, buy_pend.limit)
            if fill is None:
                continue
            j = self.idx_by_ticker[buy_pend.ticker][day]
            atr_value = self.frames[buy_pend.ticker].atr.iloc[j]
            atr_v = float(atr_value) if isinstance(atr_value, (int, float)) else 0.0
            state = trade_after_fill(
                ticker=buy_pend.ticker,
                theme=buy_pend.theme,
                fill_price=fill,
                stop_target=buy_pend.stop_target,
                volume=buy_pend.volume,
                tranches=buy_pend.tranches,
                single=buy_pend.single,
                entry_date=day,
                atr=atr_v,
            )
            if self.broker.attach_stop_to_entry is True:
                state = replace(state, broker_stop=state.stop)
            self.cash -= fill * buy_pend.volume
            self.book[buy_pend.ticker] = _LiveTrade(state=state, theme=buy_pend.theme, entry_date=day)

    def _process_broker_stop(self, ticker: str, day: dt.date) -> None:
        """Un Sell Stop vigente del broker se ejecuta intradia si el minimo lo toca."""
        live = self.book.get(ticker)
        if live is None or live.state.open_volume <= 0.0:
            return
        stop = live.state.broker_stop
        if stop is None:
            return
        bar = self._bar(ticker, day)
        if bar is None:
            return
        day_open, _high, day_low, _close = bar
        fill = sell_stop_fill(
            day_open,
            day_low,
            stop,
            self.strategy.costs.stop_slippage_pct,
        )
        if fill is None:
            return
        volume = live.state.open_volume
        self.cash += fill * volume
        live.sold_volume += volume
        live.realized += fill * volume
        self._close(ticker, ExitCause.STOP, Reason.STOP_BREACH, day)

    def _sell_executed(self, pend: _PendSell, fill: float, day: dt.date) -> None:
        live = self.book.get(pend.ticker)
        if live is None:
            return
        volume = min(pend.volume, live.state.open_volume)
        if volume <= 0.0:
            return
        self.cash += fill * volume
        live.sold_volume += volume
        live.realized += fill * volume
        if live.state.open_volume - volume > 0.0:
            previous = live.state.broker_stop
            target = pend.new_stop if pend.new_stop is not None else pend.state_after.stop
            if previous is not None:
                target = max(target, previous)
            live.state = replace(pend.state_after, broker_stop=target)
            return
        self._close(pend.ticker, pend.exit_cause, pend.reason, day)

    def _close(self, ticker: str, cause: ExitCause | None, reason: Reason, day: dt.date) -> None:
        """Cierra el trade con el precio de salida ponderado de todas sus piernas."""
        live = self.book.pop(ticker, None)
        if live is None:
            return
        risk = live.state.risk_per_share
        entry = live.state.entry_price
        if live.sold_volume > 0.0:
            exit_price = live.realized / live.sold_volume
        else:
            exit_price = 0.0
        cost_pct = self.strategy.costs.round_trip_fx_cost_pct + 2.0 * self.strategy.costs.commission_pct
        cost_per_share = entry * cost_pct / 100.0
        gross_r = (exit_price - entry) / risk
        self.closed.append(
            TradeRecord(
                ticker=ticker,
                theme=live.theme,
                entry_date=live.entry_date,
                exit_date=day,
                days=self.calendar.sessions_between(live.entry_date, day),
                entry_price=entry,
                exit_price=exit_price,
                volume=live.sold_volume,
                gross_r=gross_r,
                r=gross_r - cost_per_share / risk,
                cause=cause.value if cause else reason.value,
                earnings_derisk=cause is ExitCause.EARNINGS,
                single=live.state.single,
            )
        )
        if cause in META_CLOSURE_CAUSES:
            self.last_exit_by_ticker[ticker] = day

    # ------------------------------------------------------------------ #
    # Equity, mensual y salvaguarda
    # ------------------------------------------------------------------ #

    def _mark_to_market(self, day: dt.date) -> None:
        equity = self.cash + sum(
            live.state.close * live.state.open_volume for live in self.book.values()
        )
        self.equity.append(EquityPoint(date=day, equity_clp=equity))

    def _update_peak(self) -> None:
        if self.equity:
            self.peak_equity = max(self.peak_equity, self.equity[-1].equity_clp)

    def _track_month(self, day: dt.date) -> None:
        """Foto de inicio de mes = equity al cierre de la ultima sesion anterior."""
        month = (day.year, day.month)
        if self.prev_month is None or month != self.prev_month:
            if len(self.equity) >= 2:
                self.month_start_equity = self.equity[-2].equity_clp
            self.prev_month = month

    def _kill_state(self) -> KillState:
        if not self.options.apply_kill_switch:
            return KillState.OK
        safety = self.strategy.safety
        closed_r = [r.r for r in self.closed if not r.single]
        n = len(closed_r)
        expectancy = statistics.fmean(closed_r) if closed_r else 0.0
        se = statistics.stdev(closed_r) / math.sqrt(n) if n >= 2 else float("inf")
        drawdown = (self.peak_equity - self.equity[-1].equity_clp) / self.peak_equity * 100.0 if self.peak_equity > 0.0 else 0.0
        return kill_switch_state(n, expectancy, se, drawdown, safety)

    # ------------------------------------------------------------------ #
    # La corrida: decidir
    # ------------------------------------------------------------------ #

    def _night(self, day: dt.date) -> None:
        frames: dict[str, IndicatorFrame] = {}
        for ticker in self.universe:
            frame = self._frame_upto(ticker, day)
            if frame is not None:
                frames[ticker] = frame
        if not frames:
            return

        open_states = {ticker: live.state for ticker, live in self.book.items()}
        equity = self.equity[-1].equity_clp
        snapshot = MarketSnapshot(
            today=day,
            series=self.series,
            frames=frames,
            equity_clp=equity,
            cash_clp=self.cash,
            fx=self.options.fx,
            spy_close=self.spy_close.get(day),
            spy_sma200=self.spy_sma200.get(day),
            vix=self.vix_close.get(day),
            earnings={},
            rs=relative_strength_percentiles(frames, self.strategy.gates.rs_lookback_days),
        )
        state = PortfolioState(
            equity_clp=equity,
            cash_clp=self.cash,
            positions=position_exposures(open_states, snapshot, self.themes),
            equity_at_month_start_clp=self.month_start_equity,
        )
        plan = decide(
            snapshot,
            state,
            open_states,
            self.strategy,
            self.broker,
            self.calendar,
            self.themes,
            universe=self.universe,
            last_exit_by_ticker=self.last_exit_by_ticker,
            kill=self._kill_state(),
        )

        self.nights.append(
            NightStub(
                date=day,
                regime=plan.regime.value,
                kill=plan.kill.value,
                entries=len(plan.entries),
                deferrals=len(plan.deferrals),
                sells=sum(
                    1
                    for j in plan.exits
                    if j.has_action and j.evaluation.primary is not None and j.evaluation.primary.action is Action.SELL
                ),
                stop_edits=sum(
                    1
                    for j in plan.exits
                    if j.has_action
                    and j.evaluation.primary is not None
                    and j.evaluation.primary.action is Action.EDIT_STOP
                ),
                has_actions=plan.has_actions,
            )
        )

        next_day = self.calendar.next_session(day)
        for entry in plan.entries:
            self.pend_buys.append(
                _PendBuy(
                    target=next_day,
                    ticker=entry.ticker,
                    theme=entry.theme,
                    limit=entry.limit_price,
                    stop_target=entry.stop_target,
                    volume=entry.volume,
                    tranches=entry.tranches,
                    single=entry.single,
                )
            )
        for judgment in plan.exits:
            primary = judgment.evaluation.primary
            if primary is None or not primary.execute:
                continue
            if primary.action is Action.SELL:
                if primary.cause is Reason.CAP_TRIM and not self.options.cap_trims_auto:
                    continue
                market = self.broker.queued_market_orders is True
                self.pend_sells.append(
                    _PendSell(
                        target=next_day,
                        ticker=judgment.ticker,
                        volume=primary.volume,
                        market=market,
                        limit=None if market else self._exit_limit(judgment),
                        state_after=judgment.state_after,
                        new_stop=primary.new_stop,
                        reason=primary.cause,
                        exit_cause=primary.exit_cause,
                    )
                )
            elif primary.action is Action.EDIT_STOP and primary.new_stop is not None:
                self.pend_stops.append(
                    _PendStop(
                        target=next_day,
                        ticker=judgment.ticker,
                        state_after=judgment.state_after,
                        stop=primary.new_stop,
                    )
                )

    def _exit_limit(self, judgment: TradeExitJudgment) -> float:
        """Sell Limit de respaldo (seccion 4.3): ``cierre - exit_limit_atr x ATR``."""
        trade = judgment.state_after
        atr = trade.atr if trade.atr else 0.0
        return max(trade.close - self.strategy.exits.exit_limit_atr * atr, 0.0)


def run_backtest(
    strategy: StrategyConfig,
    broker: BrokerConfig,
    themes: ThemesConfig,
    calendar: SessionCalendar,
    series: dict[str, PriceSeries],
    spy: PriceSeries,
    vix: PriceSeries,
    universe: tuple[str, ...],
    start: dt.date,
    end: dt.date,
    options: SimOptions | None = None,
) -> BacktestRun:
    """Corre el walk-forward del rango ``[start, end]`` y devuelve el resumen."""
    sim = _Simulator(
        strategy=strategy,
        broker=broker,
        themes=themes,
        calendar=calendar,
        series=series,
        spy=spy,
        vix=vix,
        universe=universe,
        start=start,
        end=end,
        options=options or SimOptions(initial_equity_clp=1_000_000.0),
    )
    return sim.run()
