"""Secciones 16.2 y 16.3: stop, R, tamano, histéresis y Chandelier.

Las cifras vienen del spec y son reproducibles a mano, asi que tambien funcionan
como documentacion: si alguien cambia un parametro, el test dice que numero se
movio y por que.
"""

from __future__ import annotations

import pytest

from swing.core.models import Rejected
from swing.core.reasons import Reason
from swing.core.sizing import SizingCaps, size_trade, tp1_covers_costs
from swing.core.stops import (
    InitialStop,
    breakeven_price,
    chandelier_stop,
    entry_limit,
    initial_stop,
    risk_distance_with_buffer,
    should_raise_stop,
)
from swing.core.stops import (
    Rejected as StopRejected,
)

EQUITY = 40_000_000.0
FX = 984.0
RISK_PCT = 0.75
# 40.000.000 * 0,75 % = 300.000 CLP de riesgo por trade
RISK_CLP = 300_000.0

SPECS_2T = (("A", 0.50, 1.0), ("R", 0.50, None))
BIG_CAPS = SizingCaps(
    max_position_clp=EQUITY * 0.10,
    theme_headroom_clp=EQUITY * 0.30,
    cash_clp=EQUITY,
)


# --------------------------------------------------------------------------- #
# Limite de entrada (seccion 5.3)
# --------------------------------------------------------------------------- #


def test_limite_de_entrada_mas_una_fraccion_de_atr() -> None:
    # Cierre de la senal 99,25, ATR 3, entry_limit_atr 0,25 -> 100,00.
    assert entry_limit(99.25, 3.0, 0.25) == pytest.approx(100.00, abs=1e-9)


# --------------------------------------------------------------------------- #
# Stop inicial (seccion 5.4)
# --------------------------------------------------------------------------- #


def _stop(p: float, atr: float, low: float) -> InitialStop | StopRejected:
    return initial_stop(
        limit_price=p,
        atr=atr,
        swing_low=low,
        swing_low_buffer_atr=0.5,
        atr_band=(1.5, 3.0),
        max_stop_distance_pct=12.0,
    )


def test_stop_dentro_de_la_banda_no_se_mueve() -> None:
    # P 100, ATR 3, minimo de 10 ruedas 93,5 -> S_swing 92,0; distancia 8,0 en
    # la banda [4,5; 9,0] -> stop 92,0 y R 8,0 (8 %).
    out = _stop(100.0, 3.0, 93.5)
    assert isinstance(out, InitialStop)
    assert out.price == pytest.approx(92.0, abs=1e-9)
    assert out.risk_per_share == pytest.approx(8.0, abs=1e-9)
    assert out.stop_distance_pct == pytest.approx(8.0, abs=1e-9)
    assert out.swing_stop == pytest.approx(92.0, abs=1e-9)
    assert out.clamped is False


def test_stop_se_acota_por_la_banda_inferior() -> None:
    # S_swing 77 con ATR 6 -> distancia 23, se acota a 18 -> stop 82 (18 %), y
    # 18 % supera el tope de 12 %: rechazo.
    out = _stop(100.0, 6.0, 80.0)
    assert isinstance(out, Rejected)
    assert out.reason is Reason.STOP_TOO_WIDE


def test_stop_se_acota_por_la_banda_superior() -> None:
    # Minimo 85 -> S_swing 83,5, distancia 16,5 -> se acota a 9,0 -> stop 91,0
    # (9 %, valido). El stop se acerca al precio, nunca se aleja.
    out = _stop(100.0, 3.0, 85.0)
    assert isinstance(out, InitialStop)
    assert out.swing_stop == pytest.approx(83.5, abs=1e-9)
    assert out.price == pytest.approx(91.0, abs=1e-9)
    assert out.risk_per_share == pytest.approx(9.0, abs=1e-9)
    assert out.clamped is True


def test_el_tope_de_perdida_se_evalua_despues_de_acotar() -> None:
    # Un S_swing holgado que la banda deja dentro del 12 % debe entrar, aunque
    # el S_swing crudo fuera de mas del 12 %.
    out = _stop(100.0, 3.0, 85.0)
    assert isinstance(out, InitialStop)
    assert out.swing_stop < 100.0 * (1 - 0.12)
    assert out.stop_distance_pct <= 12.0


def test_atr_no_disponible_rechaza_en_vez_de_dar_un_numero() -> None:
    out = _stop(100.0, 0.0, 93.5)
    assert isinstance(out, Rejected)
    assert out.reason is Reason.INDICATOR_NOT_READY


# --------------------------------------------------------------------------- #
# Distancia de riesgo con colchon (seccion 4.5)
# --------------------------------------------------------------------------- #


def test_colchon_sin_stop_adjunto() -> None:
    # 8,0 + 1,0 x 3 = 11,0 (11 %).
    assert risk_distance_with_buffer(8.0, 3.0, 1.0, attach_stop_to_entry=False) == pytest.approx(11.0)


def test_sin_colchon_cuando_el_stop_viene_adjunto() -> None:
    assert risk_distance_with_buffer(8.0, 3.0, 1.0, attach_stop_to_entry=True) == pytest.approx(8.0)


