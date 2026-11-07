"""Tipos de mercado: velas imposibles, series desordenadas y columnas para indicadores.

Un ``Bar`` con el maximo mas bajo que el cierre tiene que fallar al construirse. La
alternativa (aceptarlo y marcarlo despues) deja un precio imposible viajando por el
motor hasta que alguien lo nota en el informe, o nunca.
"""

from __future__ import annotations

import datetime as dt

import pytest

from swing.core.market import Bar, DataProvider, EarningsInfo, PriceSeries


def bar(
    day: int,
    *,
    open_: float = 100.0,
    high: float = 101.0,
    low: float = 99.0,
    close: float = 100.5,
    volume: float = 1000.0,
) -> Bar:
    return Bar(date=dt.date(2026, 1, day), open=open_, high=high, low=low, close=close, volume=volume)


def test_una_vela_normal_se_construye() -> None:
    vela = bar(5)
    assert vela.is_flat is False


def test_el_maximo_no_puede_quedar_bajo_el_cierre() -> None:
    with pytest.raises(ValueError, match="vela inconsistente"):
        bar(5, high=100.0, close=100.5)


def test_el_minimo_no_puede_quedar_sobre_la_apertura() -> None:
    with pytest.raises(ValueError, match="vela inconsistente"):
        bar(5, low=100.2, open_=100.0)


def test_precios_no_positivos_son_imposibles() -> None:
    with pytest.raises(ValueError, match="no positivos"):
        bar(5, close=0.0)


def test_volumen_negativo_es_imposible() -> None:
    with pytest.raises(ValueError, match="volumen negativo"):
        bar(5, volume=-1.0)


def test_sesion_plana_se_reconoce() -> None:
    assert bar(5, open_=100.0, high=100.0, low=100.0, close=100.0).is_flat


def test_la_serie_rechaza_fechas_repetidas() -> None:
    with pytest.raises(ValueError, match="fecha repetida"):
        PriceSeries("AMD", (bar(5), bar(5)))


def test_la_serie_rechaza_velas_desordenadas() -> None:
    with pytest.raises(ValueError, match="desordenadas"):
        PriceSeries("AMD", (bar(6), bar(5)))


def test_una_serie_vacia_no_tiene_ultimo_cierre() -> None:
    vacia = PriceSeries.empty("AMD")
    assert vacia.is_empty
    assert len(vacia) == 0
    assert vacia.last_date is None
    assert vacia.last_close is None
    assert vacia.first_date is None


def test_la_serie_se_recorta_por_fechas() -> None:
    serie = PriceSeries("AMD", tuple(bar(day) for day in range(1, 11)))
    assert len(serie.slice(start=dt.date(2026, 1, 3), end=dt.date(2026, 1, 5))) == 3
    assert len(serie.tail(2)) == 2
    assert serie.tail(2).first_date == dt.date(2026, 1, 9)
    assert serie.has_session(dt.date(2026, 1, 4))
    assert not serie.has_session(dt.date(2026, 1, 30))


def test_las_columnas_quedan_indexadas_por_fecha() -> None:
    serie = PriceSeries("AMD", (bar(5), bar(6)))
    closes = serie.closes()
    assert list(closes.index) == [pd_ts(2026, 1, 5), pd_ts(2026, 1, 6)]
    assert list(closes.values) == [100.5, 100.5]
    assert list(serie.volumes().values) == [1000.0, 1000.0]


def test_la_frame_trae_las_cinco_columnas() -> None:
    frame = PriceSeries("AMD", (bar(5),)).frame()
    assert list(frame.columns) == ["open", "high", "low", "close", "volume"]
    assert frame["close"].iloc[0] == 100.5


def test_recortar_mas_alla_del_rango_devuelve_vacio() -> None:
    serie = PriceSeries("AMD", (bar(5),))
    assert serie.slice(start=dt.date(2026, 2, 1)).is_empty
    assert serie.tail(0).is_empty


def test_earnings_sin_fecha_es_un_dato_faltante_declarado() -> None:
    # D1: la ausencia de fecha no es "no hay reporte".
    sin_fecha = EarningsInfo.unknown("yfinance")
    assert sin_fecha.is_missing
    assert sin_fecha.confirmed is False
    con_fecha = EarningsInfo(date=dt.date(2026, 11, 5), source="yfinance", confirmed=True)
    assert not con_fecha.is_missing


def test_un_fake_es_un_data_provider() -> None:
    class Fake:
        def daily_bars(self, ticker: str, *, start: dt.date, end: dt.date) -> PriceSeries:
            return PriceSeries.empty(ticker)

        def earnings(self, ticker: str) -> EarningsInfo:
            return EarningsInfo.unknown("fake")

        def fx_usdclp(self, on: dt.date) -> float | None:
            return None

    assert isinstance(Fake(), DataProvider)


def pd_ts(year: int, month: int, day: int):
    import pandas as pd

    return pd.Timestamp(dt.date(year, month, day))
