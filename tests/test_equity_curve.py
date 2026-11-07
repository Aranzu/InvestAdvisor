"""Curva de patrimonio ajustada por aportes (``core.equity_curve``)."""

from __future__ import annotations

import datetime as dt

import pytest

from swing.core.equity_curve import build_curve
from swing.core.market import Bar, PriceSeries
from swing.core.models import ComplianceConfig
from swing.core.report import CashOp, OpenLot


def _cfg() -> ComplianceConfig:
    return ComplianceConfig(
        risk_per_open_pct=0.75,
        max_weight_open_pct=8.0,
        high_atr_pct=7.0,
        max_weight_open_high_atr_pct=3.0,
        max_weight_hold_pct=12.0,
        max_weight_hold_trim_pct=10.0,
        max_risk_position_pct=1.5,
        max_open_risk_pct=10.0,
        max_factor_pct=40.0,
        max_theme_pct=25.0,
        min_cash_pct=10.0,
        cash_band_max_pct=20.0,
        stop_atr_mult=2.5,
        stop_min_pct=8.0,
        stop_max_pct=18.0,
        inherited_stop_max_distance_pct=15.0,
        transition_stop_max_pct=10.0,
        breakeven_r=1.5,
        enhancer_r=3.0,
        trailing_sessions=20,
        trailing_atr_mult=3.0,
        caution_drawdown_pct=8.0,
        defensive_drawdown_pct=15.0,
        purchases_until=dt.date(2027, 1, 15),
        liquidate_until=dt.date(2027, 3, 1),
        take_profit_max_upside_pct=30.0,
        take_profit_min_analysts=10,
        correlation_sessions=60,
    )


def _op(day: dt.date, kind: str, amount: float) -> CashOp:
    return CashOp(kind=kind, when=dt.datetime.combine(day, dt.time()), amount_clp=amount, ticker="", position_id="", comment="")


def _lot(day: dt.date, volume: float = 10.0) -> OpenLot:
    return OpenLot(
        ticker="AAA",
        name="AAA",
        position_id="1",
        volume=volume,
        value_clp=volume * 10.0,
        price_usd=10.0,
        open_price_usd=10.0,
        open_date=day,
        stop_usd=None,
        profit_clp=0.0,
    )


def _series(prices: list[float], start: dt.date) -> PriceSeries:
    return PriceSeries(
        "AAA",
        tuple(
            Bar(date=start + dt.timedelta(days=index), open=price, high=price, low=price, close=price, volume=1.0)
            for index, price in enumerate(prices)
        ),
    )


def test_aporte_no_se_confunde_con_rentabilidad() -> None:
    day1 = dt.date(2026, 6, 24)
    day2 = dt.date(2026, 6, 25)
    today = dt.date(2026, 6, 26)
    curve = build_curve(
        cash_ops=(_op(day1, "Deposit", 1000.0), _op(day1, "Stock purchase", -100.0), _op(day2, "Deposit", 1000.0)),
        closed=(),
        lots=(_lot(day1),),
        prices={"AAA": _series([10.0, 10.0, 10.0], day1)},
        fx_by_date={},
        fallback_fx=1.0,
        today=today,
        cfg=_cfg(),
    )
    assert curve is not None
    assert curve.drawdown_pct == pytest.approx(0.0, abs=0.01)
    assert curve.current_unit_value == pytest.approx(100.0)


def test_caida_real_baja_el_valor_cuota() -> None:
    day1 = dt.date(2026, 6, 24)
    today = dt.date(2026, 6, 26)
    curve = build_curve(
        cash_ops=(_op(day1, "Deposit", 1000.0), _op(day1, "Stock purchase", -100.0)),
        closed=(),
        lots=(_lot(day1),),
        prices={"AAA": _series([10.0, 10.0, 8.0], day1)},
        fx_by_date={},
        fallback_fx=1.0,
        today=today,
        cfg=_cfg(),
    )
    assert curve is not None
    assert curve.drawdown_pct < 0.0
    assert curve.current_unit_value < curve.max_unit_value
    assert curve.mode == "normal"


def test_sin_aportes_no_hay_curva() -> None:
    assert (
        build_curve(
            cash_ops=(),
            closed=(),
            lots=(),
            prices={},
            fx_by_date={},
            fallback_fx=1.0,
            today=dt.date(2026, 6, 26),
            cfg=_cfg(),
        )
        is None
    )
