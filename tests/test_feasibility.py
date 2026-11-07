"""Prueba de factibilidad del Paso 0b (``core.feasibility``)."""

from __future__ import annotations

import datetime as dt

import pytest

from swing.core.compliance import ComplianceRow, summarize
from swing.core.feasibility import build_feasibility
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
        transition_stop_max_pct=15.0,
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


def _row(ticker: str, value: float, weight: float, *, risk: float = 1.0) -> ComplianceRow:
    return ComplianceRow(
        ticker=ticker,
        price=100.0,
        entry=100.0,
        atr_pct=4.0,
        value_clp=value,
        weight_invested_pct=weight,
        weight_equity_pct=weight,
        initial_stop=92.0,
        current_stop=92.0,
        previous_stop=None,
        transition_stop=None,
        take_profit=130.0,
        tp_formula="= analistas",
        stop_dist_price_pct=8.0,
        stop_dist_entry_pct=8.0,
        r_multiple=1.0,
        risk_pct=risk,
        max_weight_open_pct=8.0,
        status="OK",
    )


def test_factibilidad_reduce_factor_y_tema_y_arma_calendario() -> None:
    cfg = _cfg()
    factors = FactorsConfig(
        max_factor_pct=40.0,
        max_theme_pct=25.0,
        factors={
            "ia_ampliada": FactorSpec(max_pct=40.0, tickers=("AMD", "NVDA"), themes=("semiconductors",)),
        },
        themes={"AMD": "semiconductors", "NVDA": "semiconductors_ai", "V": "Financial Services"},
    )
    rows = [
        _row("AMD", 500_000.0, 50.0, risk=2.0),
        _row("NVDA", 200_000.0, 20.0, risk=1.0),
        _row("V", 100_000.0, 10.0, risk=0.2),
    ]
    summary = summarize(rows, cash_pct=20.0, cfg=cfg, factor_cfg=factors, themes=factors.themes, equity_clp=1_000_000.0)
    result = build_feasibility(
        rows,
        summary,
        cfg=cfg,
        factor_cfg=factors,
        themes=factors.themes,
        equity_clp=1_000_000.0,
        cash_clp=200_000.0,
        fx=900.0,
        today=TODAY,
    )
    scenarios = {item["name"]: item for item in result["scenarios"]}
    assert scenarios["actual"]["factor_pct"] == pytest.approx(70.0)
    assert scenarios["actual"]["theme_pct"] == pytest.approx(70.0)
    assert scenarios["todo"]["factor_ok"] is True
    assert scenarios["todo"]["theme_ok"] is True
    assert scenarios["todo"]["factor_pct"] <= 40.0 + 1e-9
    assert scenarios["todo"]["theme_pct"] <= 25.0 + 1e-9
    # AMD pesa 50 % > P3 12 %: el tramo inmediato lo recorta a 10 %.
    immediate = [item for item in result["calendar"] if item.get("week") == 0]
    assert immediate
    assert any(
        reduction.get("ticker") == "AMD"
        for item in immediate
        for reduction in item.get("reductions", [])
        if isinstance(reduction, dict)
    )
    assert result["calendar"]
