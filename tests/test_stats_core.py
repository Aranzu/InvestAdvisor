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
from swing.core.market_context import MarketInstrument, MarketRegime
from swing.core.stats import (
    POSITION_FIELDS,
    ROLES,
    SECTIONS,
    InsiderTrade,
    PositionStats,
    StatsReport,
    all_field_keys,
    coverage,
    derive_fields,
    insider_net,
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


def _report(
    rows: tuple[PositionStats, ...],
    quotes: dict[str, dict[str, object]] | None = None,
) -> StatsReport:
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
        quotes=quotes or {},
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
    assert "| cobertura |" in text
    assert f"| AAA | 3/{len(all_field_keys())} |" in text


def test_render_sin_posiciones() -> None:
    text = render_stats(_report(()))
    assert "Sin posiciones abiertas." in text


def test_render_campo_sin_dato_es_guion() -> None:
    row = PositionStats(ticker="AAA", context={}, fields={})
    text = render_stats(_report((row,)))
    assert "-" in text


def test_render_incluye_insiders_y_su_neto() -> None:
    buy = InsiderTrade(
        date=dt.date(2026, 9, 21),
        owner="Teter Timothy S.",
        relationship="EVP, General Counsel",
        transaction="Buy",
        price=110.5,
        shares=1000.0,
        value=110_500.0,
    )
    sale = InsiderTrade(
        date=dt.date(2026, 9, 18),
        owner="Stevens Mark A.",
        relationship="Director",
        transaction="Sale",
        price=100.0,
        shares=400.0,
        value=40_000.0,
    )
    row = PositionStats(ticker="NVDA", context={}, fields={}, insider_trades=(buy, sale))
    text = render_stats(_report((row,)), show_context=True)
    assert "## Insiders (Finviz)" in text
    assert "| Ticker | Compras | Ventas | Neto USD |" in text
    assert "| NVDA | 1 | 1 | 70.50K |" in text
    assert "Teter Timothy S." in text
    assert "2026-09-21" in text
    assert "110.50K" in text


def test_insider_net_suma_compras_menos_ventas() -> None:
    trades = (
        InsiderTrade(date=None, owner="A", relationship="", transaction="Buy", price=1.0, shares=1.0, value=100.0),
        InsiderTrade(date=None, owner="B", relationship="", transaction="Sale", price=1.0, shares=1.0, value=40.0),
        InsiderTrade(
            date=None,
            owner="C",
            relationship="",
            transaction="Option Exercise",
            price=1.0,
            shares=1.0,
            value=999.0,
        ),
    )
    net = insider_net(trades)
    assert net.buys == 1
    assert net.sells == 1
    assert net.net_value == pytest.approx(60.0)


def test_render_sin_insiders_no_muestra_la_seccion() -> None:
    row = PositionStats(ticker="AAA", context={}, fields={})
    text = render_stats(_report((row,)), show_context=True)
    assert "## Insiders (Finviz)" not in text


def test_stats_report_json_round_trip() -> None:
    trade = InsiderTrade(
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
        insider_trades=(trade,),
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


def test_stats_report_json_round_trip_con_briefing() -> None:
    from swing.core.briefing import Briefing, PositionBrief

    briefing = Briefing(
        positions=(PositionBrief(ticker="NVDA", flags=("RSI>70",), stop=100.0, stop_rule="precio - 3xATR"),),
        alerts=("caja 10.00 % bajo el piso 15 %",),
        position_max_pct=12.0,
        theme_max_pct=35.0,
        cash_pct=10.0,
        cash_min_pct=15.0,
        cash_max_pct=20.0,
        semis_pct=30.0,
        semis_max_pct=35.0,
        ia_pct=44.0,
        ia_max_pct=45.0,
    )
    report = replace(_report(()), briefing=briefing)
    restored = StatsReport.from_dict(json.loads(json.dumps(report.to_dict())))
    assert restored == report
    assert restored.briefing is not None
    assert restored.briefing.positions[0].flags == ("RSI>70",)


def test_stats_report_from_dict_schema_inesperado() -> None:
    with pytest.raises(ValueError, match="schema"):
        StatsReport.from_dict({"schema": 999})


def test_materias_primas_en_el_informe_y_en_el_json() -> None:
    commodities = (
        Commodity(
            key="oro",
            name="Oro",
            symbol="GC=F",
            source="tradingeconomics",
            unit="USD/t.oz",
            price=2400.0,
            chg_1m_pct=3.5,
            chg_ytd_pct=12.0,
            high_52w=2500.0,
            low_52w=2000.0,
        ),
        Commodity(
            key="cobre",
            name="Cobre",
            symbol="",
            source="tradingeconomics",
            unit="CNY/T",
            price=115400.0,
            chg_1m_pct=-16.77,
        ),
    )
    row = PositionStats(ticker="FCX", context={}, fields={})
    report = replace(_report((row,)), commodities=commodities)
    text = render_stats(report, show_context=True)
    assert "## Materias primas" in text
    assert "| Oro | Trading Economics (USD/t.oz) | 2,400.00 | +3.50 % | +12.00 % | 2,500.00 | 2,000.00 |" in text
    assert "| Cobre | Trading Economics (CNY/T) | 115,400.00 | -16.77 % | - | - | - |" in text
    # El prompt v8 manda ignorar el driver: la seccion ya no se renderiza.
    assert "## Driver de materia prima" not in text
    assert StatsReport.from_dict(json.loads(json.dumps(report.to_dict()))).commodities == commodities


def test_materias_primas_sin_datos_no_inventa_precio() -> None:
    empty = Commodity(key="cobre", name="Cobre", symbol="HG=F")
    row = PositionStats(ticker="AAA", context={}, fields={})
    text = render_stats(replace(_report((row,)), commodities=(empty,)), show_context=True)
    assert "| Cobre | - | - | - | - | - | - |" in text


def test_render_panel_de_cumplimiento_y_resumen() -> None:
    row = PositionStats(
        ticker="AAA",
        context={
            "name": "Acorp",
            "value_clp": 800_000.0,
            "weight_invested_pct": 100.0,
            "ret_clp_pct": 12.5,
            "ret_usd_pct": 9.0,
            "entry_usd": 100.0,
        },
        fields={"atr_pct": 4.0},
    )
    report = replace(
        _report((row,)),
        compliance={
            "summary": {
                "open_risk_pct": 1.2,
                "max_open_risk_pct": 10.0,
                "cash_pct": 20.0,
                "min_cash_pct": 10.0,
                "cash_band_max_pct": 20.0,
                "factor_pcts": {"ia_ampliada": 44.0},
                "factor_max_pct": {"ia_ampliada": 40.0},
                "theme_pcts": {"IA": 100.0},
                "max_theme_pct": 25.0,
                "biggest_theme": "IA",
                "biggest_theme_pct": 44.0,
                "infractions": 1,
                "mode": "cautela",
                "breadth_pct": 40.0,
                "avg_correlation": 0.71,
                "beta_spy": 1.12,
                "beta_qqq": 1.08,
                "earnings_5": ["AAA"],
                "earnings_10": ["AAA"],
                "earnings_15": ["AAA"],
                "drawdown_pct": -9.0,
                "drawdown_mode": "cautela",
                "regime_mode": "normal",
                "sessions_to_purchases": 45,
                "sessions_to_liquidation": 75,
                "purchases_until": "2027-01-15",
                "liquidate_until": "2027-03-01",
                "equity_curve": {
                    "max_unit_value": 110.0,
                    "current_unit_value": 100.1,
                    "drawdown_pct": -9.0,
                    "days_since_peak": 12,
                    "mode": "cautela",
                    "points": 100,
                },
            },
            "positions": [
                {
                    "ticker": "AAA",
                    "weight_invested_pct": 100.0,
                    "atr_pct": 4.0,
                    "initial_stop": 90.0,
                    "current_stop": 92.0,
                    "stop_dist_price_pct": 8.0,
                    "stop_dist_entry_pct": 8.0,
                    "r_multiple": 0.8,
                    "risk_pct": 1.2,
                    "max_weight_open_pct": 9.4,
                    "new_entry_pct": 6.0,
                    "new_entry_clp": 500_000.0,
                    "new_entry_shares": 5.0,
                    "take_profit": 130.0,
                    "status": "OK",
                }
            ],
        },
    )
    text = render_stats(report)
    assert "## Panel de cumplimiento (base P12 = equity XTB)" in text
    assert "| AAA | - | 100.00 % | 4.00 % | 90.00 | 92.00 |" in text
    assert "| 0.80 | 1.20 % | 6.00 % | 500,000 CLP | 5.0000 | 130.00 | - | no | OK |" in text
    assert "- Riesgo abierto total: 1.20 % (P5 10.00 %)." in text
    assert "- Factor ia_ampliada: 44.00 % (tope 40.00 %, base P12)." in text
    assert "- Modo vigente: **cautela**" in text
    assert "- Infracciones: 1." in text
    assert "- Amplitud de la cartera: 40.00 % bajo su SMA200." in text
    assert "- Diversificacion: correlacion media por pares 0.71" in text
    assert "- Earnings <= 5 sesiones: AAA." in text
    assert "- Calendario P10: compras hasta 2027-01-15 (45 sesiones)" in text
    assert "- Curva ajustada por aportes (valor cuota base 100): maximo 110.00" in text


def test_render_panel_marca_la_heredada_tipo_a() -> None:
    row = PositionStats(ticker="MU", context={"price_usd": 80.0, "entry_usd": 100.0}, fields={})
    report = replace(
        _report((row,)),
        compliance={
            "summary": {},
            "positions": [
                {
                    "ticker": "MU",
                    "price": 80.0,
                    "entry": 100.0,
                    "initial_stop": 82.0,
                    "current_stop": 82.0,
                    "stop_dist_price_pct": -2.5,
                    "stop_dist_entry_pct": 18.0,
                    "inherited_type": "A",
                    "status": "HEREDADA_TIPO_A",
                }
            ],
        },
    )
    text = render_stats(report)
    assert "HEREDADA_TIPO_A" in text
    assert "| MU | - | - | - | 82.00 | 82.00 | -2.50 % | +18.00 % |" in text


def test_render_cantidad_con_decimales() -> None:
    row = PositionStats(ticker="MELI", context={"volume": 2.0771}, fields={})
    text = render_stats(_report((row,)))
    assert "2.0771" in text


def test_render_muestra_el_mercado_sin_contexto() -> None:
    row = PositionStats(ticker="AAA", context={}, fields={})
    market = (MarketInstrument(symbol="^VIX", label="VIX (volatilidad)", price=18.5, chg_1m_pct=10.0),)
    text = render_stats(replace(_report((row,)), market=market))
    assert "## Contexto de mercado" in text


def test_render_v8_mapas_ledger_lotes_factibilidad_y_json() -> None:
    row = PositionStats(
        ticker="AAA",
        context={
            "price_usd": 100.0,
            "entry_usd": 90.0,
            "value_clp": 300_000.0,
            "weight_pct": 30.0,
            "first_entry": "2026-01-02",
            "days_since_first_entry": 30,
            "lots_detail": [{"date": "2026-01-02", "price_usd": 90.0, "shares": 3.0, "value_clp": 300_000.0}],
        },
        fields={"break_even": 90.0, "trailing_stop": 95.0},
    )
    report = replace(
        _report((row,)),
        maps={
            "factor_name": "ia_ampliada",
            "factor_pct": 30.0,
            "factor_max_pct": 40.0,
            "factor_tickers": ["AAA"],
            "rows": [{"ticker": "AAA", "factor_ia": "si", "theme": "semis", "weight_pct": 30.0}],
            "aggregate_theme": {"name": "semiconductores y equipos", "pct": 30.0, "max_pct": 25.0, "members": ["AAA"]},
        },
        feasibility={
            "scenarios": [
                {"name": "todo", "weeks": 2, "factor_pct": 35.0, "theme_pct": 20.0, "cash_pct": 12.0},
            ],
            "limits": {"factor_pct": 40.0, "theme_pct": 25.0},
            "calendar": [
                {"week": 1, "date": "2026-11-14", "reductions": [{"ticker": "AAA", "shares": 1.0, "value_clp": 10_000.0}], "factor_pct": 35.0, "theme_pct": 20.0, "cash_pct": 12.0}
            ],
        },
        data_status={
            "close_confirmed": True,
            "last_session": "2026-11-06",
            "next_session": "2026-11-09",
            "incomplete": [{"ticker": "AAA", "missing": ["PEG"]}],
        },
        compliance={
            "summary": {
                "equity_curve": {
                    "max_unit_value": 110.0,
                    "current_unit_value": 100.0,
                    "drawdown_pct": -9.0,
                    "days_since_peak": 3,
                    "max_equity_clp": 1_200_000.0,
                    "max_equity_date": "2026-10-01",
                    "current_equity_clp": 1_000_000.0,
                    "max_equity_adjusted_clp": 1_100_000.0,
                }
            },
            "positions": [
                {
                    "ticker": "AAA",
                    "current_stop": 95.0,
                    "break_even": 90.0,
                    "trailing_stop": 95.0,
                    "inherited_type": "B",
                    "stop_viable": True,
                    "status": "HEREDADA_TIPO_B",
                }
            ],
        },
        panel={"contributions_clp": 1_000_000.0, "withdrawals_clp": 0.0, "net_contributions_clp": 1_000_000.0, "cash_broker_clp": 50_000.0, "cash_calculated_clp": 50_000.0, "cash_difference_clp": 0.0, "recent_flows": []},
    )
    text = render_stats(report)
    for heading in (
        "## Mapas de factor y tema (base P12)",
        "## Registro de stops y decisiones (ledger)",
        "## Earnings, analistas y deterioro de tesis (V1)",
        "## Factibilidad y calendario de reduccion (Paso 0b)",
        "## Estado de los datos",
        "## Datos del panel (JSON)",
        "### Stops dinamicos",
        "## Aportes y caja",
    ):
        assert heading in text
    assert "Patrimonio maximo: 1,200,000 CLP el 2026-10-01" in text
    assert '"schema": 10' in text
    assert "Datos incompletos: AAA: PEG." in text


def test_render_contexto_de_mercado() -> None:
    row = PositionStats(ticker="AAA", context={}, fields={})
    market = (
        MarketInstrument(
            symbol="^VIX",
            label="VIX (volatilidad)",
            price=18.5,
            chg_1m_pct=10.0,
            sma200=16.0,
            vs_sma200_pct=15.625,
        ),
        MarketInstrument(symbol="SPY", label="S&P 500 (SPY)", price=500.0),
    )
    text = render_stats(replace(_report((row,)), market=market), show_context=True)
    assert "## Contexto de mercado" in text
    assert "| VIX (volatilidad) | 18.50 | +10.00 % | 16.00 | +15.62 % |" in text
    assert "| S&P 500 (SPY) | 500.00 | - | - | - |" in text


def test_render_regimen_de_mercado_del_paso_1() -> None:
    row = PositionStats(ticker="AAA", context={}, fields={})
    regime = MarketRegime(
        level="medio",
        count=2,
        bucket="2-3",
        signals=("VIX", "SPY<SMA200"),
        near_threshold=("10Y>SMA200+10% (+9.81 vs 10)",),
        suspicious=("Semis (SOXX)",),
        usdclp=950.25,
        vix=22.0,
        spy_vs_sma200_pct=-1.5,
        tnx_vs_sma200_pct=9.81,
        dxy_vs_sma200_pct=1.0,
        qqq_1m_pct=-4.0,
        vix_1m_pct=12.0,
        portfolio_breadth_pct=55.0,
        mode="cautela",
    )
    report = replace(
        _report((row,)),
        market=(MarketInstrument(symbol="^VIX", label="VIX (volatilidad)", price=22.0),),
        regime=regime,
    )
    text = render_stats(report, show_context=True)
    assert "### Regimen (Paso 1)" in text
    assert "- Nivel **medio** (tramo **2-3**; 2 senales de estres de 6)." in text
    assert "Senales cumplidas: VIX, SPY<SMA200." in text
    assert "A menos de 1 pp del umbral" in text
    assert "Benchmarks con dato sospechoso (no cuentan): Semis (SOXX)." in text
    assert "Amplitud de la cartera: 55.00 % bajo su SMA200." in text
    assert "USD/CLP: 950.25" in text


def test_render_ya_no_trae_las_tablas_de_stops_antiguas() -> None:
    text = render_stats(_report((PositionStats(ticker="MU", context={}, fields={}),)))
    assert "## Panel de posicion (derivado)" not in text
    assert "### Alertas SL" not in text
    assert "### Niveles de vigilancia" not in text
    assert "## Resumen determinista" not in text


def test_stats_report_json_round_trip_mercado_y_panel() -> None:
    market = (MarketInstrument(symbol="^VIX", label="VIX (volatilidad)", price=18.5, chg_1m_pct=10.0, sma200=16.0),)
    regime = MarketRegime(level="bajo", count=0, signals=(), near_threshold=(), usdclp=950.0, vix=18.5)
    panel = {"patrimonio_clp": 1.0, "themes": {"IA": 100.0}, "max_theme_pct": 30.0}
    report = replace(
        _report((PositionStats(ticker="AAA", context={}, fields={}),)),
        market=market,
        regime=regime,
        panel=panel,
    )
    restored = StatsReport.from_dict(json.loads(json.dumps(report.to_dict())))
    assert restored.market == market
    assert restored.regime == regime
    assert restored.panel == panel
    assert restored == report


def test_render_oculta_el_contexto_por_defecto() -> None:
    row = PositionStats(
        ticker="AAA",
        context={"name": "Acorp", "price_usd": 10.0},
        fields={"pe": 12.5, "dividend_yield": 3.0, "perf_month": -5.0, "short_float": 8.0},
    )
    curado = render_stats(_report((row,)))
    assert "## Dividendos" not in curado
    assert "## Desempeno" not in curado
    assert "Contexto informativo adicional" in curado
    # El dato sigue existiendo; solo se oculta en el render curado.
    assert "3.00 %" not in curado
    completo = render_stats(_report((row,)), show_context=True)
    assert "## Dividendos" in completo
    assert "## Desempeno" in completo
    assert "3.00 %" in completo
    assert "8.00 %" in completo


def test_render_conserva_los_campos_que_deciden() -> None:
    row = PositionStats(
        ticker="AAA",
        context={"name": "Acorp", "price_usd": 10.0},
        fields={"pe": 12.5, "atr": 1.1, "sma20": 11.0},
    )
    text = render_stats(_report((row,)))
    assert "## Valoracion" in text
    assert "## Tecnico" in text
    assert "12.50" in text
    assert "1.10" in text
    assert "11.00" in text


def test_todos_los_campos_tienen_un_rol_valido() -> None:
    specs = [*POSITION_FIELDS, *(spec for _, group in SECTIONS for spec in group)]
    assert specs
    assert {spec.role for spec in specs} <= ROLES


def _row(ticker: str = "AAA") -> PositionStats:
    return PositionStats(ticker=ticker, context={"name": ticker, "price_usd": 10.0}, fields={"pe": 12.5})


def test_render_muestra_el_horario_extendido_con_datos() -> None:
    report = _report(
        (_row(),),
        quotes={
            "AAA": {
                "price_usd": 10.0,
                "price_source": "p",
                "earnings_date": None,
                "earnings_known": False,
                "pre_market": {
                    "price": 100.0,
                    "change": 1.0,
                    "change_pct": 0.95,
                    "volume": 1000,
                    "time_iso": "2026-10-10T13:12:00Z",
                    "source": "yahoo",
                },
                "after_market": {
                    "price": 101.0,
                    "change": -0.5,
                    "change_pct": -0.25,
                    "volume": 500,
                    "time_iso": "2026-10-10T20:00:00Z",
                    "source": "yahoo",
                },
            }
        },
    )
    text = render_stats(report)
    assert "## Horario extendido (Pre/Post Market)" in text
    assert "100.00 (+0.95 %)" in text
    assert "101.00 (-0.25 %)" in text
    assert "1,000" in text
    assert "500" in text
    assert "09:12 ET" in text  # 13:12Z en horario de verano de NY (EDT)


def test_render_sin_horario_extendido_no_muestra_la_seccion() -> None:
    report = _report((_row(),), quotes={"AAA": {"price_usd": 10.0}})
    assert "Horario extendido" not in render_stats(report)


def test_render_horario_extendido_un_solo_lado() -> None:
    report = _report(
        (_row(),),
        quotes={"AAA": {"pre_market": {"price": 9.5, "change_pct": -5.0, "volume": None}}},
    )
    text = render_stats(report)
    assert "## Horario extendido (Pre/Post Market)" in text
    assert "| AAA | 9.50 (-5.00 %) | - | - | - |" in text


def test_stats_report_json_round_trip_con_horario_extendido() -> None:
    report = _report(
        (_row(),),
        quotes={
            "AAA": {
                "price_usd": 10.0,
                "price_source": "p",
                "earnings_date": None,
                "earnings_known": False,
                "pre_market": {"price": 100.0, "change_pct": 1.0, "volume": 5, "time_iso": "2026-10-10T13:00:00Z", "source": "yahoo"},
            }
        },
    )
    restored = StatsReport.from_dict(json.loads(json.dumps(report.to_dict())))
    assert restored == report
