"""El contexto de mercado deterministico (``core.market_context``).

Modulo puro: fija el nivel, la variacion a un mes y la distancia a la SMA200 a partir
de una serie ya resuelta, y no inventa nada cuando la serie falta o es muy corta.
"""

from __future__ import annotations

import datetime as dt

import pytest

from swing.core.market import Bar, PriceSeries
from swing.core.market_context import MARKET, MarketInstrument, MarketSpec, instrument, regime

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
    assert symbols == ["^VIX", "SPY", "QQQ", "SOXX", "ECH", "^TNX", "DX-Y.NYB", "USDCLP=X", "GC=F"]
    # USD/CLP pide una ventana corta: Yahoo publica velas incoherentes en ventanas largas.
    usdclp = next(spec for spec in MARKET if spec.symbol == "USDCLP=X")
    assert usdclp.window_days == 10


def _regime(instruments: tuple[MarketInstrument, ...]):  # type: ignore[no-untyped-def]
    return regime(
        instruments,
        vix_max=20.0,
        tnx_above_sma200_pct=10.0,
        dxy_above_sma200_pct=5.0,
        qqq_1m_max_pct=-8.0,
        vix_1m_min_pct=40.0,
        suspicious_1m_abs_pct=40.0,
        suspicious_sma_band=(0.5, 2.0),
        near_threshold_pp=1.0,
    )


def test_regimen_cuenta_senales_y_nivel() -> None:
    instruments = (
        MarketInstrument(symbol="^VIX", label="VIX", price=25.0),
        MarketInstrument(symbol="SPY", label="SPY", vs_sma200_pct=-2.0),
        MarketInstrument(symbol="^TNX", label="10Y", vs_sma200_pct=12.0),
        MarketInstrument(symbol="DX-Y.NYB", label="DXY", vs_sma200_pct=1.0),
    )
    result = _regime(instruments)
    assert result.count == 3
    assert result.level == "medio"
    assert result.signals == ("VIX", "SPY<SMA200", "10Y>SMA200+10%")
    assert result.vix == 25.0
    assert result.spy_vs_sma200_pct == -2.0


def test_regimen_bajo_y_senal_cerca_del_umbral() -> None:
    instruments = (
        MarketInstrument(symbol="^VIX", label="VIX", price=17.0),
        MarketInstrument(symbol="SPY", label="SPY", vs_sma200_pct=5.0),
        MarketInstrument(symbol="^TNX", label="10Y", vs_sma200_pct=9.5),
        MarketInstrument(symbol="DX-Y.NYB", label="DXY", vs_sma200_pct=2.0),
        MarketInstrument(symbol="USDCLP=X", label="USD/CLP", price=950.0),
    )
    result = _regime(instruments)
    assert result.count == 0
    assert result.level == "bajo"
    assert result.near_threshold == ("10Y>SMA200+10% (+9.50 vs 10)",)
    assert result.usdclp == 950.0


def test_regimen_sin_datos_no_inventa_senales() -> None:
    result = _regime((MarketInstrument(symbol="^VIX", label="VIX"),))
    assert result.count is None
    assert result.level == "sin dato"
    assert result.signals == ()


def test_regimen_v7_suma_qqq_y_vix_mensual_y_marca_defensivo() -> None:
    instruments = (
        MarketInstrument(symbol="^VIX", label="VIX", price=25.0, chg_1m_pct=20.0),
        MarketInstrument(symbol="SPY", label="SPY", vs_sma200_pct=-2.0),
        MarketInstrument(symbol="^TNX", label="10Y", vs_sma200_pct=12.0),
        MarketInstrument(symbol="DX-Y.NYB", label="DXY", vs_sma200_pct=8.0),
        MarketInstrument(symbol="QQQ", label="QQQ", chg_1m_pct=-9.0),
    )
    result = _regime(instruments)
    assert result.count == 5
    assert result.level == "alto"
    assert result.mode == "defensivo"
    assert "QQQ 1M<=-8%" in result.signals


def test_benchmark_sospechoso_no_cuenta() -> None:
    instruments = (
        # SOXX con -75 % en un mes y nivel muy por debajo de su SMA200.
        MarketInstrument(symbol="QQQ", label="QQQ", price=136.5, chg_1m_pct=-75.8, sma200=476.0),
        MarketInstrument(symbol="SPY", label="SPY", vs_sma200_pct=5.0),
    )
    result = _regime(instruments)
    assert "QQQ" in result.suspicious
    assert result.signals == ()
    assert result.count == 0
