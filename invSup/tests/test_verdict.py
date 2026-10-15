"""Veredicto determinista de ``PROMPT PORTAFOLIO`` (``core/verdict``).

Fija las garantias que reemplazan la aritmetica manual del prompt: los niveles de
revision y alarma con sus clamps, el estado del objetivo, los disparadores T, los
motivos M y los ejes E1-E4 con su mapeo a accion. Sin politica (``review`` ausente)
todo sale vacio: ningun umbral vive en el codigo.
"""

from __future__ import annotations

from pathlib import Path

from swing.adapters.config_loader import load_strategy
from swing.core.fundamentals import AxisRatings
from swing.core.verdict import (
    DecisionRecord,
    compute_levels,
    detect_triggers,
    find_motives,
    implied_action,
    score_axes,
)

CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"
STRATEGY = load_strategy(CONFIG_DIR / "strategy.yaml")
REVIEW = STRATEGY.review
assert REVIEW is not None


def _levels(**fields: object):  # type: ignore[no-untyped-def]
    return compute_levels(fields, price=100.0, policy=REVIEW)


def test_review_level_es_sma50_menos_medio_atr() -> None:
    levels = _levels(atr=3.0, sma50=97.0, high_20=104.0)
    assert levels.review == 95.5
    assert levels.review_perforated is False
    assert levels.review_distance_pct is not None


def test_review_level_perforado_si_el_precio_esta_bajo_la_sma50() -> None:
    levels = compute_levels({"atr": 3.0, "sma50": 105.0, "high_20": 110.0}, price=100.0, policy=REVIEW)
    assert levels.review == 103.5
    assert levels.review_perforated is True
    assert levels.review_distance_pct is None


def test_alarma_aplica_el_piso_de_dos_y_medio_atr() -> None:
    # Chandelier = 104 - 9 = 95, pero el piso 2,5xATR = 92,5 lo aleja (el ejemplo del prompt).
    levels = _levels(atr=3.0, sma50=97.0, high_20=104.0)
    assert levels.alarm == 92.5
    assert levels.alarm_rule.startswith("piso")
    assert levels.alarm_distance_pct == 7.5


def test_alarma_aplica_el_tope_del_quince_por_ciento() -> None:
    # ATR alto empuja la alarma muy lejos; el tope la deja a 15 % bajo el precio.
    levels = _levels(atr=10.0, sma50=97.0, high_20=101.0)
    assert levels.alarm == 85.0
    assert "tope" in levels.alarm_rule


def test_riesgo_usa_el_valor_de_la_posicion() -> None:
    levels = compute_levels(
        {"atr": 3.0, "sma50": 97.0, "high_20": 104.0},
        price=100.0,
        value_clp=1_000_000.0,
        patrimonio_clp=5_000_000.0,
        policy=REVIEW,
    )
    # (100 - 92.5) / 100 * 1.000.000 = 75.000 CLP sobre 5.000.000 = 1.5 %.
    assert levels.risk_pct == 1.5


def test_objetivo_estados() -> None:
    lejos = _levels(target_price=130.0, analyst_count=12.0)
    assert (lejos.objective, lejos.objective_state, lejos.objective_valid) == (130.0, "lejos", True)
    cerca = _levels(target_price=110.0, analyst_count=12.0)
    assert cerca.objective_state == "cerca"
    alcanzado = _levels(target_price=102.0, analyst_count=12.0)
    assert alcanzado.objective_state == "alcanzado"
    superado = _levels(target_price=95.0, analyst_count=12.0)
    assert superado.objective_state == "superado"
    # Pocos analistas: objetivo no de referencia.
    poco = _levels(target_price=130.0, analyst_count=3.0)
    assert poco.objective_valid is False
    assert poco.objective_state == "sin objetivo"


def test_zona_tactica_exige_rr_minimo() -> None:
    # Con alarma 92.5 (riesgo 7.5), el techo 104 da R:R 0.53: no alcanza.
    levels = _levels(atr=3.0, sma50=97.0, high_20=104.0)
    assert levels.tactical_zone is None
    # Un techo a 115 da R:R 2.0 >= 1.5.
    con_techo = _levels(atr=3.0, sma50=97.0, high_20=104.0, high_50=115.0)
    assert con_techo.tactical_zone == 115.0
    assert con_techo.tactical_rr is not None and con_techo.tactical_rr >= 1.5


