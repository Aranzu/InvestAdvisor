"""Seccion 16.4: precedencia de salidas, transiciones y earnings.

Lo importante aqui no es "la regla X dispara", es **"cuando dos reglas se cumplen,
gana la de arriba y la otra queda con su motivo"**. Un motor que dispara las dos
produce dos tickets que no se pueden colocar: el broker no acepta dos ordenes de
venta sobre las mismas acciones (seccion 2).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import replace
from pathlib import Path

import pytest

from swing.adapters.config_loader import load_strategy
from swing.core.calendar import SessionCalendar
from swing.core.exits import (
    EXIT_PRECEDENCE,
    ExitContext,
    aggregate_progress_r,
    apply_exits,
    evaluate_exits,
    horizon_state,
)
from swing.core.models import HorizonState
from swing.core.portfolio import CapTrim
from swing.core.reasons import Action, ExitCause, Reason
from swing.core.state import TradeState, TrancheState

CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"
STRATEGY = load_strategy(CONFIG_DIR / "strategy.yaml")
EXITS = STRATEGY.exits
STOPS = STRATEGY.stops
HORIZON = STRATEGY.horizon
CAL = SessionCalendar.us()

ENTRY = 100.0
INITIAL_STOP = 92.0  # R = 8
BREAKEVEN = 101.0  # entrada x 1,01
ATR = 3.0
VOLUME = 20.0

#: Jueves. Con ``lead_sessions = 2`` la noche del martes ya esta a dos sesiones.
EARNINGS = dt.date(2026, 11, 5)
TUESDAY = dt.date(2026, 11, 3)
WEDNESDAY = dt.date(2026, 11, 4)

TRANCHES = (TrancheState("A", 10.0, 1.0), TrancheState("R", 10.0, None))
RUNNER_ONLY = (TrancheState("R", 10.0, None),)


def trade(**over: object) -> TradeState:
    """Trade sano de dos tramos: entrada 100, stop 92, R 8, cierre 100."""
    base: dict[str, object] = {
        "ticker": "AMD",
        "entry_price": ENTRY,
        "initial_stop": INITIAL_STOP,
        "stop": INITIAL_STOP,
        "broker_stop": INITIAL_STOP,
        "volume": VOLUME,
        "close": 100.0,
        "days_open": 5,
        "atr": ATR,
        "highest_n": 105.0,
        "max_progress_r": 0.5,
        "tranches": TRANCHES,
    }
    return TradeState(**{**base, **over})  # type: ignore[arg-type]


def ctx(**over: object) -> ExitContext:
    base: dict[str, object] = {
        "exits": EXITS,
        "stops": STOPS,
        "horizon": HORIZON,
        "calendar": CAL,
        "today": TUESDAY,
    }
    return ExitContext(**{**base, **over})  # type: ignore[arg-type]


def note(evaluation: object, cause: Reason) -> object:
    return next(s for s in evaluation.signals if s.cause is cause)  # type: ignore[attr-defined]


# --------------------------------------------------------------------------- #
# Precedencia: gana la primera
# --------------------------------------------------------------------------- #


def test_stop_breach_vende_todo() -> None:
    out = evaluate_exits(trade(close=91.5), ctx())
    assert out.primary_cause is Reason.STOP_BREACH
    assert out.should_exit is True
    assert out.exit_volume == VOLUME
    assert out.primary.exit_cause is ExitCause.STOP
    assert out.primary.detail.startswith("cierre 91.50 contra stop 92.00")


def test_stop_breach_y_earnings_a_la_vez_solo_manda_el_stop() -> None:
    # Caso 16.4: el primero gana y el otro queda SUPPRESSED con su motivo.
    out = evaluate_exits(trade(close=91.5), ctx(earnings_date=EARNINGS))
    assert out.primary_cause is Reason.STOP_BREACH
    assert out.should_exit is True
    suppressed = note(out, Reason.EARNINGS_DERISK)
    assert suppressed.suppressed_by is Reason.STOP_BREACH
    assert suppressed.execute is False


def test_sin_stop_gana_al_alcance_de_la_meta() -> None:
    # STOP_MISSING es regla 2 y TP_HIT es 3: una posicion sin stop y con el cierre
    # en la meta coloca el stop primero y reevalua la meta la noche siguiente.
    out = evaluate_exits(trade(close=108.0, broker_stop=None), ctx())
    assert out.primary_cause is Reason.STOP_MISSING
    assert out.should_exit is False
    assert out.stop_target == INITIAL_STOP
    assert note(out, Reason.TP_HIT).suppressed_by is Reason.STOP_MISSING


def test_el_orden_de_las_reglas_es_el_del_spec() -> None:
    # Si el orden cambiara, "gana la primera" significaria otra cosa.
    assert EXIT_PRECEDENCE == (
        Reason.STOP_BREACH,
        Reason.STOP_MISSING,
        Reason.STOP_MISMATCH,
        Reason.TP_HIT,
        Reason.EARNINGS_DERISK,
        Reason.STALL_EXIT,
        Reason.RAISE_STOP,
        Reason.MAX_HOLD,
        Reason.HORIZON_TIGHTEN,
        Reason.CAP_TRIM,
        Reason.THESIS_EXIT,
    )


def test_todas_las_reglas_que_se_cumplen_se_reportan() -> None:
    # Meta alcanzada + earnings + maximo de vida + tesis rota: las cuatro se ven,
    # una sola manda.
    out = evaluate_exits(
        trade(close=108.0, days_open=95),
        ctx(earnings_date=EARNINGS, thesis_broken=True),
    )
    assert out.primary_cause is Reason.TP_HIT
    assert set(out.reasons) == {
        Reason.TP_HIT,
        Reason.EARNINGS_DERISK,
        Reason.MAX_HOLD,
        Reason.THESIS_EXIT,
    }
    assert out.should_exit is True
    assert all(s.suppressed_by is Reason.TP_HIT for s in out.suppressed)


# --------------------------------------------------------------------------- #
# Proteccion (regla 2)
# --------------------------------------------------------------------------- #


def test_stop_ausente_se_coloca() -> None:
    out = evaluate_exits(trade(broker_stop=None), ctx())
    assert out.primary_cause is Reason.STOP_MISSING
    assert out.primary.action is Action.EDIT_STOP
    assert out.stop_target == INITIAL_STOP
    assert out.primary.volume == VOLUME


def test_stop_distinto_se_corrige_hacia_arriba() -> None:
    out = evaluate_exits(trade(stop=95.0, broker_stop=88.0), ctx())
    assert out.primary_cause is Reason.STOP_MISMATCH
    assert out.stop_target == 95.0


def test_un_stop_objetivo_no_positivo_no_revienta() -> None:
    # Un stop 0 no es un precio: no se mide deriva ni se corrige a ciegas.
    out = evaluate_exits(trade(stop=0.0, broker_stop=88.0), ctx())
    assert Reason.STOP_MISMATCH not in out.reasons


def test_un_stop_mas_alto_en_el_broker_no_se_baja() -> None:
    # Regla de oro: un stop nunca baja. El motor informa, no edita.
    out = evaluate_exits(trade(stop=95.0, broker_stop=100.0), ctx())
    assert out.stop_target is None
    assert out.should_exit is False
    assert "nunca baja" in note(out, Reason.STOP_MISMATCH).detail


def test_el_redondeo_del_broker_no_pide_editar_el_stop() -> None:
    # 0,005 de diferencia sobre 92 es 0,005 %: es redondeo, no discrepancia.
    out = evaluate_exits(trade(broker_stop=92.005), ctx())
    assert out.signals == ()


# --------------------------------------------------------------------------- #
# Meta del tramo A (regla 3) y transiciones (seccion 5.6)
# --------------------------------------------------------------------------- #


def test_meta_alcanzada_vende_el_tramo_a() -> None:
    out = evaluate_exits(trade(close=108.0), ctx())
    assert out.primary_cause is Reason.TP_HIT
    assert out.primary.tranche == "A"
    assert out.exit_volume == 10.0
    assert out.primary.price == 108.0
    assert f"apertura de {WEDNESDAY.isoformat()}" in out.primary.detail


def test_al_vender_el_tramo_a_el_runner_queda_asegurado() -> None:
    antes = trade(close=108.0)
    despues = apply_exits(antes, evaluate_exits(antes, ctx()))
    assert despues.open_volume == 10.0
    assert despues.tranches[0].volume == 0.0
    assert despues.locked is True
    assert despues.last_exit_cause is ExitCause.TP1.value


def test_la_venta_del_tramo_a_deja_el_remanente_en_breakeven() -> None:
    # El ticket lleva las dos cosas: vender A y dejar el stop del remanente en 101,0
    # (seccion 4.3). No es una segunda salida.
    antes = trade(close=108.0)
    out = evaluate_exits(antes, ctx())
    assert out.exit_volume == 10.0
    assert out.stop_target == pytest.approx(BREAKEVEN)
    despues = apply_exits(antes, out)
    assert despues.stop == pytest.approx(BREAKEVEN)
    assert despues.locked is True


def test_la_noche_siguiente_el_trail_sigue_subiendo_desde_ahi() -> None:
    antes = trade(close=108.0)
    vendido = apply_exits(antes, evaluate_exits(antes, ctx()))
    siguiente = replace(vendido, broker_stop=vendido.stop, highest_n=115.0, close=110.0)
    out = evaluate_exits(siguiente, ctx())
    # Maximo 115, ATR 3 -> Chandelier 106,0, que ahora supera al breakeven.
    assert out.primary_cause is Reason.RAISE_STOP
    assert out.stop_target == pytest.approx(106.0)


def test_el_runner_nunca_puede_bajar_su_stop() -> None:
    antes = trade(close=108.0)
    vendido = apply_exits(antes, evaluate_exits(antes, ctx()))
    siguiente = replace(vendido, broker_stop=vendido.stop, highest_n=95.0, close=101.5)
    despues = apply_exits(siguiente, evaluate_exits(siguiente, ctx()))
    assert despues.stop == pytest.approx(BREAKEVEN)


# --------------------------------------------------------------------------- #
# Earnings (regla 4, seccion 4.6)
# --------------------------------------------------------------------------- #


def test_earnings_a_dos_sesiones_sale_esa_noche() -> None:
    # Caso 16.4: la orden se genera el martes 2026-11-03 y se ejecuta el miercoles.
    out = evaluate_exits(trade(close=101.0), ctx(earnings_date=EARNINGS))
    assert out.primary_cause is Reason.EARNINGS_DERISK
    assert out.exit_volume == VOLUME
    assert f"apertura de {WEDNESDAY.isoformat()}" in out.primary.detail


def test_un_trade_asegurado_mantiene_el_runner() -> None:
    state = replace(trade(), locked=True, tranches=RUNNER_ONLY)
    out = evaluate_exits(state, ctx(earnings_date=EARNINGS))
    assert out.should_exit is False
    assert out.exit_volume == 0.0
    earnings = note(out, Reason.EARNINGS_DERISK)
    assert earnings.is_informational
    assert "se mantiene el runner" in earnings.detail
    # No desplaza a nadie: si el trail tiene algo que hacer, ese es el que manda.
    assert Reason.EARNINGS_DERISK not in out.suppressed


def test_earnings_lejanos_no_disparan_nada() -> None:
    out = evaluate_exits(trade(), ctx(today=dt.date(2026, 10, 30), earnings_date=EARNINGS))
    assert out.primary_cause is None


def test_el_conteo_es_por_sesiones_y_no_por_dias() -> None:
    # Con el miercoles 4 declarado feriado, el jueves 5 queda a una sola sesion y la
    # venta se adelanta: por eso el conteo necesita calendario.
    cal = SessionCalendar(holidays=frozenset({WEDNESDAY}))
    out = evaluate_exits(trade(), ctx(calendar=cal, today=dt.date(2026, 11, 2), earnings_date=EARNINGS))
    assert out.primary_cause is Reason.EARNINGS_DERISK


def test_sin_fecha_de_earnings_no_se_asume_que_no_hay_reporte() -> None:
    # D1: la ausencia de fecha es un dato faltante, no una buena noticia.
    out = evaluate_exits(trade(), ctx(earnings_date=None, earnings_known=False))
    assert Reason.EARNINGS_DERISK not in out.reasons


# --------------------------------------------------------------------------- #
# Estancamiento, tope de vida y trailing (reglas 5 a 8)
# --------------------------------------------------------------------------- #


def test_stall_a_quince_ruedas_sin_progreso() -> None:
    out = evaluate_exits(trade(days_open=15, max_progress_r=0.2), ctx())
    assert out.primary_cause is Reason.STALL_EXIT
    assert out.primary.exit_cause is ExitCause.STALL


def test_stall_no_dispara_si_algun_vez_alcanzo_la_meta() -> None:
    out = evaluate_exits(trade(days_open=15, max_progress_r=0.5), ctx())
    assert Reason.STALL_EXIT not in out.reasons


def test_stall_no_dispara_antes_de_la_ronda() -> None:
    out = evaluate_exits(trade(days_open=14, max_progress_r=0.2), ctx())
    assert out.primary_cause is None


def test_max_hold() -> None:
    out = evaluate_exits(trade(days_open=90), ctx())
    assert out.primary_cause is Reason.MAX_HOLD


def test_trail_sube_el_stop_del_runner() -> None:
    state = replace(
        trade(close=104.0, highest_n=115.0, stop=BREAKEVEN, broker_stop=BREAKEVEN),
        locked=True,
        tranches=RUNNER_ONLY,
    )
    out = evaluate_exits(state, ctx())
    # Maximo de 22 ruedas 115, ATR 3 -> Chandelier 106,0.
    assert out.primary_cause is Reason.RAISE_STOP
    assert out.stop_target == pytest.approx(106.0)


def test_la_histeresis_evita_editar_el_stop_todas_las_noches() -> None:
    state = replace(
        trade(close=103.0, highest_n=110.0, stop=BREAKEVEN, broker_stop=BREAKEVEN),
        locked=True,
        tranches=RUNNER_ONLY,
    )
    out = evaluate_exits(state, ctx())
    # Chandelier 101,0 contra stop vigente 101,0: mejora 0 < 0,75 x 3 = 2,25.
    assert out.primary_cause is None
    assert out.stop_target is None
    assert "menor que" in note(out, Reason.RAISE_STOP).detail


def test_trade_no_asegurado_no_tiene_trail() -> None:
    # Antes de vender el tramo A el stop es el inicial: moverlo seria inventar
    # proteccion que la estrategia no pidio.
    out = evaluate_exits(trade(highest_n=130.0), ctx())
    assert Reason.RAISE_STOP not in out.reasons


# --------------------------------------------------------------------------- #
# Tramo unico (seccion 5.6)
# --------------------------------------------------------------------------- #


def test_single_asegura_el_stop_en_breakeven_sin_vender() -> None:
    state = trade(single=True, tranches=(TrancheState("A", 1.0, 1.0),), volume=1.0, close=108.0)
    out = evaluate_exits(state, ctx())
    assert Reason.TP_HIT not in out.reasons
    assert out.should_exit is False
    assert out.primary_cause is Reason.RAISE_STOP
    assert out.stop_target == pytest.approx(BREAKEVEN)
    assert "SINGLE" in out.primary.detail
    nuevo = apply_exits(state, out)
    assert nuevo.locked is True
    assert nuevo.volume == 1.0


def test_single_sin_llegar_a_un_r_no_assegura_nada() -> None:
    state = trade(single=True, tranches=(TrancheState("A", 1.0, 1.0),), volume=1.0, close=104.0)
    assert evaluate_exits(state, ctx()).signals == ()


# --------------------------------------------------------------------------- #
# Concentracion y tesis (reglas 9 y 10)
# --------------------------------------------------------------------------- #


def test_cap_trim_vende_lo_que_calculo_el_planificador() -> None:
    trim = CapTrim(
        ticker="AMD",
        theme="semiconductores",
        volume=6.0,
        value_clp=6_000_000.0,
        theme_pct=38.0,
        theme_pct_after=30.0,
        detail="tema en 38,0 %",
    )
    out = evaluate_exits(trade(), ctx(cap_trim=trim))
    assert out.primary_cause is Reason.CAP_TRIM
    assert out.exit_volume == 6.0
    assert "confirmacion humana" in out.primary.detail


def test_cap_trim_no_toca_otros_tickers_del_mismo_tema() -> None:
    trim = CapTrim("AMD", "semis", 6.0, 1.0, 38.0, 30.0, "detalle")
    assert evaluate_exits(trade(ticker="NVDA"), ctx(cap_trim=trim)).primary_cause is None


def test_thesis_exit() -> None:
    out = evaluate_exits(trade(), ctx(thesis_broken=True))
    assert out.primary_cause is Reason.THESIS_EXIT
    assert out.exit_volume == VOLUME


def test_el_tope_no_pisaria_a_un_stop_breach() -> None:
    trim = CapTrim("AMD", "semis", 6.0, 1.0, 38.0, 30.0, "detalle")
    out = evaluate_exits(trade(close=91.0), ctx(cap_trim=trim, thesis_broken=True))
    assert out.primary_cause is Reason.STOP_BREACH


# --------------------------------------------------------------------------- #
# Reglas de oro y validaciones del estado
# --------------------------------------------------------------------------- #


def test_ninguna_accion_aumenta_el_riesgo_abierto() -> None:
    # Tras vender el tramo A el volumen pendiente baja: el Sell Stop del remanente
    # cubre menos acciones y ninguna orden de venta suma mas de lo que se tiene.
    antes = trade(close=108.0)
    out = evaluate_exits(antes, ctx())
    despues = apply_exits(antes, out)
    assert despues.open_volume < antes.open_volume
    assert out.exit_volume <= antes.open_volume
    assert despues.volume == sum(t.volume for t in despues.tranches)


def test_una_venta_total_deja_el_volumen_en_cero() -> None:
    antes = trade(close=91.0)
    despues = apply_exits(antes, evaluate_exits(antes, ctx()))
    assert despues.volume == 0.0
    assert despues.open_tranches == ()


def test_stop_mayor_o_igual_a_la_entrada_es_un_error_de_estado() -> None:
    with pytest.raises(ValueError, match="R es negativa"):
        trade(initial_stop=101.0, stop=101.0)


def test_el_riesgo_abierto_no_es_negativo() -> None:
    # Un stop por sobre el costo no resta riesgo: una posicion en ganancia con stop
    # alto tiene riesgo de reversa, no de perdida.
    antes = trade()
    despues = apply_exits(antes, evaluate_exits(antes, ctx()))
    assert despues.risk_per_share == pytest.approx(8.0)


def test_el_progreso_se_mide_con_cierres() -> None:
    assert aggregate_progress_r(current_r=-0.2, max_progress_r=0.4) == 0.4
    assert aggregate_progress_r(current_r=0.8, max_progress_r=0.4) == 0.8


# --------------------------------------------------------------------------- #
# Horizonte (decision D13)
# --------------------------------------------------------------------------- #


def test_horizonte_normal_hasta_45_dias() -> None:
    assert horizon_state(dt.date(2026, 11, 3), HORIZON) is HorizonState.NORMAL
    assert horizon_state(dt.date(2027, 2, 20), HORIZON) is HorizonState.TIGHTEN
    assert horizon_state(dt.date(2027, 3, 30), HORIZON) is HorizonState.NO_ENTRIES


def test_horizonte_estrecha_el_trail_del_runner() -> None:
    state = replace(
        trade(close=104.0, highest_n=108.0, stop=BREAKEVEN, broker_stop=BREAKEVEN),
        locked=True,
        tranches=RUNNER_ONLY,
    )
    # Trail normal: 108 - 9 = 99, bajo el breakeven: nada que hacer. Trail de
    # horizonte: 108 - 4,5 = 103,5, que supera la histeresis (2,25).
    assert evaluate_exits(state, ctx(today=TUESDAY)).primary_cause is None
    cerca = evaluate_exits(state, ctx(today=dt.date(2027, 2, 20)))
    assert cerca.primary_cause is Reason.HORIZON_TIGHTEN
    assert cerca.stop_target == pytest.approx(103.5)


def test_pasada_la_fecha_el_motor_no_falla_solo_deja_de_entrar() -> None:
    assert horizon_state(dt.date(2028, 1, 1), HORIZON) is HorizonState.NO_ENTRIES
