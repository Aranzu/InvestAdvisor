"""Cache incremental de velas (spec v3.1 seccion 6: "solo se descargan las velas nuevas").

La propiedad que importa es verificable sin red: con la cache al dia, el rango que hay
que descargar es ``None``. Un dia que descarga lo mismo de nuevo no es lento, es
fragil: cada recarga es una occasion mas de que el provider conteste distinto.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from swing.adapters.cache import BarsCache, CacheError
from swing.core.market import Bar, PriceSeries

LUNES = dt.date(2026, 11, 2)
MARTES = dt.date(2026, 11, 3)
MIERCOLES = dt.date(2026, 11, 4)
JUEVES = dt.date(2026, 11, 5)


def bar(day: dt.date, close: float = 100.0, volume: float = 1_000_000.0) -> Bar:
    return Bar(date=day, open=close, high=close * 1.01, low=close * 0.99, close=close, volume=volume)


def serie(*days: dt.date) -> PriceSeries:
    return PriceSeries("AMD", tuple(bar(day) for day in days))


@pytest.fixture
def cache(tmp_path: Path) -> BarsCache:
    return BarsCache(tmp_path / "bars")


def test_la_cache_vacia_no_devuelve_nada(cache: BarsCache) -> None:
    assert cache.read("AMD").is_empty


def test_ida_y_vuelta_conserva_los_datos(cache: BarsCache) -> None:
    original = serie(LUNES, MARTES, MIERCOLES)
    cache.write("AMD", original)
    leida = cache.read("AMD")
    assert len(leida) == 3
    assert leida.bars == original.bars


def test_el_archivo_es_csv_legible(cache: BarsCache) -> None:
    ruta = cache.write("AMD", serie(MIERCOLES))
    texto = ruta.read_text(encoding="utf-8")
    assert texto.splitlines()[0] == "date,open,high,low,close,volume"
    assert "2026-11-04" in texto


def test_no_quedan_temporales_tras_escribir(cache: BarsCache) -> None:
    cache.write("AMD", serie(MIERCOLES))
    assert list(cache.path_for("AMD").parent.glob("*.tmp")) == []


def test_con_la_cache_al_dia_no_hay_que_descargar_nada(cache: BarsCache) -> None:
    cache.write("AMD", serie(LUNES, MARTES, MIERCOLES))
    assert cache.missing_range(cache.read("AMD"), LUNES, MIERCOLES) is None


def test_solo_se_pide_la_cola_nueva(cache: BarsCache) -> None:
    cache.write("AMD", serie(LUNES, MARTES))
    pedido = cache.missing_range(cache.read("AMD"), LUNES, JUEVES)
    assert pedido == (MIERCOLES, JUEVES)


def test_un_hueco_al_inicio_rehace_el_rango_pedido(cache: BarsCache) -> None:
    # Prefiero volver a descargar a servir una serie con un hueco en el medio.
    cache.write("AMD", serie(MARTES, MIERCOLES))
    assert cache.missing_range(cache.read("AMD"), LUNES, MIERCOLES) == (LUNES, MIERCOLES)


def test_cache_vacia_pide_el_rango_entero(cache: BarsCache) -> None:
    assert cache.missing_range(cache.read("AMD"), LUNES, MIERCOLES) == (LUNES, MIERCOLES)


def test_un_rango_invertido_es_un_error(cache: BarsCache) -> None:
    with pytest.raises(ValueError, match="anterior a start"):
        cache.missing_range(cache.read("AMD"), MIERCOLES, LUNES)


def test_al_fusionar_gana_lo_descargado(cache: BarsCache) -> None:
    # El provider puede corregir la ultima vela; la cache no la congela.
    cacheada = PriceSeries("AMD", (bar(MARTES, close=100.0), bar(MIERCOLES, close=100.0)))
    descargada = PriceSeries("AMD", (bar(MIERCOLES, close=101.5), bar(JUEVES, close=102.0)))
    unida = cache.merge("AMD", cacheada, descargada)
    assert [b.date for b in unida.bars] == [MARTES, MIERCOLES, JUEVES]
    assert unida.bars[1].close == pytest.approx(101.5)


def test_un_csv_desordenado_se_lee_ordenado(cache: BarsCache) -> None:
    # El archivo lo puede haber editado una persona o una corrida interrumpida: la
    # serie que sale tiene que estar ordenada o nada despues puede afirmar que la
    # ultima vela es la ultima sesion.
    ruta = cache.path_for("AMD")
    ruta.parent.mkdir(parents=True, exist_ok=True)
    ruta.write_text(
        "date,open,high,low,close,volume\n"
        "2026-11-04,100,101,99,100,1000\n"
        "2026-11-03,100,101,99,100,1000\n"
        "2026-11-02,100,101,99,100,1000\n",
        encoding="utf-8",
    )
    assert [b.date for b in cache.read("AMD").bars] == [LUNES, MARTES, MIERCOLES]


def test_un_csv_sin_columnas_es_un_error_explicito(cache: BarsCache) -> None:
    ruta = cache.path_for("AMD")
    ruta.parent.mkdir(parents=True, exist_ok=True)
    ruta.write_text("date,close\n2026-11-04,100\n", encoding="utf-8")
    with pytest.raises(CacheError, match="faltan columnas"):
        cache.read("AMD")


def test_un_csv_con_fecha_invalida_dice_el_archivo(cache: BarsCache) -> None:
    ruta = cache.path_for("AMD")
    ruta.parent.mkdir(parents=True, exist_ok=True)
    ruta.write_text(
        "date,open,high,low,close,volume\nayer,100,101,99,100,1000\n", encoding="utf-8"
    )
    with pytest.raises(CacheError, match="fecha invalida"):
        cache.read("AMD")


def test_un_ticker_con_barra_vertical_no_escapa_del_directorio(cache: BarsCache) -> None:
    ruta = cache.path_for("../../escapate")
    assert ruta.parent == cache.root
    assert ".." not in ruta.stem


def test_un_symbol_vacio_no_produce_archivo(cache: BarsCache) -> None:
    with pytest.raises(CacheError, match="symbol invalido"):
        cache.path_for("  ")
