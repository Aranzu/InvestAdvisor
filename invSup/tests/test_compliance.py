"""Panel de cumplimiento y stops anclados a la entrada (``core.compliance``)."""

from __future__ import annotations

import datetime as dt

import pytest

from swing.core.compliance import (
    STATUS_INHERITED,
    STATUS_OK,
    STATUS_OVERRISK,
    STATUS_OVERWEIGHT,
    STATUS_STOPPED,
    LedgerEntry,
    breadth_pct,
    initial_stop,
    mode_from_drawdown,
    plan_position,
    summarize,
    take_profit,
)
from swing.core.models import ComplianceConfig, FactorsConfig, FactorSpec

TODAY = dt.date(2026, 11, 7)


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


def _plan(**overrides: object):
    values: dict[str, object] = {
        "ticker": "AMD",
        "entry": 100.0,
        "price": 110.0,
        "atr": 4.0,
        "atr_pct": 4.0,
        "value_clp": 1_000_000.0,
        "patrimonio_clp": 10_000_000.0,
        "weight_invested_pct": 10.0,
        "high_20": 115.0,
        "previous": None,
        "stop_xtb": None,
        "tp_xtb": None,
        "target_price": None,
        "analyst_count": None,
        "earnings_sessions": None,
        "cfg": _cfg(),
    }
    values.update(overrides)
    return plan_position(**values)  # type: ignore[arg-type]


def test_stop_inicial_respeta_el_clamp_8_18() -> None:
    cfg = _cfg()
    # 2,5 x ATR = 2,5; el piso de 8 % manda.
    assert initial_stop(100.0, 1.0, cfg) == pytest.approx(92.0)
    # 2,5 x ATR = 45; el techo de 18 % manda.
    assert initial_stop(100.0, 18.0, cfg) == pytest.approx(82.0)
    assert initial_stop(100.0, None, cfg) is None


def test_primera_corrida_usa_el_stop_inicial_y_marca_heredada() -> None:
    row = _plan(price=90.0, atr=2.0)
    assert row.initial_stop == pytest.approx(92.0)
    assert row.current_stop == pytest.approx(92.0)
    assert row.status == STATUS_INHERITED
    assert row.inherited


def test_posicion_con_stop_vigente_sube_pero_nunca_baja() -> None:
    cfg = _cfg()
    previous = LedgerEntry("AMD", 100.0, TODAY, 92.0, 92.0)
    row = _plan(previous=previous, price=130.0, atr=2.0, high_20=135.0, cfg=cfg)
    # +3R -> entrada + 1R (110); el Chandelier (135 - 6 = 129) gana.
    assert row.current_stop == pytest.approx(129.0)
    higher = _plan(
        previous=LedgerEntry("AMD", 100.0, TODAY, 92.0, row.current_stop or 0.0),
        price=131.0,
        atr=2.0,
        high_20=140.0,
        cfg=cfg,
    )
    assert higher.current_stop is not None and higher.current_stop >= 129.0
    lower = _plan(
        previous=LedgerEntry("AMD", 100.0, TODAY, 92.0, row.current_stop or 0.0),
        price=120.0,
        atr=2.0,
        high_20=125.0,
        cfg=cfg,
    )
    assert lower.current_stop == pytest.approx(129.0)


def test_take_profit_topa_en_30_y_solo_sube() -> None:
    cfg = _cfg()
    tp, formula = take_profit(price=100.0, target_price=150.0, analyst_count=20, previous=None, cfg=cfg)
    assert tp == pytest.approx(130.0)
    assert "topado" in formula
    higher, _ = take_profit(price=100.0, target_price=None, analyst_count=None, previous=tp, cfg=cfg)
    assert higher == pytest.approx(130.0)


def test_estado_sobrepeso_y_sobreriesgo() -> None:
    previous = LedgerEntry("AMD", 100.0, TODAY, 92.0, 92.0)
    assert _plan(weight_invested_pct=13.0, previous=previous).status == STATUS_OVERWEIGHT
    over = _plan(
        price=100.0,
        entry=100.0,
        atr=1.0,
        high_20=100.0,
        value_clp=8_000_000.0,
        patrimonio_clp=10_000_000.0,
        previous=previous,
    )
    assert over.status == STATUS_OVERRISK
    stopped = _plan(price=90.0, entry=100.0, atr=1.0, previous=previous)
    assert stopped.status == STATUS_STOPPED


def test_ok_cuando_cumple_peso_y_riesgo() -> None:
    row = _plan(price=100.0, entry=100.0, atr=2.0, weight_invested_pct=5.0)
    assert row.status == STATUS_OK


def test_resumen_calcula_factor_tema_y_modo() -> None:
    cfg = _cfg()
    factors = FactorsConfig(
        max_factor_pct=40.0,
        max_theme_pct=25.0,
        factors={"ia_ampliada": FactorSpec(max_pct=40.0, tickers=("AMD", "NVDA"), themes=("semiconductors",))},
        themes={"AMD": "semiconductors", "NVDA": "semiconductors", "V": "Financial Services"},
    )
    rows = [
        _plan(ticker="AMD", value_clp=8_000_000.0, weight_invested_pct=80.0),
        _plan(ticker="V", value_clp=2_000_000.0, weight_invested_pct=20.0),
    ]
    summary = summarize(
        rows,
        cash_pct=12.0,
        cfg=cfg,
        factor_cfg=factors,
        themes=factors.themes,
        regime_mode="cautela",
        drawdown_pct=-10.0,
    )
    assert summary.factor_pcts["ia_ampliada"] == pytest.approx(80.0)
    assert summary.biggest_theme == "semiconductors"
    assert summary.mode == "cautela"
    assert summary.infractions >= 1


def test_modo_por_caida() -> None:
    cfg = _cfg()
    assert mode_from_drawdown(None, cfg) == "normal"
    assert mode_from_drawdown(-7.9, cfg) == "normal"
    assert mode_from_drawdown(-8.0, cfg) == "cautela"
    assert mode_from_drawdown(-15.0, cfg) == "defensivo"


def test_amplitud_cuenta_bajo_sma200() -> None:
    rows = [
        {"price_vs_sma200_pct": -1.0},
        {"price_vs_sma200_pct": 3.0},
        {"price_vs_sma200_pct": None},
    ]
    assert breadth_pct(rows) == pytest.approx(50.0)
    assert breadth_pct([{"price_vs_sma200_pct": None}]) is None
