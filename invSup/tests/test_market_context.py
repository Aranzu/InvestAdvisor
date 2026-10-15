"""El contexto de mercado deterministico (``core.market_context``).

Modulo puro: fija el nivel, la variacion a un mes y la distancia a la SMA200 a partir
de una serie ya resuelta, y no inventa nada cuando la serie falta o es muy corta.
"""

from __future__ import annotations

import datetime as dt

import pytest

from swing.core.market import Bar, PriceSeries
from swing.core.market_context import MARKET, MarketInstrument, MarketSpec, instrument

START = dt.date(2026, 1, 1)


def _bar(day: dt.date, price: float) -> Bar:
    return Bar(date=day, open=price, high=price, low=price, close=price, volume=0.0)


def _series(prices: list[float]) -> PriceSeries:
    bars = tuple(_bar(START + dt.timedelta(days=i), price) for i, price in enumerate(prices))
    return PriceSeries("X", bars)


def test_instrument_calcula_nivel_mes_y_sma200() -> None:
    prices = [100.0 + i for i in range(220)]
    item = instrument(MarketSpec("X", "X"), _series(prices))
    assert item.price == pytest.approx(319.0)
    assert item.chg_1m_pct == pytest.approx((319.0 / 298.0 - 1.0) * 100.0)
    expected_sma = sum(prices[-200:]) / 200.0
    assert item.sma200 == pytest.approx(expected_sma)
    assert item.vs_sma200_pct == pytest.approx((319.0 / expected_sma - 1.0) * 100.0)


def test_instrument_sin_serie_no_inventa() -> None:
    base = MarketInstrument(symbol="X", label="X")
    assert instrument(MarketSpec("X", "X"), None) == base
    assert instrument(MarketSpec("X", "X"), _series([])) == base


def test_instrument_serie_corta_deja_lo_que_falta_en_none() -> None:
    item = instrument(MarketSpec("X", "X"), _series([100.0, 101.0, 102.0]))
    assert item.price == pytest.approx(102.0)
    assert item.chg_1m_pct is None
    assert item.sma200 is None
    assert item.vs_sma200_pct is None


def test_market_tiene_los_benchmarks_esperados() -> None:
    symbols = [spec.symbol for spec in MARKET]
    assert symbols == ["^VIX", "SPY", "QQQ", "SOXX", "ECH", "^TNX", "DX-Y.NYB"]
