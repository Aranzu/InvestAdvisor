"""Seccion 5.2: las seis puertas, y seccion 2: los tres regimenes.

Lo que mas importa aqui no es que cada puerta rechace, sino que el rechazo
declare **por que** y que se acumulen. Un "NO_SETUP" unico no dice si el problema
son los earnings o la tendencia, y el usuario no puede corregir nada sin saberlo.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from swing.adapters.config_loader import load_strategy
from swing.core.models import RegimeState
from swing.core.reasons import Reason
from swing.core.regime import effective_risk_pct, evaluate_regime, gate_entries
from swing.core.screening import (
    GateInputs,
    GateThresholds,
    Setup,
    check_gates,
    check_setup,
)

CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"

TH = GateThresholds(
    min_price=10.0,
    rs_min_percentile=70.0,
    min_above_52w_low=1.30,
    min_vs_52w_high=0.75,
    min_avg_volume=1_000_000.0,
    min_universe=40,
    earnings_entry_blackout_sessions=20,
)
UNIVERSE = 70


def ok(**over: object) -> GateInputs:
    """Un ticker que pasa todo, para que cada test rompa una sola cosa."""
    base: dict[str, object] = {
        "close": 200.0,
        "sma200": 180.0,
        "sma200_slope_20": 1.0,
        "sma50": 170.0,
        "rs_percentile": 85.0,
        "high_52w": 210.0,
        "low_52w": 100.0,
        "avg_dollar_volume": 50_000_000.0,
        "days_to_earnings": 90,
        "earnings_source_available": True,
    }
    return GateInputs(**{**base, **over})  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Puertas
# --------------------------------------------------------------------------- #


def test_un_ticker_sano_pasa_las_seis_puertas() -> None:
    assert check_gates(ok(), TH, UNIVERSE).passes is True


def test_g1_bajo_la_sma200() -> None:
    res = check_gates(ok(sma200=220.0), TH, UNIVERSE)
    assert res.passes is False
    assert Reason.BELOW_SMA200 in {r.reason for r in res.rejections}


def test_g1_sma200_bajando() -> None:
    res = check_gates(ok(sma200_slope_20=-0.5), TH, UNIVERSE)
    assert Reason.SMA200_FALLING in {r.reason for r in res.rejections}


def test_g2_bajo_la_sma50() -> None:
    res = check_gates(ok(sma50=210.0), TH, UNIVERSE)
    assert Reason.BELOW_SMA50 in {r.reason for r in res.rejections}


def test_g2_stack_invertido() -> None:
    # Stack invertido con el precio arriba de las dos: la trampa clasica de un
    # rebote dentro de una tendencia a la baja.
    res = check_gates(ok(sma200=180.0, sma50=185.0), TH, UNIVERSE)
    assert Reason.STACK_INVERTED in {r.reason for r in res.rejections}


def test_g3_muy_junto_al_minimo_de_52_semanas() -> None:
    # Minimo 100: 1,30 x son 130, asi que 125 no entra.
    res = check_gates(ok(close=125.0, sma50=115.0, sma200=110.0), TH, UNIVERSE)
    assert Reason.BELOW_52W_LOW_MULTIPLE in {r.reason for r in res.rejections}


def test_g3_justo_sobre_el_multiplo_del_minimo_pasa() -> None:
    # 130 es exactamente 1,30 x y el tope es inclusivo.
    res = check_gates(ok(close=130.0, sma50=120.0, sma200=115.0), TH, UNIVERSE)
    assert Reason.BELOW_52W_LOW_MULTIPLE not in {r.reason for r in res.rejections}


def test_g3_a_un_paso_del_maximo() -> None:
    # Maximo 210: 0,75 x son 157,50, asi que 155 queda fuera.
    res = check_gates(ok(close=155.0, sma50=145.0, sma200=140.0), TH, UNIVERSE)
    assert Reason.BELOW_52W_HIGH_FRACTION in {r.reason for r in res.rejections}


def test_g3_rango_invalido_no_se_trata_como_que_pasa() -> None:
    res = check_gates(ok(low_52w=0.0), TH, UNIVERSE)
    assert Reason.INDICATOR_NOT_READY in {r.reason for r in res.rejections}


def test_g4_fuerza_relativa_baja() -> None:
    res = check_gates(ok(rs_percentile=45.0), TH, UNIVERSE)
    assert Reason.LOW_RS_PERCENTILE in {r.reason for r in res.rejections}


def test_g4_universo_pequeno_no_es_universo() -> None:
    # Decision D2: el percentil no significa nada con 12 nombres.
    res = check_gates(ok(), TH, universe_size=12)
    assert Reason.UNIVERSE_TOO_SMALL in {r.reason for r in res.rejections}


def test_g5_ilquido() -> None:
    res = check_gates(ok(avg_dollar_volume=250_000.0), TH, UNIVERSE)
    assert Reason.ILLIQUID in {r.reason for r in res.rejections}


def test_g6_earnings_dentro_del_blackout() -> None:
    res = check_gates(ok(days_to_earnings=5), TH, UNIVERSE)
    assert Reason.EARNINGS_NEAR in {r.reason for r in res.rejections}


def test_g6_earnings_justo_fuera_del_blackout_pasa() -> None:
    # El limite es "dentro de 20 sesiones"; a 21 todavia entra.
    assert check_gates(ok(days_to_earnings=21), TH, UNIVERSE).passes is True


def test_g6_dias_negativos_se_rechazan_por_rancios() -> None:
    # Un reporte en el pasado es una fecha rancia: fail-closed, no "lejos".
    res = check_gates(ok(days_to_earnings=-3), TH, UNIVERSE)
    assert Reason.EARNINGS_NEAR in {r.reason for r in res.rejections}


def test_g6_sin_fuente_de_earnings_se_rechaza_en_vez_de_asumir() -> None:
    # Decision D1: fail-closed. Sin fuente no hay entrada, aunque los dias a
    # earnings digan 90.
    res = check_gates(ok(earnings_source_available=False), TH, UNIVERSE)
    assert Reason.DATA_GAP in {r.reason for r in res.rejections}


def test_g6_sin_calendario_tambien_se_rechaza() -> None:
    res = check_gates(ok(days_to_earnings=None), TH, UNIVERSE)
    assert Reason.INDICATOR_NOT_READY in {r.reason for r in res.rejections}


def test_precio_bajo_el_piso() -> None:
    res = check_gates(ok(close=6.0, sma50=5.0, sma200=4.0), TH, UNIVERSE)
    assert Reason.PRICE_BELOW_FLOOR in {r.reason for r in res.rejections}


def test_se_reportan_todas_las_razones_no_solo_la_primera() -> None:
    # Bajo la SMA200, stack invertido y con earnings cerca: el informe tiene que
    # decir las tres, no una.
    res = check_gates(
        ok(sma200=260.0, sma50=265.0, days_to_earnings=3),
        TH,
        UNIVERSE,
    )
    reasons = {r.reason for r in res.rejections}
    assert {Reason.BELOW_SMA200, Reason.STACK_INVERTED, Reason.EARNINGS_NEAR} <= reasons
    assert len(res.rejections) >= 3


def test_los_umbrales_salen_de_la_config_de_entrega() -> None:
    # Si el nombre o el valor se desincronizan, la config deja de mandar y el
    # motor empieza a decidir con numeros que nadie reviso.
    strategy = load_strategy(CONFIG_DIR / "strategy.yaml")
    th = GateThresholds.from_config(strategy.gates)
    assert th.min_universe == strategy.gates.min_universe == 40
    assert th.rs_min_percentile == strategy.gates.rs_min_percentile == 70.0
    assert th.min_above_52w_low == pytest.approx(1.30)
    assert th.min_vs_52w_high == pytest.approx(0.75)
    assert th.min_price == 10.0


# --------------------------------------------------------------------------- #
# Setup P1 + sesion 2 (seccion 5.3)
# --------------------------------------------------------------------------- #


def test_setup_confirmado_pasa() -> None:
    assert check_setup(Setup(200.0, "2026-01-05", 1, 1.8), 1.2) is None


def test_sin_sesion_2_no_hay_setup() -> None:
    out = check_setup(Setup(200.0, "2026-01-05", 0, 1.8), 1.2)
    assert out is not None and out.reason is Reason.NO_SETUP


def test_volumen_de_confirmacion_insuficiente() -> None:
    out = check_setup(Setup(200.0, "2026-01-05", 1, 0.9), 1.2)
    assert out is not None and out.reason is Reason.VOLUME_TOO_SMALL


def test_volumen_justo_en_el_minimo_pasa() -> None:
    assert check_setup(Setup(200.0, "2026-01-05", 1, 1.2), 1.2) is None


# --------------------------------------------------------------------------- #
# Regime (seccion 2)
# --------------------------------------------------------------------------- #


def test_regimen_favorable() -> None:
    assert evaluate_regime(500.0, 450.0, 14.0, 30.0) is RegimeState.FULL


def test_spy_bajo_su_sma200_cierra_la_puerta() -> None:
    assert evaluate_regime(400.0, 450.0, 14.0, 30.0) is RegimeState.NO_ENTRIES


def test_vix_en_el_maximo_cierra_la_puerta() -> None:
    assert evaluate_regime(500.0, 450.0, 30.0, 30.0) is RegimeState.NO_ENTRIES


def test_vix_alto_pero_no_extremo_halva_el_riesgo() -> None:
    # 26 sobre un umbral de 30 es cautela, no veto.
    assert evaluate_regime(500.0, 450.0, 26.0, 30.0) is RegimeState.HALF


def test_vix_ausente_no_se_trata_como_vix_bajo() -> None:
    # El dato que falta no es "todo bien": es no saber, y no saber cierra.
    assert evaluate_regime(500.0, 450.0, 0.0, 30.0) is RegimeState.NO_ENTRIES


def test_cautela_halva_el_riesgo_por_trade() -> None:
    assert effective_risk_pct(0.75, RegimeState.HALF) == pytest.approx(0.375)
    assert effective_risk_pct(0.75, RegimeState.FULL) == pytest.approx(0.75)


def test_solo_el_veto_total_cierra_la_puerta() -> None:
    assert gate_entries(RegimeState.NO_ENTRIES) is not None
    assert gate_entries(RegimeState.HALF) is None
    assert gate_entries(RegimeState.FULL) is None