# --------------------------------------------------------------------------- #
# Tamano (seccion 5.5) — cifras del test 16.2
# --------------------------------------------------------------------------- #


def _sized(distance: float, whole_shares: bool = False, caps: SizingCaps = BIG_CAPS) -> object:
    out = size_trade(
        equity_clp=EQUITY,
        fx=FX,
        risk_per_trade_pct=RISK_PCT,
        limit_price=100.0,
        risk_distance=distance,
        caps=caps,
        tranche_specs=SPECS_2T,
        whole_shares=whole_shares,
    )
    assert not isinstance(out, Rejected), out
    return out


def test_tamano_con_colchon() -> None:
    # distancia de riesgo 11,0 (11 %) -> notional 2.727.272,73 CLP (6,82 % del
    # equity) -> volumen 27,7161; tramo A 13,8580; runner 13,8581.
    out = _sized(11.0)
    assert out.notional_clp == pytest.approx(2_727_272.73, abs=0.01)
    assert out.equity_pct == pytest.approx(6.818, abs=0.001)
    assert out.volume == pytest.approx(27.7161, abs=1e-9)
    assert out.capped_by is None
    assert out.tranches[0].volume == pytest.approx(13.8580, abs=1e-9)
    assert out.tranches[1].volume == pytest.approx(13.8581, abs=1e-9)


def test_tamano_sin_colchon() -> None:
    # distancia 8,0 (8 %) -> notional 3.750.000 CLP (9,375 %, bajo el tope) ->
    # volumen 38,1097; tramo A 19,0548; runner 19,0549.
    out = _sized(8.0)
    assert out.notional_clp == pytest.approx(3_750_000.0, abs=0.01)
    assert out.equity_pct == pytest.approx(9.375, abs=0.001)
    assert out.volume == pytest.approx(38.1097, abs=1e-9)
    assert out.tranches[0].volume == pytest.approx(19.0548, abs=1e-9)
    assert out.tranches[1].volume == pytest.approx(19.0549, abs=1e-9)


def test_el_colchon_reduce_el_tamano() -> None:
    # Con el colchon la posicion es ~27 % mas chica: ese es el costo de la
    # primera sesion sin stop y la razon de la verificacion #3.
    con = _sized(11.0)
    sin = _sized(8.0)
    assert con.volume < sin.volume
    assert con.volume / sin.volume == pytest.approx(8.0 / 11.0, abs=1e-3)


def test_redondear_hacia_abajo_nunca_aumenta_el_riesgo() -> None:
    for distance in (8.0, 11.0, 5.5, 16.0):
        out = _sized(distance)
        assert out.risk_clp_actual <= out.risk_clp + 1e-9


def test_tope_de_posicion_gana_cuando_aprieta() -> None:
    tight = SizingCaps(
        max_position_clp=EQUITY * 0.05,
        theme_headroom_clp=EQUITY * 0.30,
        cash_clp=EQUITY,
    )
    out = _sized(8.0, caps=tight)
    assert out.capped_by == "max_position_pct"
    assert out.notional_clp == pytest.approx(2_000_000.0, abs=0.01)


def test_la_caja_aprieta_y_se_declara() -> None:
    short_cash = SizingCaps(
        max_position_clp=EQUITY * 0.10,
        theme_headroom_clp=EQUITY * 0.30,
        cash_clp=500_000.0,
    )
    out = _sized(8.0, caps=short_cash)
    assert out.capped_by == "cash"
    assert out.notional_clp == pytest.approx(500_000.0, abs=0.01)


def test_sin_caja_se_rechaza() -> None:
    nada = SizingCaps(max_position_clp=EQUITY, theme_headroom_clp=EQUITY, cash_clp=0.0)
    out = size_trade(
        equity_clp=EQUITY,
        fx=FX,
        risk_per_trade_pct=RISK_PCT,
        limit_price=100.0,
        risk_distance=8.0,
        caps=nada,
        tranche_specs=SPECS_2T,
        whole_shares=False,
    )
    assert isinstance(out, Rejected)
    assert out.reason is Reason.CASH_BUFFER


# --------------------------------------------------------------------------- #
# Acciones enteras (seccion 5.5, test 16.2 v3.1)
# --------------------------------------------------------------------------- #


def _whole(volume_before_floor: float) -> object:
    """Dimensiona para que el volumen crudo caiga en el valor pedido."""
    notional = volume_before_floor * 100.0 * FX
    caps = SizingCaps(max_position_clp=notional, theme_headroom_clp=notional, cash_clp=notional)
    out = size_trade(
        equity_clp=EQUITY,
        fx=FX,
        risk_per_trade_pct=RISK_PCT,
        limit_price=100.0,
        risk_distance=8.0,
        caps=caps,
        tranche_specs=SPECS_2T,
        whole_shares=True,
    )
    assert not isinstance(out, Rejected), out
    return out


