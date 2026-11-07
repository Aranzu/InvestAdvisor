"""FX implicito del reporte del broker (spec v3.1 seccion 8).

El numero importa mas de lo que parece: el riesgo se calcula en CLP, asi que un FX mal
leido no produce un rechazo visible, produce una posicion con el doble o la mitad del
riesgo que el usuario cree tener.
"""

from __future__ import annotations

import datetime as dt

import pytest

from swing.core.fx import FxRow, implicit_usd_clp, with_fallback

HOY = dt.date(2026, 11, 4)


def row(value_clp: float, volume: float = 100.0, price_usd: float = 10.0) -> FxRow:
    return FxRow(value_clp=value_clp, volume=volume, price_usd=price_usd)


def test_todas_las_filas_con_el_mismo_tipo_de_cambio() -> None:
    filas = (row(984_000.0), row(984_000.0), row(984_000.0), row(984_000.0))
    estimacion = implicit_usd_clp(filas)
    assert estimacion.usd_clp == pytest.approx(984.0)
    assert estimacion.source == "implicito"
    assert estimacion.rows_used == 4
    assert estimacion.rows_dropped == 0


def test_una_fila_atipica_no_mueve_la_mediana() -> None:
    # Con la media, esta fila sola correria el FX de todo el tamano de la corrida.
    filas = (row(984_000.0), row(984_000.0), row(984_000.0), row(2_000_000.0))
    estimacion = implicit_usd_clp(filas)
    assert estimacion.usd_clp == pytest.approx(984.0)
    assert estimacion.rows_dropped == 1


def test_una_dispersion_pequena_no_se_descarta() -> None:
    # 0,5 % es redondeo del broker, no una fila distinta.
    filas = (row(984_000.0), row(979_000.0), row(989_000.0))
    estimacion = implicit_usd_clp(filas)
    assert estimacion.rows_dropped == 0
    assert estimacion.usd_clp == pytest.approx(984.0)


def test_filas_sin_posicion_no_son_outliers() -> None:
    # Categorias y filas a volumen cero: no son un error de lectura, no son datos.
    filas = (row(984_000.0), row(984_000.0), row(984_000.0), row(500_000.0, volume=0.0))
    estimacion = implicit_usd_clp(filas)
    assert estimacion.usd_clp == pytest.approx(984.0)
    assert estimacion.rows_total == 4
    assert estimacion.rows_dropped == 0


def test_con_una_sola_fila_no_se_da_un_numero() -> None:
    # Con dos filas la mediana es indistinguible del valor de una de ellas.
    estimacion = implicit_usd_clp((row(984_000.0), row(700_000.0)))
    assert estimacion.usd_clp is None
    assert not estimacion.is_usable
    assert estimacion.source == "sin_filas_utiles"


def test_sin_filas_no_hay_fx() -> None:
    estimacion = implicit_usd_clp(())
    assert estimacion.usd_clp is None
    assert estimacion.rows_total == 0


def test_si_descartar_deja_pocas_filas_no_se_inventa_la_mediana() -> None:
    # Tres filas, dos atipicas: la mediana "filtrada" seria de una fila.
    estimacion = implicit_usd_clp((row(984_000.0), row(1_500_000.0), row(600_000.0)))
    assert estimacion.usd_clp is None
    assert estimacion.source == "outliers_dejan_pocas_filas"


def test_el_fallback_de_yahoo_va_marcado() -> None:
    sin_datos = implicit_usd_clp(())
    con_fallback = with_fallback(sin_datos, 984.5, HOY)
    assert con_fallback.usd_clp == pytest.approx(984.5)
    assert con_fallback.source == f"fallback_yahoo:{HOY.isoformat()}"


def test_un_fx_implicito_no_se_reemplaza() -> None:
    implicito = implicit_usd_clp((row(984_000.0), row(984_000.0), row(984_000.0)))
    assert with_fallback(implicito, 700.0, HOY).usd_clp == pytest.approx(984.0)
    assert with_fallback(implicito, 700.0, HOY).source == "implicito"


def test_sin_fallback_no_hay_numero() -> None:
    assert with_fallback(implicit_usd_clp(()), None, HOY).usd_clp is None