def test_piso_de_catastrofe_solo_con_peso_alto_o_earnings_cerca() -> None:
    sin = compute_levels({"atr": 3.0, "sma50": 97.0, "high_20": 104.0}, price=100.0, policy=REVIEW)
    assert sin.catastrophe_floor is None
    # Con earnings cerca se admite el borde lejano del rango (20 %).
    earnings = compute_levels(
        {"atr": 3.0, "sma50": 97.0, "high_20": 104.0},
        price=100.0,
        earnings_sessions=5.0,
        policy=REVIEW,
    )
    assert earnings.catastrophe_floor == 80.0
    # Por peso sobre el tope, sin earnings, se usa el borde cercano (15 %).
    alto = compute_levels(
        {"atr": 3.0, "sma50": 97.0, "high_20": 104.0},
        price=100.0,
        weight_invested_pct=20.0,
        position_max_pct=12.0,
        policy=REVIEW,
    )
    assert alto.catastrophe_floor == 85.0


def test_subir_stop_solo_al_alcanzar_el_multiplo_de_r() -> None:
    temprano = compute_levels({"atr": 3.0, "sma50": 97.0, "high_20": 104.0}, price=100.0, progress_r=1.0, policy=REVIEW)
    assert temprano.stop_raise_due is False
    maduro = compute_levels({"atr": 3.0, "sma50": 97.0, "high_20": 104.0}, price=100.0, progress_r=2.0, policy=REVIEW)
    assert maduro.stop_raise_due is True


def test_sin_politica_no_hay_niveles() -> None:
    levels = compute_levels({"atr": 3.0, "sma50": 97.0, "high_20": 104.0}, price=100.0, policy=None)
    assert levels.review is None
    assert levels.alarm is None


def test_disparador_t6_por_balance_debil() -> None:
    triggers = detect_triggers({"debt_to_equity": 3.0, "current_ratio": 0.8}, policy=REVIEW)
    assert [trigger.code for trigger in triggers] == ["T6"]
    assert detect_triggers({"debt_to_equity": 1.0, "current_ratio": 2.0}, policy=REVIEW) == ()


def test_motivo_m2_objetivo_alcanzado_sin_momentum() -> None:
    levels = _levels(target_price=102.0, analyst_count=12.0)
    motives = find_motives(triggers=(), levels=levels, fields={}, momentum=False, policy=REVIEW)
    assert [motive.code for motive in motives] == ["M2"]


def test_motivo_m3_necesita_ruptura_y_fundamentales_debiles() -> None:
    levels = _levels()
    fields = {
        "price_vs_sma200_pct": -5.0,
        "rs_pct": -3.0,
        "revenue_growth": -1.0,
        "earnings_growth": -2.0,
    }
    motives = find_motives(triggers=(), levels=levels, fields=fields, momentum=False, policy=REVIEW)
    assert "M3" in [motive.code for motive in motives]
    # Sin los fundamentales debiles no alcanza: la ruptura sola no es motivo.
    solo_tecnico = find_motives(
        triggers=(),
        levels=levels,
        fields={"price_vs_sma200_pct": -5.0, "rs_pct": -3.0},
        momentum=False,
        policy=REVIEW,
    )
    assert "M3" not in [motive.code for motive in solo_tecnico]


def test_motivo_m5_pico_de_ciclo_con_crecimiento_en_baja() -> None:
    levels = _levels()
    motives = find_motives(
        triggers=(),
        levels=levels,
        fields={"oper_margin": 62.0, "revenue_growth": -1.0},
        momentum=True,
        policy=REVIEW,
    )
    assert "M5" in [motive.code for motive in motives]
    # Margen alto con crecimiento sano no es pico de ciclo.
    sano = find_motives(
        triggers=(),
        levels=levels,
        fields={"oper_margin": 62.0, "revenue_growth": 15.0, "earnings_growth": 20.0, "eps_next_y": 10.0},
        momentum=True,
        policy=REVIEW,
    )
    assert "M5" not in [motive.code for motive in sano]


