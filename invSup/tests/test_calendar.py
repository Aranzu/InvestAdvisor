"""Sesiones de mercado: el conteo del spec (seccion 4.6) y el calendario.

Si el conteo de sesiones esta mal, el blackout de earnings miente y el motor
mantiene un trade el dia del reporte. Estos tests fijan numeros que se pueden
mirar en un calendario, no formulas.
"""

from __future__ import annotations

import datetime as dt

import pytest

from swing.core.calendar import SessionCalendar, good_friday, us_market_holidays

#: Jueves 2026-11-05: el caso del spec 16.4.
EARNINGS = dt.date(2026, 11, 5)
TUESDAY = dt.date(2026, 11, 3)
MONDAY = dt.date(2026, 11, 2)

CAL_US = SessionCalendar.us()


def d(year: int, month: int, day: int) -> dt.date:
    return dt.date(year, month, day)


# --------------------------------------------------------------------------- #
# Reglas de feriado
# --------------------------------------------------------------------------- #


def test_fines_de_semana_no_son_sesion() -> None:
    assert SessionCalendar().is_session(d(2026, 11, 4)) is True  # miercoles
    assert SessionCalendar().is_session(d(2026, 11, 7)) is False  # sabado
    assert SessionCalendar().is_session(d(2026, 11, 8)) is False  # domingo


def test_navidad_2026_cierra_el_mercado() -> None:
    # 25 de diciembre de 2026 cae en viernes: se cierra.
    assert CAL_US.is_session(d(2026, 12, 25)) is False
    assert CAL_US.is_session(d(2026, 12, 24)) is True


def test_feriado_en_fin_de_semana_se_observa_al_otro_dia() -> None:
    # 4 de julio de 2020 fue sabado: el mercado abrio el viernes 3.
    cal = SessionCalendar(holidays=us_market_holidays(2020, 2020))
    assert cal.is_session(d(2020, 7, 3)) is False
    assert cal.is_session(d(2020, 7, 2)) is True


def test_juneteenth_no_existe_antes_de_2022() -> None:
    cal = SessionCalendar(holidays=us_market_holidays(2021, 2021))
    assert cal.is_session(d(2021, 6, 18)) is True  # viernes previo, sesion normal


def test_viernes_santo_es_feriado_en_cada_ano() -> None:
    assert good_friday(2026) == d(2026, 4, 3)
    assert good_friday(2027) == d(2027, 3, 26)
    assert CAL_US.is_session(good_friday(2026)) is False


def test_rango_invertido_no_revienta() -> None:
    assert us_market_holidays(2030, 2020) == frozenset()


# --------------------------------------------------------------------------- #
# Conteo de sesiones
# --------------------------------------------------------------------------- #


def test_dos_sesiones_hasta_el_reporte() -> None:
    # Caso 16.4: earnings el jueves 5, lead_sessions 2 -> la noche del martes 3 ya
    # estan a dos sesiones, y la orden sale esa noche para el miercoles 4.
    assert CAL_US.sessions_until(EARNINGS, TUESDAY) == 2
    assert CAL_US.next_session(TUESDAY) == d(2026, 11, 4)


def test_la_noche_anterior_al_reporte_solo_mira_una_sesion() -> None:
    assert CAL_US.sessions_until(EARNINGS, MONDAY) == 3


def test_sesiones_no_cuentan_fines_de_semana() -> None:
    # Del lunes 2 al lunes 9 hay 5 sesiones, no 8 dias.
    assert CAL_US.sessions_between(MONDAY, d(2026, 11, 9)) == 5


def test_feriado_intermedio_no_es_sesion() -> None:
    # Del 24 de noviembre (martes, dia de accion de gracias) al 27: el conteo
    # respeta el feriado, que es justo el caso que el spec pide explícito.
    cal = SessionCalendar(holidays=us_market_holidays(2026, 2026))
    assert cal.is_session(d(2026, 11, 26)) is False  # jueves, Thanksgiving
    assert cal.sessions_between(d(2026, 11, 24), d(2026, 11, 27)) == 2


def test_sin_feriados_el_conteo_sobrecuenta_y_no_estima_menos() -> None:
    # El supuesto conservador: sin calendario de feriados se cuentan mas sesiones.
    naive = SessionCalendar()
    assert naive.sessions_between(d(2026, 11, 24), d(2026, 11, 27)) == 3
    assert naive.sessions_between(d(2026, 11, 24), d(2026, 11, 27)) > CAL_US.sessions_between(
        d(2026, 11, 24), d(2026, 11, 27)
    )


def test_un_objetivo_pasado_no_es_negativo() -> None:
    assert CAL_US.sessions_until(d(2026, 10, 1), TUESDAY) == 0
    assert CAL_US.sessions_between(EARNINGS, MONDAY) == 0


def test_avanzar_sesiones() -> None:
    assert CAL_US.add_sessions(MONDAY, 1) == TUESDAY
    assert CAL_US.add_sessions(TUESDAY, 2) == d(2026, 11, 5)
    assert CAL_US.add_sessions(TUESDAY, 0) == TUESDAY


def test_sesion_anterior() -> None:
    # Domingo 8 -> viernes 6.
    assert CAL_US.previous_session(d(2026, 11, 8)) == d(2026, 11, 6)


def test_no_se_puede_avanzar_en_negativo() -> None:
    with pytest.raises(ValueError, match="count"):
        CAL_US.add_sessions(MONDAY, -1)
