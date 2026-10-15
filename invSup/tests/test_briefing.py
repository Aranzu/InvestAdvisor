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
    briefs = brief_positions([row], patrimonio_clp=patrimonio, policy=POLICY)
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
    assert con_objetivo.take_profit == pytest.approx(140.0)
    assert con_objetivo.tp_label == "objetivo medio"

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
        "clusters": {"ia_ampliada": {"pct": 50.0, "max_pct": 45.0}},
    }
    briefing = build(rows, panel=panel, patrimonio_clp=1_000_000.0, policy=POLICY)
    joined = " | ".join(briefing.alerts)
    assert "caja 10.00 % bajo el piso 15 %" in joined
    assert "AAA: peso 15.00 % sobre el tope 12 %" in joined
    assert "tema IA: 40.00 % sobre el tope 35 %" in joined
    assert "IA ampliada: 50.00 % sobre el tope 45 %" in joined


def test_alerta_de_tema_no_cuenta_el_cajon_sin_tema() -> None:
    panel = {"cash_pct": 18.0, "themes": {"sin_tema": 50.0, "IA": 40.0}, "clusters": {}}
    briefing = build([], panel=panel, patrimonio_clp=1.0, policy=POLICY)
    joined = " | ".join(briefing.alerts)
    assert "sin_tema" not in joined
    assert "tema IA: 40.00 % sobre el tope 35 %" in joined


def test_build_expone_agregados_de_cluster() -> None:
    panel = {
        "cash_pct": 18.0,
        "clusters": {
            "semis": {"pct": 30.0, "max_pct": 35.0},
            "ia_ampliada": {"pct": 44.0, "max_pct": 45.0},
        },
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
