"""Seccion 16.1: indicadores calculables a mano, tolerancia 1e-6.

Las series son sinteticas y las respuestas esperadas son fracciones exactas
divididas a mano, no numeros copiados de una libreria: asi el test verifica la
formula del spec, no la implementacion de pandas.
"""

from __future__ import annotations

import math

import pandas as pd
import pytest

from swing.core.indicators import (
    atr,
    ema,
    heikin_ashi_state,
    highest,
    lowest,
    rsi,
    sma,
    smoothed_heikin_ashi,
    true_range,
    wilder_smooth,
)

TOL = 1e-6


def _frame(values: list[float]) -> pd.Series:
    return pd.Series(values, index=pd.RangeIndex(len(values)), dtype="float64")


# --------------------------------------------------------------------------- #
# True Range y ATR de Wilder
# --------------------------------------------------------------------------- #

# high/low suben en escalones de 1 y el cierre queda a mitad de rango, asi que
# cada TR posterior es el mayor de los tres candidatos.
STEP_HL = [10.0, 11.0, 12.0, 13.0, 14.0]
STEP_LL = [9.0, 10.0, 11.0, 12.0, 13.0]
STEP_CLOSE = [9.5, 10.5, 11.5, 12.5, 13.5]
# TR = [1, 1.5, 1.5, 1.5, 1.5]
STEP_TR = [1.0, 1.5, 1.5, 1.5, 1.5]


def test_true_range_usa_la_primera_barra_como_h_menos_l() -> None:
    tr = true_range(_frame(STEP_HL), _frame(STEP_LL), _frame(STEP_CLOSE))
    assert list(tr) == pytest.approx(STEP_TR, abs=TOL)


def test_true_range_toma_el_maximo_entre_gap_hacia_arriba_y_hacia_abajo() -> None:
    # Barra que abre muy por debajo del cierre previo: |H - C_prev| gana.
    tr = true_range(_frame([10.0, 20.0]), _frame([9.0, 12.0]), _frame([9.5, 15.0]))
    assert list(tr) == pytest.approx([1.0, 10.5], abs=TOL)


def test_wilder_smooth_semilla_y_recursion() -> None:
    # n = 3 sobre [1, 1.5, 1.5, 1.5, 1.5]:
    #   semilla = (1 + 1.5 + 1.5) / 3          = 4/3
    #   X3 = (4/3 * 2 + 1.5) / 3               = 25/18
    #   X4 = (25/18 * 2 + 1.5) / 3             = 77/54
    out = wilder_smooth(_frame(STEP_TR), 3)
    assert math.isnan(out[0])
    assert math.isnan(out[1])
    assert out[2] == pytest.approx(4.0 / 3.0, abs=TOL)
    assert out[3] == pytest.approx(25.0 / 18.0, abs=TOL)
    assert out[4] == pytest.approx(77.0 / 54.0, abs=TOL)


def test_atr_de_wilder_con_periodo_3() -> None:
    out = atr(_frame(STEP_HL), _frame(STEP_LL), _frame(STEP_CLOSE), period=3)
    expected = [math.nan, math.nan, 4.0 / 3.0, 25.0 / 18.0, 77.0 / 54.0]
    for got, want in zip(out, expected, strict=True):
        if math.isnan(want):
            assert math.isnan(got)
        else:
            assert got == pytest.approx(want, abs=TOL)


