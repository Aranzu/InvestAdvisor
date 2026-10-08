"""Calidad de datos: las tres razones por las que el motor no opera (D3, D6, D9).

Cada test de este archivo es un ``DATA_GAP``, ``STALE_DATA`` o ``INDICATOR_NOT_READY``
que evita un plan con precios viejos. Son los tests mas baratos del proyecto: una red
caida, una vela a medio formar o un CSV truncado se detectan aca y no en una posicion.
"""

from __future__ import annotations

import datetime as dt

import pytest

from swing.core.calendar import SessionCalendar
from swing.core.dataquality import (
    MIN_BARS_FOR_INDICATORS,
    check_freshness,
    check_series,
    detect_split_ratio,
    find_unadjusted_jumps,
    gap_pct,
    rebase_to_post_split,
    rebased_series,
    sessions_to_earnings,
)
from swing.core.market import Bar, PriceSeries
from swing.core.reasons import Reason

CAL = SessionCalendar.us()

#: Jueves 2026-11-05 con su sesion previa (miercoles 4) y el dia habil siguiente.
WEDNESDAY = dt.date(2026, 11, 4)
THURSDAY = dt.date(2026, 11, 5)
FRIDAY = dt.date(2026, 11, 6)


def flat_series(ticker: str = "AMD", days: int = 60, last: dt.date = WEDNESDAY) -> PriceSeries:
    """Serie de ``days`` sesiones que termina en ``last``, con alto 101 y bajo 99."""
    sessions = CAL.sessions_in_range(CAL.subtract_sessions(last, days), last)
    bars = tuple(
        Bar(date=day, open=100.0, high=101.0, low=99.0, close=100.5, volume=5_000_000.0)
        for day in sessions
    )
    return PriceSeries(ticker, bars)


# --------------------------------------------------------------------------- #
# Frescura del reloj (D3)
# --------------------------------------------------------------------------- #


def close_ny(day: dt.date = WEDNESDAY) -> dt.datetime:
    """16:00 de New York, ya resuelta en zona (el horario de verano no es codigo)."""
    return dt.datetime.combine(day, dt.time(16, 0), tzinfo=dt.timezone(dt.timedelta(hours=-4)))


def test_dentro_del_margen_no_hay_rechazo() -> None:
    ahora = close_ny() + dt.timedelta(minutes=95)
    assert check_freshness(ahora, close_ny(), 90) is None


def test_dentro_del_margno_se_usa_la_vela_de_hoy() -> None:
    ahora = close_ny() + dt.timedelta(minutes=89, seconds=59)
    rechazo = check_freshness(ahora, close_ny(), 90)
    assert rechazo is not None
    assert rechazo.reason is Reason.DATA_GAP
    assert "17:30" in rechazo.detail


def test_el_margen_cuenta_desde_el_cierre_y_no_desde_la_medianoche() -> None:
    # Medianoche en New York con la vela de ayerformed: hay que rechazar igual.
    medianoche = dt.datetime.combine(WEDNESDAY, dt.time(0, 30), tzinfo=dt.timezone(dt.timedelta(hours=-4)))
    assert check_freshness(medianoche, close_ny(), 90) is not None


def test_el_rechazo_dice_a_que_hora_se_puede_reintentar() -> None:
    ahora = close_ny() + dt.timedelta(minutes=10)
    detalle = check_freshness(ahora, close_ny(), 90).detail  # type: ignore[union-attr]
    assert "17:30" in detalle


def test_un_margen_negativo_es_un_error_de_configuracion() -> None:
    with pytest.raises(ValueError, match="min_minutes_after_close"):
        check_freshness(close_ny(), close_ny(), -1)


# --------------------------------------------------------------------------- #
# Antiguedad y huecos
# --------------------------------------------------------------------------- #


def test_una_serie_al_dia_no_tiene_rechazos() -> None:
    assert check_series(flat_series(), CAL, WEDNESDAY).ok


def test_sin_velas_es_data_gap() -> None:
    reporte = check_series(PriceSeries.empty("AMD"), CAL, WEDNESDAY)
    assert reporte.reasons == (Reason.DATA_GAP,)
    assert "no tiene velas" in reporte.rejections[0].detail


def test_una_vela_antes_de_la_ultima_sesion_es_dato_obsoleto() -> None:
    # La red se cae el jueves: la ultima vela es del miercoles.
    reporte = check_series(flat_series(last=WEDNESDAY), CAL, THURSDAY)
    assert Reason.STALE_DATA in reporte.reasons
    assert "2026-11-05" in next(r.detail for r in reporte.rejections if r.reason is Reason.STALE_DATA)


def test_en_feriado_la_ultima_vela_no_es_obsoleta() -> None:
    # El 26 de noviembre de 2026 es Thanksgiving: el mercado no abrio, asi que la
    # ultima vela correcta es la del miercoles 25.
    ayer_del_feriado = dt.date(2026, 11, 25)
    reporte = check_series(flat_series(last=ayer_del_feriado), CAL, dt.date(2026, 11, 26))
    assert Reason.STALE_DATA not in reporte.reasons


def test_una_serie_con_sesiones_faltantes_es_data_gap() -> None:
    completa = flat_series(days=40)
    sin_la_sesion_del_miercoles = PriceSeries(
        "AMD", tuple(b for b in completa.bars if b.date != WEDNESDAY)
    )
    reporte = check_series(sin_la_sesion_del_miercoles, CAL, WEDNESDAY)
    assert Reason.DATA_GAP in reporte.reasons
    detalle = next(r.detail for r in reporte.rejections if r.reason is Reason.DATA_GAP)
    assert "2026-11-04" in detalle


