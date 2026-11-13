"""Adopcion de posiciones (``core/planner``): maximo real y proteccion de ganancia."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from swing.adapters.config_loader import load_strategy
from swing.core.calendar import SessionCalendar
from swing.core.engine import IndicatorFrame
from swing.core.market import Bar, PriceSeries
from swing.core.planner import adopt_positions
from swing.core.reconcile import BrokerLot, BrokerPosition

CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"
STRATEGY = load_strategy(CONFIG_DIR / "strategy.yaml")
CAL = SessionCalendar.us()
TODAY = dt.date(2026, 10, 14)


# --------------------------------------------------------------------------- #
# Adopcion (``adopt_positions``): maximo real y proteccion de ganancia
# --------------------------------------------------------------------------- #


def _ramp_series(ticker: str, *, start: float, step: float, bars: int) -> PriceSeries:
    """Serie lineal con rango intradia constante (ATR predecible, aqui 2.0)."""
    out = []
    for i in range(bars):
        close = start + i * step
        day = TODAY - dt.timedelta(days=bars - 1 - i)
        out.append(Bar(day, close, close + 1.0, close - 1.0, close, 1_000_000.0))
    return PriceSeries(ticker, tuple(out))


def _position(
    ticker: str,
    *,
    entry: float = 100.0,
    volume: float = 10.0,
    broker_stop: float | None = None,
    open_time: dt.datetime = dt.datetime(2026, 9, 20, 10, 0),
) -> BrokerPosition:
    lot = BrokerLot(id="L1", ticker=ticker, volume=volume, open_price=entry, open_time=open_time)
    return BrokerPosition(ticker=ticker, volume=volume, avg_open_price=entry, broker_stop=broker_stop, lots=(lot,))


def test_la_adopcion_usa_el_maximo_de_la_ventana_del_trail() -> None:
    # Sube de 90 a 110 con rango 2: el maximo de las ultimas 22 ruedas es 111, el
    # ultimo high. Antes se usaba max(entrada, cierre), que aqui daria 110.
    frame = IndicatorFrame.build(_ramp_series("AMD", start=90.0, step=0.5, bars=41), STRATEGY)
    assert frame is not None
    trade = adopt_positions((_position("AMD"),), {"AMD": frame}, STRATEGY, CAL, TODAY)["AMD"]
    idx = frame.last_index
    lookback = STRATEGY.exits.runner_trail.lookback_days
    expected_high = float(frame.high.iloc[max(0, idx - lookback + 1) : idx + 1].max())
    assert trade.highest_n == pytest.approx(expected_high)


def test_la_adopcion_protege_la_ganancia_con_el_trail() -> None:
    # Entrada 100, cierre 110, maximo 111, ATR 2 -> Chandelier 111 - 3x2 = 105:
    # sobre el costo y bajo el precio, asi que el stop objetivo lo adopta.
    frame = IndicatorFrame.build(_ramp_series("AMD", start=90.0, step=0.5, bars=41), STRATEGY)
    assert frame is not None
    atr = frame.features_at(frame.last_index).atr
    trade = adopt_positions((_position("AMD"),), {"AMD": frame}, STRATEGY, CAL, TODAY)["AMD"]
    assert trade.initial_stop == pytest.approx(100.0 * (1.0 - STRATEGY.legacy.max_loss_from_cost_pct / 100.0))
    assert trade.entry_price < trade.stop < trade.close
    assert trade.stop == pytest.approx(trade.highest_n - STRATEGY.exits.runner_trail.atr_mult * atr)


def test_una_posicion_en_perdida_no_adopta_un_stop_de_ganancia() -> None:
    # Cae de 130 a 100: el Chandelier queda sobre el precio (no es colocable), asi
    # que el stop sigue siendo el piso de perdida del motor (-18 %).
    frame = IndicatorFrame.build(_ramp_series("AMD", start=130.0, step=-0.75, bars=41), STRATEGY)
    assert frame is not None
    trade = adopt_positions((_position("AMD", entry=100.0),), {"AMD": frame}, STRATEGY, CAL, TODAY)["AMD"]
    assert trade.stop == pytest.approx(100.0 * (1.0 - STRATEGY.legacy.max_loss_from_cost_pct / 100.0))


def test_la_adopcion_toma_el_stop_de_ganancia_ya_colocado_en_el_broker() -> None:
    # Un Sell Stop del broker por encima del costo no se descarta: pasa a ser el
    # stop vigente, y la R sigue saliendo del piso (nunca de un stop sobre la entrada).
    frame = IndicatorFrame.build(_ramp_series("AMD", start=90.0, step=0.5, bars=41), STRATEGY)
    assert frame is not None
    trade = adopt_positions((_position("AMD", broker_stop=108.0),), {"AMD": frame}, STRATEGY, CAL, TODAY)["AMD"]
    assert trade.stop == pytest.approx(108.0)
    assert trade.initial_stop < trade.entry_price
    assert trade.broker_stop == pytest.approx(108.0)


def test_el_stop_duro_v11_manda_sobre_el_stop_del_broker_y_el_piso_v10() -> None:
    # Cae de 130 a 100: sin override el stop seria el piso v10 (-18 %). Con el stop v11
    # del ledger (bajo la entrada) ese nivel pasa a ser stop y R.
    frame = IndicatorFrame.build(_ramp_series("AMD", start=130.0, step=-0.75, bars=41), STRATEGY)
    assert frame is not None
    trade = adopt_positions(
        (_position("AMD", entry=100.0, broker_stop=95.0),),
        {"AMD": frame},
        STRATEGY,
        CAL,
        TODAY,
        stop_overrides={"AMD": 70.0},
    )["AMD"]
    assert trade.stop == pytest.approx(70.0)
    assert trade.initial_stop == pytest.approx(70.0)


def test_un_stop_v11_sobre_la_entrada_no_rompe_la_r() -> None:
    # Tras el break-even el stop v11 queda en/sobre la entrada: la R sale del piso.
    frame = IndicatorFrame.build(_ramp_series("AMD", start=90.0, step=0.5, bars=41), STRATEGY)
    assert frame is not None
    trade = adopt_positions(
        (_position("AMD", entry=100.0),), {"AMD": frame}, STRATEGY, CAL, TODAY, stop_overrides={"AMD": 100.0}
    )["AMD"]
    assert trade.stop == pytest.approx(100.0)
    assert trade.initial_stop < trade.entry_price