def test_ejes_e1_e4_y_accion() -> None:
    # Fundamental solido, tecnico en momentum, sin earnings: mantener.
    fundamental = AxisRatings(value=2, quality=2, growth=2, projections=2)
    fields = {
        "price_vs_sma50_pct": 3.0,
        "price_vs_sma200_pct": 10.0,
        "rs_1m_pct": 2.0,
        "rs_3m_pct": 3.0,
        "rs_pct": 4.0,
        "rsi": 55.0,
        "target_price": 130.0,
        "target_upside_pct": 30.0,
        "analyst_count": 12.0,
    }
    levels = compute_levels(fields, price=100.0, policy=REVIEW)
    score = score_axes(fields=fields, price=100.0, fundamental=fundamental, levels=levels, triggers=(), policy=REVIEW)
    assert score.e1 == 2
    assert score.e3 == 2
    assert score.total >= REVIEW.axes.action_maintain_total
    assert implied_action(score, policy=REVIEW) == "Mantener"


def test_veto_e1_menos_dos_pide_venta_parcial() -> None:
    fundamental = AxisRatings(value=2, quality=2, growth=2, projections=2)
    fields: dict[str, object] = {}
    levels = compute_levels(fields, price=100.0, policy=REVIEW)
    score = score_axes(
        fields=fields,
        price=100.0,
        fundamental=fundamental,
        levels=levels,
        triggers=(),
        policy=REVIEW,
        thesis_broken=True,
    )
    assert score.e1 == -2
    assert implied_action(score, policy=REVIEW) == "Vender parcial (minimo)"


def test_e4_ventana_11_15_pide_menos_colchon_que_la_estricta() -> None:
    # Precio 106 sobre minimo 100 = 6 % de colchon: alcanza el 5 % de 11-15 sesiones,
    # pero no el 8 % de <=10 sesiones.
    fields = {"low_20": 100.0, "earnings_sessions": 12.0}
    levels = compute_levels(fields, price=106.0, policy=REVIEW)
    score = score_axes(fields=fields, price=106.0, fundamental=None, levels=levels, triggers=(), policy=REVIEW)
    assert score.e4 == 0
    ajustado = score_axes(
        fields={"low_20": 100.0, "earnings_sessions": 12.0},
        price=103.0,
        fundamental=None,
        levels=levels,
        triggers=(),
        policy=REVIEW,
    )
    assert ajustado.e4 == -1


def test_e3_menos_uno_solo_por_estar_bajo_la_sma200() -> None:
    fields = {"price_vs_sma50_pct": 1.0, "price_vs_sma200_pct": -2.0, "rsi": 50.0}
    levels = compute_levels({}, price=100.0, policy=REVIEW)
    score = score_axes(fields=fields, price=100.0, fundamental=None, levels=levels, triggers=(), policy=REVIEW)
    assert score.e3 == -1


def test_disparador_unico_deja_e1_en_menos_uno() -> None:
    from swing.core.verdict import Trigger

    fundamental = AxisRatings(value=2, quality=2, growth=2, projections=2)
    levels = compute_levels({}, price=100.0, policy=REVIEW)
    score = score_axes(
        fields={},
        price=100.0,
        fundamental=fundamental,
        levels=levels,
        triggers=(Trigger("T6", "x"),),
        policy=REVIEW,
    )
    assert score.e1 == -1


def test_decision_record_ida_y_vuelta() -> None:
    record = DecisionRecord(
        date="2026-10-14",
        ticker="AAA",
        action="Mantener",
        price=100.0,
        entry_usd=95.0,
        review_level=95.5,
        alarm_level=92.5,
        objective=130.0,
        motive="M2",
        trigger="T6",
    )
    again = DecisionRecord.from_dict(record.to_dict())
    assert again == record
    assert DecisionRecord.from_dict({"action": "x"}) is None
