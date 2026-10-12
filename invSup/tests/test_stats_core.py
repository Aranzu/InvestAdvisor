"""El nucleo del informe de estadisticas (``core/stats``): mezcla, derivados y render.

Modulo puro: fija la politica de mezcla (Yahoo gana, Finviz rellena), las formulas
de los derivados (variacion contra medias y niveles de 52 semanas) y el formato de
unidades (porcentajes en puntos, sufijos de magnitud).
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import replace

import pytest

from swing.core.commodities import Commodity
from swing.core.stats import (
    InsiderBuy,
    PositionStats,
    StatsReport,
    all_field_keys,
    coverage,
    derive_fields,
    merge_fields,
    render_stats,
)

TODAY = dt.date(2026, 10, 7)


def test_merge_fields_la_primera_fuente_gana() -> None:
    yahoo = {"pe": 10.0, "roe": None, "beta": 1.1}
    finviz = {"pe": 99.0, "roe": 20.0, "ps": 3.0}
    merged = merge_fields(yahoo, finviz)
    assert merged["pe"] == 10.0
    assert merged["roe"] == 20.0
    assert merged["ps"] == 3.0
    assert merged["beta"] == 1.1


def test_derive_fields_contra_medias_y_52_semanas() -> None:
    fields = {
        "sma20": 100.0,
        "sma50": 90.0,
        "sma200": 80.0,
        "high_52w": 120.0,
        "low_52w": 50.0,
        "target_price": 130.0,
    }
    out = derive_fields(fields, 110.0)
    assert out["price_vs_sma20_pct"] == pytest.approx(10.0)
    assert out["price_vs_sma50_pct"] == pytest.approx(110.0 / 90.0 * 100.0 - 100.0)
    assert out["price_vs_sma200_pct"] == pytest.approx(110.0 / 80.0 * 100.0 - 100.0)
    assert out["pct_to_52w_high"] == pytest.approx(110.0 / 120.0 * 100.0 - 100.0)
    assert out["pct_from_52w_low"] == pytest.approx(110.0 / 50.0 * 100.0 - 100.0)
    assert out["target_upside_pct"] == pytest.approx(130.0 / 110.0 * 100.0 - 100.0)


def test_derive_fields_sin_precio_o_sin_bases() -> None:
    assert derive_fields({"sma20": 100.0}, None) == {}
    assert derive_fields({"sma20": 100.0}, 0.0) == {}
    assert derive_fields({"sma20": 0.0}, 100.0) == {}
    assert derive_fields({"target_price": 130.0}, 100.0) == {"target_upside_pct": pytest.approx(30.0)}


def test_coverage_cuenta_presentes_y_lista_faltantes() -> None:
    present, missing = coverage({"pe": 12.0, "roe": 20.0})
    assert present == 2
    assert "P/E" not in missing
    assert "ROE" not in missing
    assert "P/B" in missing


def test_all_field_keys_no_repite() -> None:
    keys = all_field_keys()
    assert len(keys) == len(set(keys))
    assert "pe" in keys
    assert "short_float" in keys
    assert "insider_own" in keys


def _report(rows: tuple[PositionStats, ...]) -> StatsReport:
    return StatsReport(
        account="123456",
        today=TODAY,
        mode="live",
        fx=950.0,
        fx_source="USDCLP=X",
        equity_clp=1_000_000.0,
        cash_clp=100_000.0,
        positions_value_clp=900_000.0,
        rows=rows,
    )


def test_render_incluye_secciones_formato_y_cobertura() -> None:
    row = PositionStats(
        ticker="AAA",
        context={"name": "Acorp S.A.", "price_usd": 10.0, "value_clp": 900_000.0},
        fields={"pe": 12.5, "roe": 25.0, "revenue_growth": 8.0},
    )
    text = render_stats(_report((row,)))
    assert "# Estadisticas de posiciones - cuenta 123456" in text
    assert "## Posicion y valuacion" in text
    assert "## Valoracion" in text
    assert "## Cobertura de datos" in text
    assert "Acorp S.A." in text
    assert "12.50" in text
    assert "25.00 %" in text
    assert "8.00 %" in text
    assert "AAA" in text


def test_render_sin_posiciones() -> None:
    text = render_stats(_report(()))
    assert "Sin posiciones abiertas." in text


def test_render_campo_sin_dato_es_guion() -> None:
    row = PositionStats(ticker="AAA", context={}, fields={})
    text = render_stats(_report((row,)))
    assert "-" in text


def test_render_incluye_compras_de_insiders() -> None:
    buy = InsiderBuy(
        date=dt.date(2026, 9, 21),
        owner="Teter Timothy S.",
        relationship="EVP, General Counsel",
        transaction="Buy",
        price=110.5,
        shares=1000.0,
        value=110_500.0,
    )
    row = PositionStats(ticker="NVDA", context={}, fields={}, insider_buys=(buy,))
    text = render_stats(_report((row,)))
    assert "## Compras de insiders (Finviz)" in text
    assert "Teter Timothy S." in text
    assert "2026-09-21" in text
    assert "110.50K" in text


def test_render_sin_compras_no_muestra_la_seccion() -> None:
    row = PositionStats(ticker="AAA", context={}, fields={})
    text = render_stats(_report((row,)))
    assert "## Compras de insiders" not in text


def test_stats_report_json_round_trip() -> None:
    buy = InsiderBuy(
        date=dt.date(2026, 9, 21),
        owner="X",
        relationship="Director",
        transaction="Buy",
        price=1.0,
        shares=2.0,
        value=2.0,
    )
    row = PositionStats(
        ticker="NVDA",
        context={"name": "Nvidia", "value_clp": 1.0},
        fields={"pe": 10.0, "sma20": None},
        insider_buys=(buy,),
    )
    report = StatsReport(
        account="1",
        today=TODAY,
        mode="live",
        fx=950.0,
        fx_source="x",
        equity_clp=1.0,
        cash_clp=2.0,
        positions_value_clp=3.0,
        rows=(row,),
        limitations=("a",),
        quotes={"NVDA": {"price_usd": 1.0, "earnings_date": None}},
        themes={"NVDA": "IA"},
        sectors={"X": "Tech"},
        fundamentals={"NVDA": {"ticker": "NVDA", "as_of": TODAY.isoformat(), "source": "s", "trailing_pe": 10.0}},
    )
    restored = StatsReport.from_dict(json.loads(json.dumps(report.to_dict())))
    assert restored == report


def test_stats_report_from_dict_schema_inesperado() -> None:
    with pytest.raises(ValueError, match="schema"):
        StatsReport.from_dict({"schema": 999})


def test_materias_primas_en_el_informe_y_en_el_json() -> None:
    commodities = (
        Commodity(
            key="oro",
            name="Oro",
            symbol="GC=F",
            proxy=False,
            price=2400.0,
            chg_1m_pct=3.5,
            chg_ytd_pct=12.0,
            high_52w=2500.0,
            low_52w=2000.0,
        ),
        Commodity(key="litio", name="Litio", symbol="LIT", proxy=True, price=45.0, chg_1m_pct=-2.0),
    )
    row = PositionStats(ticker="SQM-B", context={}, fields={"driver": "Litio (proxy)", "driver_1m_pct": -2.0})
    report = replace(_report((row,)), commodities=commodities)
    text = render_stats(report)
    assert "## Materias primas" in text
    assert "| Oro | GC=F | 2,400.00 | +3.50 % | +12.00 % | 2,500.00 | 2,000.00 |" in text
    assert "| Litio (proxy) | LIT | 45.00 | -2.00 % | - | - | - |" in text
    # La fila de SQM-B trae su driver de materia prima.
    assert "| SQM-B | Litio (proxy) | -2.00 % |" in text
    assert StatsReport.from_dict(json.loads(json.dumps(report.to_dict()))).commodities == commodities


def test_materias_primas_sin_datos_no_inventa_precio() -> None:
    empty = Commodity(key="cobre", name="Cobre", symbol="HG=F", proxy=False)
    row = PositionStats(ticker="AAA", context={}, fields={})
    text = render_stats(replace(_report((row,)), commodities=(empty,)))
    assert "| Cobre | HG=F | - | - | - | - | - |" in text