def _whole_raw(volume_before_floor: float) -> object:
    """Igual que ``_whole`` pero sin afirmar el resultado: hay casos de rechazo."""
    notional = volume_before_floor * 100.0 * FX
    caps = SizingCaps(max_position_clp=notional, theme_headroom_clp=notional, cash_clp=notional)
    return size_trade(
        equity_clp=EQUITY,
        fx=FX,
        risk_per_trade_pct=RISK_PCT,
        limit_price=100.0,
        risk_distance=8.0,
        caps=caps,
        tranche_specs=SPECS_2T,
        whole_shares=True,
    )


def test_acciones_enteras_reparto_impar() -> None:
    out = _whole(5.3)
    assert out.volume == 5.0
    assert out.tranches[0].volume == 2.0
    assert out.tranches[1].volume == 3.0
    assert out.single_tranche is False


def test_acciones_enteras_reparto_par() -> None:
    out = _whole(4.0)
    assert out.tranches[0].volume == 2.0
    assert out.tranches[1].volume == 2.0


def test_una_sola_accion_activa_tramo_unico() -> None:
    out = _whole(1.7)
    assert out.volume == 1.0
    assert out.single_tranche is True
    assert len(out.tranches) == 1


def test_menos_de_una_accion_se_rechaza() -> None:
    out = _whole_raw(0.6)
    assert isinstance(out, Rejected)
    assert out.reason is Reason.VOLUME_TOO_SMALL


def test_los_tramos_suman_exactamente_el_volumen() -> None:
    for raw in (5.3, 4.0, 27.7161, 38.1097):
        out = _whole(raw) if raw in (5.3, 4.0) else _sized(8.0)
        assert sum(t.volume for t in out.tranches) == pytest.approx(out.volume, abs=1e-9)


# --------------------------------------------------------------------------- #
# Meta A contra costos (seccion 5.6)
# --------------------------------------------------------------------------- #


def test_meta_a_supera_tres_veces_el_costo_con_los_parametros_por_defecto() -> None:
    # Meta +1 R = 8 % contra un round-trip de 1,0 %: razon 8, umbral 3.
    assert tp1_covers_costs(8.0, 100.0, 1.0, 1.0, 3.0) is True


def test_meta_a_rechazada_cuando_el_stop_se_acota_demasiado() -> None:
    # Con la banda inferior en un ticker volatil la R cae a 1,5 ATR: si el ATR es
    # 2,0 % del precio, la R es 3,0 % y la meta queda en 3,0 %, que no supera
    # 3 x 1,0 %. Ahi el setup no vale la pena y se rechaza.
    atr = 2.0
    risk = 1.5 * atr
    assert tp1_covers_costs(risk, 100.0, 1.0, 1.0, 3.0) is False


def test_sin_costo_de_conversion_el_criterio_no_muerde() -> None:
    assert tp1_covers_costs(1.0, 100.0, 1.0, 0.0, 3.0) is True


# --------------------------------------------------------------------------- #
# Breakeven, histéresis y Chandelier (seccion 16.3)
# --------------------------------------------------------------------------- #


def test_breakeven_del_runner() -> None:
    # 100 x 1,01 = 101,00.
    assert breakeven_price(100.0, 1.0) == pytest.approx(101.0, abs=1e-9)


def test_histeresis_pide_subir_el_stop() -> None:
    # Stop vigente 101,0, ATR 3, umbral 0,75 x 3 = 2,25. Candidato 103,5 (+2,5).
    assert should_raise_stop(101.0, 103.5, 3.0, 0.75) is True


def test_histeresis_no_pide_editar_el_stop_a_futile() -> None:
    # Candidato 102,9 (+1,9 < 2,25): ninguna accion, para no editar todas las noches.
    assert should_raise_stop(101.0, 102.9, 3.0, 0.75) is False


def test_el_trail_nunca_baja_el_stop() -> None:
    # Si el maximo retrocede, el Chandelier candidate queda bajo el vigente y la
    # histeresis lo rechaza.
    stop_vigente = 106.0
    candidate = chandelier_stop(112.0, 3.0, 3.0, days_open=5, tighten_after_days=60, tightened_atr_mult=1.5)
    assert candidate == pytest.approx(103.0, abs=1e-9)
    assert should_raise_stop(stop_vigente, candidate, 3.0, 0.75) is False


def test_chandelier() -> None:
    # Maximo de 22 ruedas 115, ATR 3 -> 106,0.
    assert chandelier_stop(115.0, 3.0, 3.0, 5, 60, 1.5) == pytest.approx(106.0, abs=1e-9)


def test_el_trail_se_estrecha_tras_el_plazo() -> None:
    # Pasados 60 dias el multiplicador baja de 3,0 a 1,5: el stop queda mas alto.
    temprano = chandelier_stop(115.0, 3.0, 3.0, days_open=10, tighten_after_days=60, tightened_atr_mult=1.5)
    tarde = chandelier_stop(115.0, 3.0, 3.0, days_open=90, tighten_after_days=60, tightened_atr_mult=1.5)
    assert temprano == pytest.approx(106.0, abs=1e-9)
    assert tarde == pytest.approx(110.5, abs=1e-9)
    assert tarde > temprano