def _ramp(n: int) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Rampa continua de ``n`` barras: TR = 10 en todas.

    Consecutive, el maximo de los tres candidatos de TR siempre es ``H - L``, asi
    que la semilla del ATR es exactamente 10 y el test no depende de sumar una
    lista a mano. No sirve para las pruebas de periodo 3, que necesitan TR
    variables, por eso alli se usa STEP_*.
    """
    highs = [100.0 + i for i in range(n)]
    lows = [90.0 + i for i in range(n)]
    closes = [95.0 + i for i in range(n)]
    return _frame(highs), _frame(lows), _frame(closes)


def test_atr_de_wilder_con_periodo_14_no_inventa_valores() -> None:
    # Con 13 barras no hay semilla de 14: todo NaN. Un ATR immature en vez de un
    # numero es justamente lo que evita una entrada con sizing erroneo.
    high, low, close = _ramp(13)
    out = atr(high, low, close, period=14)
    assert len(out) == 13
    assert out.isna().all()


def test_atr_de_wilder_aparece_en_la_primera_semilla() -> None:
    # Con exactamente 14 barras hay un unico valor, en la ultima posicion.
    high, low, close = _ramp(14)
    out = atr(high, low, close, period=14)
    assert out.iloc[:13].isna().all()
    assert out.iloc[13] == pytest.approx(10.0, abs=TOL)


def test_atr_de_14_sobre_una_rampa_no_deriva() -> None:
    # Si el suavizado esta mal, una serie con TR constante no puede quedar en 10.
    high, low, close = _ramp(30)
    out = atr(high, low, close, period=14)
    assert out.iloc[13:].to_list() == pytest.approx([10.0] * 17, abs=TOL)


def test_wilder_smooth_rechaza_semilla_con_nan() -> None:
    with pytest.raises(ValueError, match="NaN"):
        wilder_smooth(_frame([math.nan, 1.0, 2.0, 3.0]), 3)


def test_wilder_smooth_rechaza_periodo_invalido() -> None:
    with pytest.raises(ValueError, match="period"):
        wilder_smooth(_frame([1.0, 2.0]), 0)


def test_indicadores_rechazan_series_desalineadas() -> None:
    a = pd.Series([1.0, 2.0, 3.0], index=[0, 1, 2])
    b = pd.Series([1.0, 2.0, 3.0], index=[1, 2, 3])
    with pytest.raises(ValueError, match="indice"):
        true_range(a, b, a)


# --------------------------------------------------------------------------- #
# SMA
# --------------------------------------------------------------------------- #


def test_sma_con_vector_conocido() -> None:
    out = sma(_frame([1.0, 2.0, 3.0, 4.0, 5.0]), 3)
    assert math.isnan(out[0])
    assert math.isnan(out[1])
    assert list(out[2:]) == pytest.approx([2.0, 3.0, 4.0], abs=TOL)


def test_sma_de_50_y_200_son_na_n_hasta_tener_historia() -> None:
    close = _frame([100.0] * 199)
    assert sma(close, 200).isna().all()
    assert sma(close, 50).notna().sum() == 150


# --------------------------------------------------------------------------- #
# RSI de Wilder
# --------------------------------------------------------------------------- #


# Cierra 10,11,12,13,14,13,12,11,10 con periodo 3. La primera barra no aporta
# (el RSI necesita dos cierres), asi que la semilla cae en el indice 3 y el
# indice 4 sigue sin perdidas, todavia RSI 100:
#   idx:      0    1    2    3      4      5       6       7        8
#   avg_gain: -    -    -    1      1      2/3     4/9     8/27     16/81
#   avg_loss: -    -    -    0      0      1/3     5/9     19/27    65/81
#   RSI = 100 - 100 * avg_loss / (avg_gain + avg_loss):
#             -    -    -    100    100    200/3   400/9   800/27    1600/81
RSI_CLOSE = [10.0, 11.0, 12.0, 13.0, 14.0, 13.0, 12.0, 11.0, 10.0]
RSI_EXPECTED = [
    math.nan,
    math.nan,
    math.nan,
    100.0,
    100.0,
    200.0 / 3.0,
    400.0 / 9.0,
    800.0 / 27.0,
    1600.0 / 81.0,
]


def test_rsi_de_wilder_con_vector_conocido() -> None:
    out = rsi(_frame(RSI_CLOSE), period=3)
    for got, want in zip(out, RSI_EXPECTED, strict=True):
        if math.isnan(want):
            assert math.isnan(got)
        else:
            assert got == pytest.approx(want, abs=1e-5)


def test_rsi_queda_en_50_en_una_serie_plana() -> None:
    out = rsi(_frame([50.0] * 6), period=3)
    assert out[3] == pytest.approx(50.0, abs=TOL)


def test_rsi_de_14_para_el_setup_pullback_acepta_el_rango_38_55() -> None:
    # El setup exige RSI14 en [38, 55]: una serie que solo sube tiene RSI 100 y
    # debe quedar fuera del rango, no recortado al borde.
    up = rsi(_frame([10.0 + i for i in range(40)]), period=14)
    assert up.iloc[-1] > 55.0


# --------------------------------------------------------------------------- #
# Extremos rodantes (puerta 3 de la seccion 5.2)
# --------------------------------------------------------------------------- #


def test_maximo_y_minimo_de_52_semanas() -> None:
    close = _frame([10.0, 12.0, 8.0, 15.0, 9.0, 11.0])
    assert highest(close, 3).iloc[2] == pytest.approx(12.0, abs=TOL)
    assert lowest(close, 3).iloc[2] == pytest.approx(8.0, abs=TOL)
    assert highest(close, 6).iloc[5] == pytest.approx(15.0, abs=TOL)
    assert lowest(close, 6).iloc[5] == pytest.approx(8.0, abs=TOL)


def test_extremos_devuelven_nan_sin_historia_suficiente() -> None:
    close = _frame([1.0, 2.0])
    assert highest(close, 3).isna().all()
    assert lowest(close, 3).isna().all()


# --------------------------------------------------------------------------- #
# Heikin-Ashi suavizado (indicador "Smoothed HA candles")
# --------------------------------------------------------------------------- #


def test_ema_semilla_en_la_primera_observacion_y_alpha_2_sobre_n_mas_1() -> None:
    out = ema(_frame([1.0, 2.0, 3.0]), period=2)
    # alpha = 2/3: e0=1; e1=(2/3)*2+(1/3)*1; e2=(2/3)*3+(1/3)*e1.
    assert list(out) == pytest.approx([1.0, 5.0 / 3.0, 23.0 / 9.0], abs=TOL)


def _rising_bars(count: int) -> tuple[pd.Series, pd.Series, pd.Series, pd.Series]:
    opens = _frame([float(i) for i in range(count)])
    closes = _frame([i + 0.8 for i in range(count)])
    highs = _frame([i + 1.0 for i in range(count)])
    lows = _frame([i - 0.5 for i in range(count)])
    return opens, highs, lows, closes


def test_sha_sube_en_una_serie_que_solo_sube() -> None:
    opens, highs, lows, closes = _rising_bars(40)
    frame = smoothed_heikin_ashi(opens, highs, lows, closes)
    assert list(frame.columns) == ["open", "high", "low", "close"]
    state = heikin_ashi_state(frame)
    assert state is not None
    trend, sessions = state
    assert trend == "alcista"
    # Los primeros suavizados arrancan cerca de cero y tardan unas velas en girar.
    assert 0 <= sessions < len(frame)


def test_sha_sin_giro_cuenta_todas_las_sesiones() -> None:
    frame = pd.DataFrame(
        {"open": [1.0, 1.0, 1.0], "high": [2.0, 2.0, 2.0], "low": [0.0, 0.0, 0.0], "close": [2.0, 2.0, 2.0]}
    )
    assert heikin_ashi_state(frame) == ("alcista", 3)


def test_sha_estado_marca_el_giro_y_las_sesiones_desde_el() -> None:
    # Cierres por debajo de la apertura en la ultima vela: giró a bajista ahi mismo.
    frame = pd.DataFrame(
        {
            "open": [1.0, 1.0, 2.0],
            "high": [2.0, 2.0, 2.0],
            "low": [0.5, 0.5, 0.5],
            "close": [2.0, 1.5, 1.0],
        }
    )
    state = heikin_ashi_state(frame)
    assert state == ("bajista", 0)


def test_sha_sin_velas_no_tiene_estado() -> None:
    empty = pd.DataFrame({"open": [], "high": [], "low": [], "close": []})
    assert heikin_ashi_state(empty) is None
