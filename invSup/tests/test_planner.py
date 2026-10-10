"""Planner de tickets (``core/planner``): precio de la venta y tope de acciones.

Lo que se fija: la venta sale a mercado cuando ``queued_market_orders`` esta
confirmado, o como Sell Limit de respaldo marcado ``RIESGO_SIN_COBERTURA`` si no;
y el tope ``max_actions_per_evening`` nunca difiere una accion de seguridad.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from swing.adapters.config_loader import load_broker, load_strategy
from swing.core.engine import ExitEvaluation, NightPlan, TradeExitJudgment
from swing.core.exits import ExitSignal
from swing.core.models import HorizonState, RegimeState
from swing.core.orders import OrderKind
from swing.core.planner import plan_tickets, render_tickets
from swing.core.reasons import Action, Reason
from swing.core.state import TradeState, TrancheState

CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"
STRATEGY = load_strategy(CONFIG_DIR / "strategy.yaml")
BROKER = load_broker(CONFIG_DIR / "broker.yaml")


def _trade(ticker: str, *, close: float = 110.0, atr: float = 4.0) -> TradeState:
    return TradeState(
        ticker=ticker,
        entry_price=100.0,
        initial_stop=90.0,
        stop=90.0,
        broker_stop=90.0,
        volume=10.0,
        close=close,
        days_open=10,
        atr=atr,
        highest_n=max(100.0, close),
        max_progress_r=1.0,
        tranches=(TrancheState(name="R", volume=10.0, tp_r=None),),
    )


def _judgment(ticker: str, signal: ExitSignal) -> TradeExitJudgment:
    trade = _trade(ticker)
    return TradeExitJudgment(
        trade=trade,
        evaluation=ExitEvaluation(signals=(signal,), primary_cause=signal.cause, should_exit=True),
        state_after=trade,
    )


def _plan(exits: tuple[TradeExitJudgment, ...]) -> NightPlan:
    return NightPlan(
        date=dt.date(2026, 10, 7),
        regime=RegimeState.FULL,
        regime_detail="",
        horizon=HorizonState.NORMAL,
        regime_caution=False,
        equity_clp=1_000_000.0,
        cash_clp=0.0,
        fx=900.0,
        exits=exits,
        entries=(),
        deferrals=(),
        candidates=(),
        cap_trims=(),
        alerts=(),
    )


def _sell(cause: Reason, volume: float = 5.0) -> ExitSignal:
    return ExitSignal(cause=cause, action=Action.SELL, execute=True, volume=volume)


def test_venta_a_mercado_cuando_queued_market_orders_esta_confirmado() -> None:
    plan = _plan((_judgment("AAA", _sell(Reason.MAX_HOLD)),))
    ticket = plan_tickets(plan, BROKER, STRATEGY).tickets[0]
    assert ticket.kind is OrderKind.SELL
    assert ticket.price is None
    assert ticket.flag is None


def test_venta_es_sell_limit_de_respaldo_y_avisa_sin_cobertura() -> None:
    broker = BROKER.model_copy(update={"queued_market_orders": False})
    plan = _plan((_judgment("AAA", _sell(Reason.MAX_HOLD)),))
    ticket = plan_tickets(plan, broker, STRATEGY).tickets[0]
    # cierre 110 - 0,5 x ATR 4 = 108
    assert ticket.price == 108.0
    assert ticket.flag == "RIESGO_SIN_COBERTURA"


def test_el_tope_de_acciones_difiere_lo_que_sobra() -> None:
    limit = STRATEGY.operations.max_actions_per_evening
    exits = tuple(_judgment(f"T{i:02d}", _sell(Reason.MAX_HOLD)) for i in range(limit + 2))
    result = plan_tickets(_plan(exits), BROKER, STRATEGY)
    assert len(result.tickets) == limit
    assert len(result.deferred) == 2
    assert all(ticket.recurring_count >= 1 for ticket in result.deferred)
    assert "RECURRING" in "\n".join(render_tickets(result))


def test_las_acciones_de_seguridad_nunca_se_difieren() -> None:
    safety = tuple(
        _judgment(f"S{i:02d}", _sell(Reason.STOP_BREACH))
        for i in range(STRATEGY.operations.max_actions_per_evening + 1)
    )
    extra = (_judgment("ZZZ", _sell(Reason.MAX_HOLD)),)
    result = plan_tickets(_plan(safety + extra), BROKER, STRATEGY)
    tickers = {ticket.ticker for ticket in result.tickets}
    assert all(f"S{i:02d}" in tickers for i in range(STRATEGY.operations.max_actions_per_evening + 1))
    assert [ticket.ticker for ticket in result.deferred] == ["ZZZ"]


def test_stop_ausente_es_seguridad_y_no_se_difiere() -> None:
    signal = ExitSignal(cause=Reason.STOP_MISSING, action=Action.EDIT_STOP, execute=True, volume=10.0, new_stop=90.0)
    exits = tuple(
        _judgment(f"T{i:02d}", _sell(Reason.MAX_HOLD)) for i in range(STRATEGY.operations.max_actions_per_evening)
    )
    result = plan_tickets(_plan((*exits, _judgment("AAA", signal))), BROKER, STRATEGY)
    assert "AAA" in {ticket.ticker for ticket in result.tickets}
