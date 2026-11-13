"""F-Score de Piotroski y su puerta; momentum 12-1."""

from __future__ import annotations

import datetime as dt
from dataclasses import replace

import pytest

from swing.core.engine import IndicatorFrame
from swing.core.fundamentals import Fundamentals, check_fundamentals
from swing.core.models import FundamentalsConfig
from swing.core.piotroski import AnnualInputs, f_score
from swing.core.reasons import Reason

GOOD_NOW = AnnualInputs(
    net_income=120.0,
    total_assets=1000.0,
    operating_cash_flow=150.0,
    long_term_debt=100.0,
    current_assets=500.0,
    current_liabilities=250.0,
    shares=100.0,
    gross_profit=450.0,
    revenue=1000.0,
)
BEFORE = AnnualInputs(
    net_income=80.0,
    total_assets=1000.0,
    operating_cash_flow=90.0,
    long_term_debt=200.0,
    current_assets=400.0,
    current_liabilities=250.0,
    shares=100.0,
    gross_profit=380.0,
    revenue=950.0,
)


def test_empresa_sana_suma_las_nueve_senales() -> None:
    result = f_score(GOOD_NOW, BEFORE)
    assert (result.score, result.computed) == (9, 9)


def test_una_emision_de_acciones_resta_una_senal() -> None:
    result = f_score(replace(GOOD_NOW, shares=120.0), BEFORE)
    assert (result.score, result.computed) == (8, 9)
    assert result.signals["sin_emision"] is False


def test_datos_faltantes_no_suman_ni_cuentan() -> None:
    result = f_score(AnnualInputs(net_income=10.0, total_assets=100.0), AnnualInputs())
    assert result.computed == 1  # solo ROA positivo es calculable
    assert result.score == 1
    assert result.signals["cfo_positivo"] is None


def test_sin_deuda_actual_el_apalancamiento_cuenta_como_baja() -> None:
    debt_free = AnnualInputs(net_income=1.0, total_assets=10.0, long_term_debt=0.0)
    assert f_score(debt_free, AnnualInputs()).signals["apalancamiento_baja"] is True


def _fund(**kwargs: object) -> Fundamentals:
    return Fundamentals(ticker="AAA", as_of=dt.date(2026, 11, 13), source="test", **kwargs)  # type: ignore[arg-type]


@pytest.fixture
def config_v11() -> FundamentalsConfig:
    from swing.adapters.config_loader import load_strategy

    return load_strategy().fundamentals.model_copy(
        update={"min_f_score": 7, "min_f_score_computed": 7, "require_positive_fcf": True, "min_operating_margin": 0.0}
    )


def test_la_puerta_rechaza_f_score_bajo(config_v11: FundamentalsConfig) -> None:
    f = _fund(f_score=5, f_score_computed=9, operating_margin=0.2, fcf_last_q=10.0, profit_margin=0.1)
    reasons = [r.reason for r in check_fundamentals(f, 100.0, config_v11).rejections]
    assert Reason.FUNDAMENTAL_GRADE in reasons


def test_la_puerta_rechaza_f_score_no_calculable(config_v11: FundamentalsConfig) -> None:
    f = _fund(f_score=8, f_score_computed=4, operating_margin=0.2, fcf_last_q=10.0, profit_margin=0.1)
    reasons = [r.reason for r in check_fundamentals(f, 100.0, config_v11).rejections]
    assert Reason.FUNDAMENTALS_MISSING in reasons


def test_la_puerta_rechaza_fcf_negativo_y_margen_operativo_negativo(config_v11: FundamentalsConfig) -> None:
    f = _fund(f_score=9, f_score_computed=9, operating_margin=-0.1, fcf_last_q=-5.0, profit_margin=0.1)
    details = " ".join(r.detail for r in check_fundamentals(f, 100.0, config_v11).rejections)
    assert "FCF" in details
    assert "operating_margin" in details


def test_empresa_de_calidad_pasa(config_v11: FundamentalsConfig) -> None:
    f = _fund(
        f_score=8,
        f_score_computed=9,
        operating_margin=0.25,
        fcf_last_q=10.0,
        profit_margin=0.18,
        return_on_equity=0.2,
        revenue_growth=0.1,
        debt_to_equity=0.5,
        forward_pe=25.0,
        trailing_pe=30.0,
    )
    result = check_fundamentals(f, 100.0, config_v11)
    assert result.passes, [r.detail for r in result.rejections]


def _frame_from(prices: list[float]) -> IndicatorFrame:
    from swing.adapters.config_loader import load_strategy
    from swing.core.market import Bar, PriceSeries

    start = dt.date(2025, 1, 1)
    bars = tuple(
        Bar(date=start + dt.timedelta(days=i), open=p, high=p * 1.01, low=p * 0.99, close=p, volume=1_000_000.0)
        for i, p in enumerate(prices)
    )
    frame = IndicatorFrame.build(PriceSeries(ticker="AAA", bars=bars), load_strategy())
    assert frame is not None
    return frame


def test_momentum_12_1_ignora_el_ultimo_mes() -> None:
    # 300 ruedas planas en 100 y las ultimas 21 saltan a 150: el 12-1 no ve ese salto.
    flat = [100.0] * 280 + [150.0] * 21
    frame = _frame_from(flat)
    assert frame.returns_skipping(252, 21) == pytest.approx(1.0)
    assert frame.returns(252) == pytest.approx(1.5)


def test_momentum_sin_historia_devuelve_none() -> None:
    frame = _frame_from([100.0] * 120)
    assert frame.returns_skipping(252, 21) is None


def test_percentil_de_momentum_12_1_ordena_el_universo() -> None:
    from swing.core.engine import relative_strength_percentiles

    rising = _frame_from([100.0 + i * 0.5 for i in range(300)])
    flat = _frame_from([100.0] * 300)
    falling = _frame_from([200.0 - i * 0.3 for i in range(300)])
    pct = relative_strength_percentiles({"UP": rising, "FLAT": flat, "DOWN": falling}, 252, 21)
    assert pct["UP"] > pct["FLAT"] > pct["DOWN"]
    assert pct["UP"] == pytest.approx(100.0)
