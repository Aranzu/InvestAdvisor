"""Provider de Yahoo: normalizacion, cache incremental y el dato faltante de earnings.

La suite completa es **offline**: el ``fetcher`` es un doble que registra lo que se le
pide y devuelve frames sinteticos. Los tests de red van al final, marcados ``live`` y
con ``RUN_LIVE=1``, como pide el spec.

Lo que se verifica aqui y no en los tests de red:

- con la cache al dia no se pide nada (una vez, no en cada corrida);
- Asking for more only asks for the tail;
- un ticker del broker se traduce al symbol de Yahoo, y ``BRK.B`` no queda en
  ``DATA_GAP`` para siempre;
- si la fuente no da fecha de earnings, el resultado es "dato faltante", no "no hay
  reporte" (D1).
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pandas as pd
import pytest

from swing.adapters.cache import BarsCache
from swing.adapters.data_yahoo import (
    FX_SYMBOL,
    DataUnavailable,
    EarningsFetcher,
    FetchRequest,
    YahooDataProvider,
    _yf_timestamp,
    _yfinance_bars,
    frame_to_series,
    symbols_from,
    to_yahoo_symbol,
)
from swing.core.calendar import SessionCalendar
from swing.core.market import DataProvider

LUNES = dt.date(2026, 11, 2)
MARTES = dt.date(2026, 11, 3)
MIERCOLES = dt.date(2026, 11, 4)
JUEVES = dt.date(2026, 11, 5)


def frame(*days: dt.date, close: float = 100.0, volume: float = 5_000_000.0) -> pd.DataFrame:
    """Frame con las columnas que entrega el provider, OHLC coherente entre si."""
    closes = [close + index for index in range(len(days))]
    return pd.DataFrame(
        {
            "Open": closes,
            "High": [value * 1.01 for value in closes],
            "Low": [value * 0.99 for value in closes],
            "Close": closes,
            "Volume": [volume] * len(days),
        },
        index=pd.to_datetime(days),
    )


class FakeFetcher:
    """Doble de la red que registra pedidos y responde con filas sinteticas."""

    def __init__(self, rows: dict[dt.date, float] | None = None) -> None:
        self.requests: list[FetchRequest] = []
        self.rows = rows

    def __call__(self, request: FetchRequest) -> pd.DataFrame:
        self.requests.append(request)
        if self.rows is not None:
            picked = sorted(day for day in self.rows if request.start <= day <= request.end)
            if not picked:
                return pd.DataFrame()
            return frame(*picked, close=self.rows[picked[0]] if len(picked) == 1 else 100.0)
        days = _sessions(request.start, request.end)
        return frame(*days) if days else pd.DataFrame()


def _sessions(start: dt.date, end: dt.date) -> list[dt.date]:
    out: list[dt.date] = []
    day = start
    while day <= end:
        if day.weekday() < 5:
            out.append(day)
        day += dt.timedelta(days=1)
    return out


def provider(
    tmp_path: Path,
    fetcher: FakeFetcher | None = None,
    earnings: EarningsFetcher | None = None,
) -> YahooDataProvider:
    fake = fetcher if fetcher is not None else FakeFetcher()
    return YahooDataProvider(
        BarsCache(tmp_path / "bars"),
        bars_fetcher=fake,
        earnings_fetcher=earnings if earnings is not None else (lambda symbol, limit: None),
    )


# --------------------------------------------------------------------------- #
# Normalizacion de simbolos (spec seccion 8)
# --------------------------------------------------------------------------- #


def test_se_quita_el_sufijo_del_broker() -> None:
    assert to_yahoo_symbol("AMD.US") == "AMD"
    assert to_yahoo_symbol(" amd ") == "AMD"


def test_las_clases_de_berkshire_se_mapean() -> None:
    # Yahoo usa guion: sin el mapeo el ticker no baja nunca.
    assert to_yahoo_symbol("BRK.B") == "BRK-B"
    assert to_yahoo_symbol("BF.B") == "BF-B"


def test_un_universo_no_repite_symbols() -> None:
    assert symbols_from(["AMD.US", "AMD", "BRK.B"]) == ("AMD", "BRK-B")


def test_el_provider_es_un_data_provider(tmp_path: Path) -> None:
    assert isinstance(provider(tmp_path), DataProvider)


# --------------------------------------------------------------------------- #
# Cache incremental
# --------------------------------------------------------------------------- #


def test_la_primera_vez_descarga_el_rango_pedido(tmp_path: Path) -> None:
    fetcher = FakeFetcher()
    serie = provider(tmp_path, fetcher).daily_bars("AMD.US", start=LUNES, end=MIERCOLES)
    assert fetcher.requests == [FetchRequest("AMD", LUNES, MIERCOLES)]
    assert serie.first_date == LUNES
    assert len(serie) == 3


def test_la_segunda_corrida_no_toca_la_red(tmp_path: Path) -> None:
    fetcher = FakeFetcher()
    p = provider(tmp_path, fetcher)
    p.daily_bars("AMD", start=LUNES, end=MIERCOLES)
    p.daily_bars("AMD", start=LUNES, end=MIERCOLES)
    assert len(fetcher.requests) == 1


def test_al_extender_el_rango_solo_se_pide_la_cola(tmp_path: Path) -> None:
    fetcher = FakeFetcher()
    p = provider(tmp_path, fetcher)
    p.daily_bars("AMD", start=LUNES, end=MIERCOLES)
    p.daily_bars("AMD", start=LUNES, end=JUEVES)
    assert fetcher.requests[1] == FetchRequest("AMD", JUEVES, JUEVES)


def test_la_serie_devuelta_se_recorta_al_rango_pedido(tmp_path: Path) -> None:
    fetcher = FakeFetcher()
    p = provider(tmp_path, fetcher)
    p.daily_bars("AMD", start=LUNES, end=JUEVES)
    recorte = p.daily_bars("AMD", start=MARTES, end=MIERCOLES)
    assert recorte.first_date == MARTES
    assert recorte.last_date == MIERCOLES
    assert len(recorte) == 2


def test_lo_que_falta_de_la_cache_se_conserva_al_ampliar(tmp_path: Path) -> None:
    fetcher = FakeFetcher()
    p = provider(tmp_path, fetcher)
    p.daily_bars("AMD", start=LUNES, end=MIERCOLES)
    completa = p.daily_bars("AMD", start=LUNES, end=JUEVES)
    assert completa.first_date == LUNES
    assert completa.last_date == JUEVES


def test_daily_bars_report_cachea_la_serie_coherente(tmp_path: Path) -> None:
    fetcher = FakeFetcher()
    p = provider(tmp_path, fetcher)
    serie, repaired = p.daily_bars_report("AMD", start=LUNES, end=MIERCOLES)
    assert repaired is False
    assert len(serie) == 3
    p.daily_bars_report("AMD", start=LUNES, end=MIERCOLES)
    assert len(fetcher.requests) == 1


def test_daily_bars_report_descarta_la_vela_sin_cachearla(tmp_path: Path) -> None:
    calls: list[FetchRequest] = []

    def fetcher(request: FetchRequest) -> pd.DataFrame:
        calls.append(request)
        crudo = frame(*_sessions(request.start, request.end))
        crudo.loc[pd.to_datetime(request.start), "High"] = 1.0
        return crudo

    p = YahooDataProvider(BarsCache(tmp_path / "bars"), bars_fetcher=fetcher)
    serie, repaired = p.daily_bars_report("AMD", start=LUNES, end=MIERCOLES)
    assert repaired is True
    assert [bar.date for bar in serie.bars] == [MARTES, MIERCOLES]
    # La vista recortada no se guarda: la cache queda sin el symbol y la proxima
    # corrida vuelve a bajar y a declarar el descarte (no se "arregla" en silencio).
    assert not (tmp_path / "bars" / "AMD.csv").exists()
    _, repaired_otra_vez = p.daily_bars_report("AMD", start=LUNES, end=MIERCOLES)
    assert repaired_otra_vez is True
    assert len(calls) == 2


def test_un_frame_vacio_no_inventa_velas(tmp_path: Path) -> None:
    fetcher = FakeFetcher(rows={})
    serie = provider(tmp_path, fetcher).daily_bars("AMD", start=LUNES, end=MIERCOLES)
    assert serie.is_empty


# --------------------------------------------------------------------------- #
# Normalizacion del frame
# --------------------------------------------------------------------------- #


def test_las_filas_con_nan_se_omiten() -> None:
    crudo = frame(MARTES)
    crudo.loc[pd.to_datetime(MIERCOLES)] = [float("nan")] * 5
    serie = frame_to_series("AMD", crudo)
    assert [b.date for b in serie.bars] == [MARTES]


def test_las_filas_con_precio_cero_se_omiten() -> None:
    # El provider a veces devuelve una fila de relleno con Open=0: es "sin dato", no
    # una vela, y no puede tumbar la conversion de todo el universo.
    crudo = frame(MARTES, MIERCOLES)
    crudo.loc[pd.to_datetime(MARTES), "Open"] = 0.0
    serie = frame_to_series("AMD", crudo)
    assert [b.date for b in serie.bars] == [MIERCOLES]


def test_un_frame_sin_columnas_de_precio_falla_con_mensaje() -> None:
    with pytest.raises(DataUnavailable, match="columnas"):
        frame_to_series("AMD", pd.DataFrame({"Close": [1.0]}))


def test_una_vela_imposible_falla_la_conversion() -> None:
    crudo = frame(MARTES)
    crudo.loc[pd.to_datetime(MARTES), "High"] = 1.0
    with pytest.raises(ValueError, match="vela inconsistente"):
        frame_to_series("AMD", crudo)


def test_la_vela_imposible_se_descarta_si_se_pide() -> None:
    # La vista best effort del informe descarta la vela contraditoria (no la repara)
    # y sigue con las demas, en vez de tirar la serie completa.
    crudo = frame(MARTES, MIERCOLES)
    crudo.loc[pd.to_datetime(MARTES), "High"] = 1.0
    serie = frame_to_series("AMD", crudo, skip_incoherent=True)
    assert [b.date for b in serie.bars] == [MIERCOLES]


def test_el_indice_en_utc_no_adelanta_el_dia() -> None:
    # Una corrida a las 23:00 de Chile con index en UTC trae la fecha del dia
    # siguiente: el motor tiene que ver la fecha de la sesion de NYSE.
    crudo = frame(MARTES)
    crudo.index = pd.DatetimeIndex(["2026-11-04 01:00:00+00:00"])
    serie = frame_to_series("AMD", crudo)
    assert serie.bars[0].date == MARTES


# --------------------------------------------------------------------------- #
# Pedido a Yahoo: la medianoche que no existe en el cambio de hora
# --------------------------------------------------------------------------- #


def test_el_pedido_usa_mediodia_para_esquivar_la_medianoche_inexistente() -> None:
    # El cambio de horario de Santiago hace que el 2025-09-07 00:00 no exista; yfinance
    # localiza la fecha a la zona del mercado y revienta. Al pedir un datetime al
    # mediodia el instante existe y la sesion pedida no cambia.
    start = _yf_timestamp(dt.date(2025, 9, 7))
    assert start == dt.datetime(2025, 9, 7, 12, 0)


def test_yfinance_bars_pasa_datetimes_al_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    import yfinance as yf

    captured: dict[str, object] = {}

    class FakeTicker:
        def __init__(self, symbol: str) -> None:
            self.symbol = symbol

        def history(self, **kwargs: object) -> pd.DataFrame:
            captured.update(kwargs)
            return frame(MARTES)

    monkeypatch.setattr(yf, "Ticker", FakeTicker)
    request = FetchRequest("FCX", dt.date(2025, 9, 7), dt.date(2026, 10, 12))
    result = _yfinance_bars(request)
    assert list(result.columns) == ["Open", "High", "Low", "Close", "Volume"]
    assert captured["start"] == dt.datetime(2025, 9, 7, 12, 0)
    assert captured["end"] == dt.datetime(2026, 10, 13, 12, 0)


# --------------------------------------------------------------------------- #
# Earnings (D1)
# --------------------------------------------------------------------------- #


def test_se_toma_el_reporte_futuro_mas_proximo() -> None:
    near = dt.date.today() + dt.timedelta(days=30)
    far = dt.date.today() + dt.timedelta(days=120)
    tabla = pd.DataFrame(
        {"Earnings Date": [dt.datetime.combine(far, dt.time()), dt.datetime.combine(near, dt.time())]},
        index=pd.to_datetime([far.isoformat(), near.isoformat()]),
    )
    p = provider(Path("."), earnings=lambda s, n: tabla)
    info = p.earnings("AMD")
    assert info.confirmed
    assert info.date is not None
    assert info.date > dt.date.today()
    assert info.source.startswith("yfinance")


def test_sin_tabla_el_reporte_es_un_dato_faltante() -> None:
    # D1: esto no significa "no hay reporte".
    assert provider(Path("."), earnings=lambda s, n: None).earnings("AMD").confirmed is False
    assert provider(Path("."), earnings=lambda s, n: pd.DataFrame()).earnings("AMD").confirmed is False


def test_una_tabla_solo_con_reportes_pasados_tambien_es_dato_faltante() -> None:
    viejo = pd.DataFrame({"Earnings Date": [dt.datetime(2015, 11, 5)]}, index=pd.to_datetime(["2015-11-05"]))
    info = provider(Path("."), earnings=lambda s, n: viejo).earnings("AMD")
    assert info.is_missing
    assert info.source.startswith("yfinance")


# --------------------------------------------------------------------------- #
# FX
# --------------------------------------------------------------------------- #


def test_el_fx_se_pide_con_el_simbolo_de_divisa(tmp_path: Path) -> None:
    fetcher = FakeFetcher()
    p = provider(tmp_path, fetcher)
    fx = p.fx_usdclp(MIERCOLES)
    assert fetcher.requests[0].symbol == FX_SYMBOL
    assert fetcher.requests[0].end == MIERCOLES
    assert fx is not None
    assert fx == pytest.approx(p.cache.read(FX_SYMBOL).last_close)


def test_el_fx_toma_la_ultima_vela_anterior_si_el_dia_no_cotizo(tmp_path: Path) -> None:
    # Un feriado local puede dejar el par sin vela el dia pedido.
    p = provider(tmp_path, FakeFetcher())
    fx = p.fx_usdclp(MARTES)
    cacheada = p.cache.read(FX_SYMBOL)
    assert fx == pytest.approx(cacheada.bars[-1].close)


def test_sin_dato_de_divisa_no_hay_numero(tmp_path: Path) -> None:
    assert provider(tmp_path, FakeFetcher(rows={})).fx_usdclp(MIERCOLES) is None


# --------------------------------------------------------------------------- #
# Red real (opt-in)
# --------------------------------------------------------------------------- #


@pytest.mark.live
@pytest.mark.skipif(not __import__("os").environ.get("RUN_LIVE"), reason="requiere RUN_LIVE=1")
def test_descarga_real_de_amd(tmp_path: Path) -> None:
    p = YahooDataProvider(BarsCache(tmp_path / "bars"))
    hoy = dt.date.today()
    serie = p.daily_bars("AMD", start=hoy - dt.timedelta(days=120), end=hoy)
    assert not serie.is_empty
    assert serie.last_date == SessionCalendar.us().last_session_on_or_before(hoy)
    assert p.earnings("AMD").source.startswith("yfinance")


# --------------------------------------------------------------------------- #
# Foto de cotizacion (horario extendido)
# --------------------------------------------------------------------------- #


def test_quote_snapshot_traduce_el_symbol_y_pide_la_foto(tmp_path: Path) -> None:
    seen: list[str] = []

    def fetcher(symbol: str) -> dict[str, object]:
        seen.append(symbol)
        return {"preMarketPrice": 1.0, "postMarketPrice": 2.0}

    p = YahooDataProvider(BarsCache(tmp_path / "bars"), quote_fetcher=fetcher)
    assert p.quote_snapshot("AMD.US") == {"preMarketPrice": 1.0, "postMarketPrice": 2.0}
    assert seen == ["AMD"]  # el symbol del broker se traduce antes de consultar


def test_quote_snapshot_sin_fuente_no_levanta(tmp_path: Path) -> None:
    def boom(symbol: str) -> dict[str, object]:
        raise RuntimeError("sin red")

    p = YahooDataProvider(BarsCache(tmp_path / "bars"), quote_fetcher=boom)
    assert p.quote_snapshot("AMD") is None


def test_provider_offline_no_consulta_la_foto_de_cotizacion(tmp_path: Path) -> None:
    from swing.adapters.data_yahoo import provider_from_project

    p = provider_from_project(tmp_path / "bars", offline=True)
    assert p.quote_snapshot("AMD") is None