def test_un_hueco_lejano_no_es_fallo_de_hoy() -> None:
    # El proveedor puede no tener el historico de un ticker que hace anos no cotizaba.
    corta = PriceSeries("AMD", flat_series(days=40).bars[-5:])
    assert check_series(corta, CAL, WEDNESDAY).ok


def test_series_corta_para_los_indicadores() -> None:
    reporte = check_series(flat_series(days=40), CAL, WEDNESDAY, min_bars=MIN_BARS_FOR_INDICATORS)
    assert reporte.reasons == (Reason.INDICATOR_NOT_READY,)
    assert str(MIN_BARS_FOR_INDICATORS) in reporte.rejections[0].detail


def test_todas_las_razones_se_reportan() -> None:
    # Obsoleto y con huecos a la vez: el informe tiene que decir las dos.
    completa = flat_series(days=60, last=WEDNESDAY)
    incompleta = PriceSeries("AMD", tuple(b for b in completa.bars if b.date != FRIDAY))
    reporte = check_series(incompleta, CAL, THURSDAY)
    assert set(reporte.reasons) == {Reason.STALE_DATA, Reason.DATA_GAP}


# --------------------------------------------------------------------------- #
# Splits en datos sin ajustar (D6)
# --------------------------------------------------------------------------- #


def test_un_salto_del_30_por_ciento_no_es_un_salto() -> None:
    assert detect_split_ratio(100.0, 70.0) is None
    assert gap_pct(100.0, 90.0) == pytest.approx(10.0)


def test_un_split_cuatro_a_uno_se_detecta() -> None:
    assert detect_split_ratio(100.0, 25.0) == pytest.approx(4.0)


def test_una_caida_real_no_se_confunde_con_un_split() -> None:
    assert detect_split_ratio(100.0, 25.0) is not None
    assert detect_split_ratio(100.0, 24.0) == pytest.approx(100.0 / 24.0)


def test_un_salto_se_reporta_con_su_fecha() -> None:
    serie = PriceSeries(
        "XYZ",
        (
            Bar(date=WEDNESDAY, open=100.0, high=101.0, low=99.0, close=100.0, volume=1_000.0),
            Bar(date=THURSDAY, open=25.0, high=26.0, low=24.0, close=25.0, volume=4_000.0),
        ),
    )
    saltos = find_unadjusted_jumps(serie)
    assert saltos == ((THURSDAY, pytest.approx(4.0)),)


def test_una_serie_normal_no_tiene_saltos() -> None:
    assert find_unadjusted_jumps(flat_series(days=20)) == ()


def test_reescalar_el_historico_deja_la_serie_comparable() -> None:
    # Con el rebasing, el dia del split ya no parece una catastrophe de 75 %, y el ATR
    # deja de ver un salto que no existio.
    serie = PriceSeries(
        "XYZ",
        (
            Bar(date=WEDNESDAY, open=100.0, high=101.0, low=99.0, close=100.0, volume=1_000.0),
            Bar(date=THURSDAY, open=25.0, high=26.0, low=24.0, close=25.0, volume=4_000.0),
        ),
    )
    reescalada = rebase_to_post_split(serie.bars, ratio=4.0, from_date=THURSDAY)
    assert reescalada[0].close == pytest.approx(25.0)  # historico a la escala nueva
    assert reescalada[0].volume == pytest.approx(4_000.0)  # 1.000 acciones x 4
    assert reescalada[1].close == pytest.approx(25.0)  # la vela del broker no se toca
    assert find_unadjusted_jumps(PriceSeries("XYZ", reescalada)) == ()


def test_reescalar_con_un_ratio_invalido_falla() -> None:
    serie = flat_series(days=3)
    with pytest.raises(ValueError, match="ratio"):
        rebase_to_post_split(serie.bars, ratio=0.0, from_date=WEDNESDAY)


# --------------------------------------------------------------------------- #
# Earnings en sesiones (seccion 4.6 y 5.2)
# --------------------------------------------------------------------------- #


def test_las_sesiones_hasta_el_reporte_son_sesiones() -> None:
    # Caso del spec 16.4: la noche del martes 3 hay dos sesiones (4 y 5).
    assert sessions_to_earnings(CAL, dt.date(2026, 11, 3), THURSDAY) == 2
    assert sessions_to_earnings(CAL, dt.date(2026, 11, 4), THURSDAY) == 1
    assert sessions_to_earnings(CAL, dt.date(2026, 11, 2), THURSDAY) == 3


def test_sin_fecha_no_hay_conteo() -> None:
    # D1: el motor no puede suponer que no habra reporte.
    assert sessions_to_earnings(CAL, dt.date(2026, 11, 3), None) is None


def test_rebased_series_empalma_el_split_al_historico() -> None:
    # Split 4:1: el historico viejo (100) queda en la escala de hoy (25) y el volumen
    # viejo se cuadruplica; la vela del split queda intacta (es el precio real).
    d1, d2 = dt.date(2026, 1, 2), dt.date(2026, 1, 5)
    bars = (
        Bar(d1, 100.0, 101.0, 99.0, 100.0, 1_000.0),
        Bar(d2, 25.0, 25.5, 24.5, 25.0, 4_000.0),
    )
    out = rebased_series(PriceSeries("X", bars))
    assert out.bars[0].close == pytest.approx(25.0)
    assert out.bars[0].volume == pytest.approx(4_000.0)
    assert out.bars[1].close == pytest.approx(25.0)
