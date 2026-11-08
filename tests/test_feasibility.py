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


def _row(
    ticker: str,
    value: float,
    weight: float,
    *,
    risk: float = 1.0,
    new_entry_clp: float | None = None,
    inherited_type: str = "",
    stop_viable: bool = True,
    status: str = "OK",
    stop_dist: float = 8.0,
) -> ComplianceRow:
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
        stop_dist_price_pct=stop_dist,
        stop_dist_entry_pct=stop_dist,
        r_multiple=1.0,
        risk_pct=risk,
        max_weight_open_pct=8.0,
        status=status,
        new_entry_clp=new_entry_clp,
        inherited=bool(inherited_type),
        inherited_type=inherited_type,
        stop_viable=stop_viable,
    )


def test_p16_reparte_en_cuatro_semanas_y_respeta_el_25_por_ciento() -> None:
    cfg = _cfg().model_copy(
        update={"max_weight_hold_pct": 100.0, "max_weight_hold_trim_pct": 100.0, "reduction_max_weeks": 4}
    )
    factors = FactorsConfig(
        max_factor_pct=40.0,
        max_theme_pct=25.0,
        factors={"ia_ampliada": FactorSpec(max_pct=40.0, tickers=("AMD",), themes=())},
        themes={"AMD": "semiconductors"},
    )
    rows = [_row("AMD", 1_000_000.0, 100.0, risk=0.2)]
    summary = summarize(rows, cash_pct=0.0, cfg=cfg, factor_cfg=factors, themes=factors.themes, equity_clp=1_000_000.0)
    result = build_feasibility(
        rows,
        summary,
        cfg=cfg,
        factor_cfg=factors,
        themes=factors.themes,
        equity_clp=1_000_000.0,
        cash_clp=0.0,
        fx=900.0,
        today=TODAY,
    )
    weeks = [item for item in result["calendar"] if item.get("week") not in (None, 0)]
    assert weeks
    assert max(int(item["week"]) for item in weeks) <= 4
    for week in weeks:
        for reduction in week["reductions"]:
            assert reduction["value_clp"] <= 250_000.0 + 1e-6
            assert str(week["date"]) > TODAY.isoformat()


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


def _reduce_all(result: dict[str, object]) -> float:
    total = 0.0
    for week in result.get("calendar", []):
        if not isinstance(week, dict):
            continue
        for reduction in week.get("reductions", []):
            if isinstance(reduction, dict):
                total += float(reduction.get("value_clp") or 0.0)
    return total


def test_calendario_incluye_adopcion_y_fechas_en_lunes() -> None:
    cfg = _cfg().model_copy(
        update={
            "max_weight_hold_pct": 100.0,
            "max_weight_hold_trim_pct": 100.0,
            "reduction_max_weeks": 4,
            "max_factor_pct": 100.0,
            "max_theme_pct": 100.0,
        }
    )
    rows = [
        _row(
            "AMD",
            1_000_000.0,
            50.0,
            risk=0.2,
            new_entry_clp=500_000.0,
            inherited_type="B",
        )
    ]
    summary = summarize(rows, cash_pct=0.0, cfg=cfg, equity_clp=1_000_000.0)
    result = build_feasibility(
        rows,
        summary,
        cfg=cfg,
        factor_cfg=None,
        themes={},
        equity_clp=1_000_000.0,
        cash_clp=0.0,
        fx=900.0,
        today=dt.date(2026, 11, 20),
    )
    # Sin P3/P4/P13, toda la venta es adopcion: 1M -> tamano de entrada nueva 0,5M.
    assert _reduce_all(result) == pytest.approx(500_000.0)
    weeks = [item for item in result["calendar"] if item.get("week") not in (None, 0)]
    assert [item["date"] for item in weeks] == ["2026-11-23", "2026-11-30"]
    for week in weeks:
        assert dt.date.fromisoformat(str(week["date"])).weekday() == 0
        for reduction in week["reductions"]:
            assert reduction["value_clp"] <= 250_000.0 + 1e-6
            assert "adopcion" in reduction["reason"]


def test_adopcion_y_tope_de_factor_no_se_suman() -> None:
    cfg = _cfg().model_copy(
        update={
            "max_weight_hold_pct": 100.0,
            "max_weight_hold_trim_pct": 100.0,
            "reduction_max_weeks": 4,
        }
    )
    factors = FactorsConfig(
        max_factor_pct=40.0,
        max_theme_pct=100.0,
        factors={"ia_ampliada": FactorSpec(max_pct=40.0, tickers=("AMD",), themes=())},
        themes={"AMD": "Financial Services"},
    )
    rows = [
        _row(
            "AMD",
            1_000_000.0,
            100.0,
            risk=0.2,
            new_entry_clp=500_000.0,
            inherited_type="B",
        )
    ]
    summary = summarize(
        rows,
        cash_pct=0.0,
        cfg=cfg,
        factor_cfg=factors,
        themes=factors.themes,
        equity_clp=1_000_000.0,
    )
    result = build_feasibility(
        rows,
        summary,
        cfg=cfg,
        factor_cfg=factors,
        themes=factors.themes,
        equity_clp=1_000_000.0,
        cash_clp=0.0,
        fx=900.0,
        today=TODAY,
    )
    # Adopcion pide 0,5M y el tope de factor pide 0,6M: manda el menor objetivo
    # (400k), asi que la venta total es 0,6M, no 1,1M.
    assert _reduce_all(result) == pytest.approx(600_000.0)
    reasons = {
        reduction["reason"]
        for week in result["calendar"]
        for reduction in week.get("reductions", [])
        if isinstance(reduction, dict)
    }
    assert any("adopcion" in reason and "factor" in reason for reason in reasons)


def test_p13_sin_stop_viable_sale_todo_en_la_primera_sesion() -> None:
    cfg = _cfg().model_copy(
        update={
            "max_weight_hold_pct": 100.0,
            "max_weight_hold_trim_pct": 100.0,
            "max_factor_pct": 100.0,
            "max_theme_pct": 100.0,
        }
    )
    rows = [
        _row(
            "SNDK",
            1_000_000.0,
            50.0,
            risk=0.2,
            new_entry_clp=900_000.0,
            inherited_type="A",
            stop_viable=False,
        )
    ]
    summary = summarize(rows, cash_pct=0.0, cfg=cfg, equity_clp=1_000_000.0)
    result = build_feasibility(
        rows,
        summary,
        cfg=cfg,
        factor_cfg=None,
        themes={},
        equity_clp=1_000_000.0,
        cash_clp=0.0,
        fx=900.0,
        today=TODAY,
    )
    assert len(result["calendar"]) == 1
    week = result["calendar"][0]
    assert week["week"] == 0
    assert week["reductions"][0]["value_clp"] == pytest.approx(1_000_000.0)
    assert "P13" in week["reductions"][0]["reason"]
