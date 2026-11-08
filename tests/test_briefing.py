"""Bloque determinista del informe (``core/briefing``).

Fija las garantias que sostienen la revision manual: las banderas salen de umbrales
de config (no de numeros escritos en el flujo), el stop tecnico respeta la regla
"soporte - buffer de ATR" con sus dos clamps, y las alertas de concentracion son
informativas.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from swing.adapters.config_loader import load_strategy
from swing.core.briefing import brief_positions, build
from swing.core.models import BriefingConfig
from swing.core.stats import PositionStats

CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"
STRATEGY = load_strategy(CONFIG_DIR / "strategy.yaml")
POLICY: BriefingConfig = STRATEGY.briefing  # type: ignore[assignment]


def _row(
    *,
    ticker: str = "AAA",
    context: dict[str, object] | None = None,
    fields: dict[str, object] | None = None,
) -> PositionStats:
    return PositionStats(ticker=ticker, context=context or {}, fields=fields or {})


def _one(row: PositionStats, *, patrimonio: float = 1_000_000.0):
    briefs = brief_positions([row], patrimonio_clp=patrimonio, policy=POLICY, review=STRATEGY.review)
    return briefs[0]


def test_policy_de_ejemplo_es_la_esperada() -> None:
    assert POLICY.position_max_pct == 12.0
    assert POLICY.stop.buffer_atr == 0.5
    assert POLICY.stop.min_atr_mult == 2.0
    assert POLICY.stop.max_distance_pct == 15.0


def test_flags_de_ganancia_perdida_y_tecnicos() -> None:
    row = _row(
        context={"price_usd": 100.0, "ret_usd_pct": 30.0, "weight_invested_pct": 15.0, "earnings_sessions": 5},
        fields={
            "price_vs_sma200_pct": -5.0,
            "rsi": 75.0,
            "atr_pct": 5.0,
            "target_upside_pct": 5.0,
            "target_price": 90.0,
            "rs_pct": -3.0,
        },
    )
    flags = _one(row).flags
    assert "ganancia>25%" in flags
    assert "bajo SMA200" in flags
    assert "RSI>70" in flags
    assert "ATR%>4" in flags
    assert "upside<10%" in flags
    assert "sobre objetivo" in flags
    assert "earnings<=10 ses" in flags
    assert "peso>12%" in flags
    assert "RS<0" in flags


def test_flags_de_perdida_y_sobreventa() -> None:
    row = _row(context={"price_usd": 100.0, "ret_usd_pct": -15.0}, fields={"rsi": 25.0})
    flags = _one(row).flags
    assert "perdida>10%" in flags
    assert "RSI<30" in flags


def test_flag_indicador_sospechoso_por_pb() -> None:
    row = _row(context={"price_usd": 100.0}, fields={"pb": 2824.17})
    assert "indicador sospechoso (P/B)" in _one(row).flags


def test_flag_indicador_sospechoso_por_serie_congelada() -> None:
    row = _row(context={"price_usd": 100.0}, fields={"flat_tail_sessions": 57.0})
    assert "indicador sospechoso (serie plana sin volumen)" in _one(row).flags


def test_stop_tecnico_usa_el_soporte_mas_cercano_menos_buffer() -> None:
    row = _row(
        context={"price_usd": 100.0, "value_clp": 1_000_000.0},
        fields={"atr": 2.0, "sma50": 96.0, "sma200": 90.0, "low_20": 97.0, "low_50": 95.0},
    )
    brief = _one(row)
    assert brief.stop == pytest.approx(96.0)  # 97 (Min 20 ses) - 0.5*2
    assert brief.stop_dist_pct == pytest.approx(4.0)
    assert brief.stop_rule == "Min 20 ses - 0.5xATR"
    assert brief.risk_pct == pytest.approx(1_000_000.0 * 0.04 / 1_000_000.0 * 100.0)


def test_stop_tecnico_cae_al_soporte_de_respaldo_sin_soportes() -> None:
    row = _row(context={"price_usd": 100.0}, fields={"atr": 2.0, "sma50": 70.0})
    brief = _one(row)
    assert brief.stop == pytest.approx(94.0)  # 100 - 3*2
    assert brief.stop_rule == "precio - 3xATR"


def test_stop_tecnico_acota_el_piso_de_atr() -> None:
    # Soporte a 2 % del precio: el piso de distancia (2xATR = 4 %) lo aleja a 96.
    row = _row(context={"price_usd": 100.0}, fields={"atr": 2.0, "low_20": 98.0})
    brief = _one(row)
    assert brief.stop == pytest.approx(96.0)  # ceil = 100 - 2*2
    assert "clamp 2xATR" in brief.stop_rule


def test_stop_con_atr_alto_no_supera_el_tope_de_riesgo() -> None:
    # Con ATR 10 el piso de distancia (2xATR = 20 %) quedaria mas abajo que el tope
    # de riesgo (15 %): manda el tope, asi que el stop queda en el soporte - buffer
    # (94), no en 80.
    row = _row(context={"price_usd": 100.0}, fields={"atr": 10.0, "low_20": 99.0})
    brief = _one(row)
    assert brief.stop == pytest.approx(94.0)  # 99 (Min 20 ses) - 0.5*10
    assert brief.stop_dist_pct == pytest.approx(6.0)
    assert "clamp" not in brief.stop_rule


def test_stop_tecnico_acota_el_techo_del_15_por_ciento() -> None:
    row = _row(context={"price_usd": 100.0}, fields={"atr": 0.5, "low_50": 85.2})
    brief = _one(row)
    assert brief.stop == pytest.approx(85.0)  # floor = 100 * 0.85
    assert "clamp al 15%" in brief.stop_rule


def test_stop_sin_atr_es_indeterminado() -> None:
    row = _row(context={"price_usd": 100.0}, fields={})
    brief = _one(row)
    assert brief.stop is None
    assert brief.stop_rule == "sin ATR"


def test_take_profit_toma_el_nivel_mas_cercano_sobre_el_recorrido_minimo() -> None:
    row = _row(context={"price_usd": 100.0}, fields={"high_20": 120.0, "high_50": 130.0, "high_52w": 200.0})
    brief = _one(row)
    assert brief.take_profit == pytest.approx(120.0)
    assert brief.tp_label == "techo de rango (corto plazo)"


def test_take_profit_ignora_un_nivel_pegado_al_precio() -> None:
    # El maximo de 20 sesiones esta a 0.02 %: no es un objetivo. El analista si lo es.
    row = _row(context={"price_usd": 100.0}, fields={"high_20": 100.02, "high_50": 100.5, "target_price": 130.0})
    brief = _one(row)
    assert brief.take_profit == pytest.approx(130.0)
    assert brief.tp_label == "objetivo medio"


def test_take_profit_sin_nivel_suficiente_es_sin_techo() -> None:
    row = _row(context={"price_usd": 100.0}, fields={"high_20": 102.0, "high_52w": 103.0})
    brief = _one(row)
    assert brief.take_profit is None
    assert brief.tp_label == "sin techo claro"


def test_take_profit_prioriza_el_objetivo_y_luego_sin_techo() -> None:
    con_objetivo = _one(_row(context={"price_usd": 100.0}, fields={"target_price": 140.0}))
    # v6: el objetivo se acota al tope de recorrido del Tipo A (+30 %).
    assert con_objetivo.take_profit == pytest.approx(130.0)
    assert con_objetivo.tp_label == "objetivo medio"
    assert "acotado" in con_objetivo.tp_note

    sin_niveles = _one(_row(context={"price_usd": 100.0}, fields={}))
    assert sin_niveles.take_profit is None
    assert sin_niveles.tp_label == "sin techo claro"


def test_alertas_de_caja_peso_tema_e_ia() -> None:
    rows = [
        _row(ticker="AAA", context={"theme": "IA", "value_clp": 500_000.0, "weight_invested_pct": 15.0}),
    ]
    panel = {
        "cash_pct": 10.0,
        "themes": {"IA": 40.0},
        "factor_pcts": {"ia_ampliada": 50.0},
        "factor_max_pct": {"ia_ampliada": 45.0},
    }
    briefing = build(rows, panel=panel, patrimonio_clp=1_000_000.0, policy=POLICY)
    joined = " | ".join(briefing.alerts)
    assert "caja 10.00 % bajo el piso 15 %" in joined
    assert "AAA: peso 15.00 % sobre el tope 12 %" in joined
    assert "tema IA: 40.00 % sobre el tope 35 %" in joined
    assert "IA ampliada: 50.00 % sobre el tope 45 %" in joined


def test_alerta_de_tema_no_cuenta_el_cajon_sin_tema() -> None:
    panel = {"cash_pct": 18.0, "themes": {"sin_tema": 50.0, "IA": 40.0}}
    briefing = build([], panel=panel, patrimonio_clp=1.0, policy=POLICY)
    joined = " | ".join(briefing.alerts)
    assert "sin_tema" not in joined
    assert "tema IA: 40.00 % sobre el tope 35 %" in joined


def test_build_expone_agregados_de_factor_y_tema() -> None:
    panel = {
        "cash_pct": 18.0,
        "aggregate_theme_pcts": {"semiconductores y equipos": 30.0},
        "max_theme_pct": 35.0,
        "factor_pcts": {"ia_ampliada": 44.0},
        "factor_max_pct": {"ia_ampliada": 45.0},
    }
    briefing = build([], panel=panel, patrimonio_clp=1.0, policy=POLICY)
    assert briefing.cash_pct == pytest.approx(18.0)
    assert briefing.semis_pct == pytest.approx(30.0)
    assert briefing.semis_max_pct == pytest.approx(35.0)
    assert briefing.ia_pct == pytest.approx(44.0)


def test_riesgo_patrimonial_es_none_sin_patrimonio() -> None:
    row = _row(context={"price_usd": 100.0, "value_clp": 1_000_000.0}, fields={"atr": 2.0, "low_20": 97.0})
    brief = _one(row, patrimonio=0.0)
    assert brief.risk_pct is None


def test_banderas_de_trampas_de_datos_y_percentiles() -> None:
    row = _row(
        context={"price_usd": 100.0, "sessions_open": 5.0, "coverage_pct": 40.0},
        fields={
            "profit_margin": 70.0,
            "oper_margin": 65.0,
            "revenue_growth": 350.0,
            "rsi_percentile": 85.0,
            "atr_pct_percentile": 85.0,
            "rs_1m_pct": 10.0,
            "rs_3m_pct": 5.0,
            "rs_pct": 1.0,
        },
    )
    brief = _one(row)
    assert "margen neto>operativo" in brief.flags
    assert "margen oper.>60%" in brief.flags
    assert "crecimiento>300%" in brief.flags
    assert "RSI pct>80" in brief.flags
    assert "ATR% pct>80" in brief.flags
    assert "RS acelera" in brief.flags
    assert "sesiones<10" in brief.flags
    assert "cobertura<60%" in brief.flags
    # La clasificacion del prompt v6 separa tecnicas, fundamentales y contexto.
    assert "RS acelera" in brief.flags_technical
    assert "margen neto>operativo" in brief.flags_fundamental
    assert "sesiones<10" in brief.flags_context


def test_tipo_sl_por_atr_y_por_cluster_de_semis() -> None:
    atr_alto = _one(_row(context={"price_usd": 100.0}, fields={"atr_pct": 5.0}))
    assert atr_alto.sl_type == "B"
    semis = brief_positions(
        [_row(ticker="NVDA", context={"price_usd": 100.0}, fields={"atr_pct": 2.0})],
        patrimonio_clp=1_000_000.0,
        policy=POLICY,
        semis_tickers=("NVDA", "MU"),
    )[0]
    assert semis.sl_type == "B"
    normal = _one(_row(context={"price_usd": 100.0}, fields={"atr_pct": 2.0}))
    assert normal.sl_type == "A"


def test_alerta_2_solo_en_tipo_b_y_riesgo_teorico() -> None:
    row = _row(
        context={"price_usd": 100.0, "value_clp": 500_000.0, "entry_usd": 95.0},
        fields={"atr": 5.0, "atr_pct": 5.0, "high_20": 105.0},
    )
    brief = _one(row)
    assert brief.sl_type == "B"
    # El Chandelier (105 - 2*5 = 95) cae bajo el piso de 2.5xATR (100 - 12.5 = 87.5).
    assert brief.alarm_1 == pytest.approx(87.5)
    assert brief.alarm_2 == pytest.approx(82.0)
    assert brief.alarm_2_dist_pct == pytest.approx(18.0)
    assert brief.risk_theoretical_pct == pytest.approx(500_000.0 * 0.18 / 1_000_000.0 * 100.0)


def test_tp_distancia_y_rr_contra_la_alerta_1() -> None:
    row = _row(
        context={"price_usd": 100.0},
        fields={"atr": 5.0, "high_20": 104.0, "high_52w": 110.0},
    )
    brief = _one(row)
    assert brief.take_profit == pytest.approx(110.0)
    assert brief.tp_distance_pct == pytest.approx(10.0)
    # Alarma en el piso de 2.5xATR: 100 - 12.5 = 87.5. (110 - 100) / (100 - 87.5).
    assert brief.tp_rr == pytest.approx(10.0 / 12.5)
    assert "R:R TP<1.5" in brief.flags


def test_tp_tipo_b_admite_mas_recorrido_que_el_tipo_a() -> None:
    # ATR % >= 4 -> Tipo B: el tope sube de +30 % a +40 %.
    row = _row(
        context={"price_usd": 100.0},
        fields={"atr_pct": 5.0, "target_price": 150.0},
    )
    brief = _one(row)
    assert brief.sl_type == "B"
    assert brief.take_profit == pytest.approx(140.0)
    assert brief.tp_label == "objetivo medio"
    assert "acotado" in brief.tp_note


def test_tp_sube_al_siguiente_techo_tecnico_para_cumplir_el_rr() -> None:
    # Alarma en el piso 2,5xATR = 87,5. El primer techo (110) da R:R 0,8; el
    # siguiente (130) cumple el minimo de 1,5 y es el que se usa.
    row = _row(
        context={"price_usd": 100.0},
        fields={"atr": 5.0, "high_20": 110.0, "high_52w": 130.0},
    )
    brief = _one(row)
    assert brief.take_profit == pytest.approx(130.0)
    assert brief.tp_label == "max 52S"
    assert "siguiente techo tecnico" in brief.tp_note
    assert brief.tp_rr is not None and brief.tp_rr >= POLICY.take_profit_min_rr


def test_tp_declara_la_excepcion_si_no_hay_techo_para_el_rr() -> None:
    # Sin un techo mas arriba que el primero, se conserva el TP realista y se
    # declara la excepcion con el R:R resultante.
    row = _row(
        context={"price_usd": 100.0},
        fields={"atr": 5.0, "high_20": 110.0},
    )
    brief = _one(row)
    assert brief.take_profit == pytest.approx(110.0)
    assert "excepcion declarada" in brief.tp_note


def test_ajuste_bajo_soporte_y_pisos_de_ganancia() -> None:
    soporte = _one(
        _row(
            context={"price_usd": 100.0},
            fields={"atr": 0.4, "high_20": 100.4, "low_50": 99.0},
        )
    )
    # El piso de 2.5xATR deja la alarma en 99, a 0 % del soporte 99: baja a 99 * 0.99.
    assert soporte.alarm_1 == pytest.approx(98.01)
    assert "1% bajo soporte" in soporte.alarm_1_rule

    breakeven = _one(
        _row(
            context={"price_usd": 100.0, "entry_usd": 100.0, "ret_usd_pct": 20.0},
            fields={"atr": 2.0, "high_20": 104.0},
        )
    )
    assert breakeven.alarm_1 == pytest.approx(100.0)
    assert "piso entrada" in breakeven.alarm_1_rule

    # Con +30 % la Alerta 1 sube a entrada +10 %. Se prueba el ajuste directo porque
    # el tope de alarma del 15 % ya deja la alerta por encima de ese piso.
    from swing.core.briefing import _alarm_1
    from swing.core.verdict import Levels

    alarm, rule = _alarm_1(
        Levels(alarm=100.0),
        {},
        price=140.0,
        entry=100.0,
        ret=35.0,
        policy=POLICY,
        review=STRATEGY.review,
    )
    assert alarm == pytest.approx(110.0)
    assert "piso entrada+10%" in rule


def test_mapa_de_liquidez_y_ventanas_de_earnings() -> None:
    rows = [
        _row(
            ticker="AAA",
            context={"price_usd": 100.0, "value_clp": 100_000.0, "earnings_sessions": 4.0},
            fields={"target_price": 110.0},
        ),
        _row(
            ticker="BBB",
            context={"price_usd": 100.0, "value_clp": 100_000.0, "earnings_sessions": 12.0},
            fields={"target_price": 140.0},
        ),
    ]
    panel = {"cash_pct": 18.0, "themes": {}}
    briefing = build(rows, panel=panel, patrimonio_clp=1_000_000.0, policy=POLICY)
    assert briefing.liquidity_tp_pct == pytest.approx(50.0)
    # AAA conserva su objetivo de 110; el de BBB (140) se acota al tope del Tipo A (130).
    assert briefing.liquidity_cash_clp == pytest.approx(240_000.0)
    assert briefing.liquidity_near_tp_pct == 15.0
    joined = " | ".join(briefing.alerts)
    assert "earnings <=5 ses: AAA" in joined
    assert "earnings <=10 ses: AAA" in joined
    assert "earnings <=15 ses: AAA, BBB" in joined


def test_alerta_de_riesgo_teorico_sobre_2_por_ciento() -> None:
    row = _row(
        ticker="AAA",
        context={"price_usd": 100.0, "value_clp": 500_000.0},
        fields={"atr": 5.0, "high_20": 105.0},
    )
    panel = {"cash_pct": 18.0, "themes": {}}
    briefing = build([row], panel=panel, patrimonio_clp=1_000_000.0, policy=POLICY, review=STRATEGY.review)
    joined = " | ".join(briefing.alerts)
    # Alarma en 87.5 (piso 2.5xATR): 500.000 * 12.5 % / 1.000.000 = 6.25 %.
    assert "AAA: riesgo teorico 6.25 % sobre 2 % del patrimonio" in joined
