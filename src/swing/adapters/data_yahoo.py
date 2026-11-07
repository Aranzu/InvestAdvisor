"""Proveedor de velas, earnings y FX sobre Yahoo Finance (spec v3.1 secciones 6 y 8).

Decisiones de diseno, y por que:

- **``fetcher`` inyectable.** Toda la logica (cache incremental, normalizacion del
  symbol, mapeo de columnas, deteccion de respuesta vacia) se ejercita con un ``fetcher``
  falso y sin red. Los tests de red quedan para ``RUN_LIVE=1``, como pide el spec, y el
  resto de la suite es offline y determinista.
- **Import perezoso de ``yfinance``.** Importarlo cuesta tiempo de arranque y el paquete
  hace trabajo de red al importarse en algunos versiones. El motor no lo necesita para
  arrancar, y el comando de diagnostico tiene que ser rapido.
- **Cache antes que red.** Cada symbol pide su rango completo al cache local; a la red
  solo va lo que falta (seccion 6). El proveedor es el que decide el rango, porque es el
  que sabe que hay en disco.
- **Sin ajustes.** ``auto_adjust=False`` y ``actions=False``: el precio crudo es el que
  el broker ejecuto (D6).

Lo que **no** hace, a proposito: inventar la fecha de earnings. Si la fuente no la entrega,
devuelve ``EarningsInfo`` con ``confirmed=False`` y el motor marca ``DATA_GAP`` (D1). Un
``None`` que significara "no hay reporte" seria la lectura opuesta y mucho mas peligrosa.
"""

from __future__ import annotations

import datetime as dt
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from swing.adapters.cache import BarsCache
from swing.core.market import Bar, EarningsInfo, PriceSeries

#: Yahoo usa guion donde el ticker usa punto: ``BRK.B`` -> ``BRK-B``.
_CLASS_TO_DASH: dict[str, str] = {"BRK.B": "BRK-B", "BF.B": "BF-B"}

#: XTB entrega los tickers con sufijo de mercado; para Yahoo no existe.
_XTB_SUFFIX = ".US"

#: Simbolo del par CLP/USD en Yahoo.
FX_SYMBOL = "CLP=X"

#: Cuantos reportes futuros pedir. Doce cubre el ano de una empresa que reporta
#: trimestral, con margen para los que reportan cada dos semanas.
EARNINGS_LIMIT = 12

#: Dias hacia atras que se piden por defecto. 260 sesiones cubren la SMA200 con
#: pendiente, la puerta de 52 semanas y el Chandelier de 22 ruedas.
DEFAULT_LOOKBACK_DAYS = 400

#: Reintentos y backoff de la descarga de precios. Yahoo devuelve fallos transitorios
#: (429, timeouts) que no deben traducirse en un ``DATA_GAP`` de un ticker sano.
NETWORK_RETRIES = 3
NETWORK_BACKOFF_S = 0.5

#: Timeout por peticion a Yahoo. Sin esto, un socket colgado deja la corrida programada
#: esperando indefinidamente.
YF_TIMEOUT_S = 20.0


class DataUnavailable(RuntimeError):
    """El provider no pudo entregar el dato. Se traduce a ``DATA_GAP``, no a excepcion
    abierta en el informe."""


@dataclass(frozen=True, slots=True)
class FetchRequest:
    """Lo que se le pide a la red. Objeto y no argumentos sueltos para poder registrar
    las llamadas en un test sin depender del mock."""

    symbol: str
    start: dt.date
    end: dt.date


#: Devuelve el frame crudo del provider, con las columnas como las entrega el.
FrameFetcher = Callable[[FetchRequest], pd.DataFrame]

#: Devuelve la tabla de fechas de reporte, o ``None`` si la fuente no responde.
EarningsFetcher = Callable[[str, int], pd.DataFrame | None]

#: Devuelve la foto de cotizacion cruda de un symbol (``info`` de Yahoo), o ``None``.
QuoteFetcher = Callable[[str], Mapping[str, object] | None]

#: Campos de horario extendido que pide el informe. Yahoo los publica en ``info``;
#: lo que no venga queda ``None`` (best effort, sin inventar).
_EXT_QUOTE_FIELDS: tuple[str, ...] = (
    "preMarketPrice",
    "preMarketChange",
    "preMarketChangePercent",
    "preMarketVolume",
    "preMarketTime",
    "postMarketPrice",
    "postMarketChange",
    "postMarketChangePercent",
    "postMarketVolume",
    "postMarketTime",
)


def to_yahoo_symbol(ticker: str) -> str:
    """Ticker del broker -> symbol de Yahoo.

    Dos transformaciones, ambas del spec (seccion 8): quitar el sufijo ``.US`` y mapear
    las clases de Berkshire. Sin el mapeo, ``BRK.B`` no baja nunca y el ticker queda en
    ``DATA_GAP`` para siempre.
    """
    symbol = ticker.strip().upper()
    if symbol.endswith(_XTB_SUFFIX):
        symbol = symbol[: -len(_XTB_SUFFIX)]
    return _CLASS_TO_DASH.get(symbol, symbol)


def _yf_timestamp(day: dt.date) -> dt.datetime:
    """``day`` al mediodia, como ``datetime`` para yfinance.

    yfinance localiza la fecha a la zona del mercado. Una medianoche que no existe
    (el arranque del horario de verano de Santiago: 00:00 salta directo a 01:00) hace
    fallar la descarga con ``... is a nonexistent time due to daylight savings time``,
    y ese error se confundia con un fallo de red (se reintentaba tres veces y el ticker
    quedaba "sin dato"). Pasar un instante al mediodia evita el limite horario sin mover
    el rango de sesiones pedido: yfinance descarta la hora y usa el dia.
    """
    return dt.datetime.combine(day, dt.time(12, 0))


def _yfinance_bars(request: FetchRequest) -> pd.DataFrame:
    """Descarga velas crudas. Es el unico punto del proyecto que toca la red de precios.

    Reintenta con backoff ante fallos transitorios y, si no lo logra, levanta
    ``DataUnavailable``: un problema de red es "sin dato", no un traceback abierto.
    """
    import yfinance as yf

    last_exc: Exception | None = None
    for attempt in range(NETWORK_RETRIES):
        try:
            frame = yf.Ticker(request.symbol).history(
                start=_yf_timestamp(request.start),
                end=_yf_timestamp(request.end + dt.timedelta(days=1)),  # yfinance es exclusivo en end
                auto_adjust=False,
                actions=False,
                raise_errors=False,
                timeout=YF_TIMEOUT_S,
            )
            return frame if isinstance(frame, pd.DataFrame) else pd.DataFrame()
        except Exception as exc:  # yfinance/requests no exponen una jerarquia estable
            last_exc = exc
            if attempt + 1 < NETWORK_RETRIES:
                time.sleep(NETWORK_BACKOFF_S * (2**attempt))
    raise DataUnavailable(f"{request.symbol}: fallo de red tras {NETWORK_RETRIES} intentos: {last_exc}") from last_exc


def _offline_bars(request: FetchRequest) -> pd.DataFrame:
    """Fetcher que nunca toca la red: lo que falte en cache es ``DataUnavailable``."""
    raise DataUnavailable(f"offline: {request.symbol} no esta en cache/bars ({request.start} a {request.end})")


def _yfinance_earnings(symbol: str, limit: int) -> pd.DataFrame | None:
    """Fechas de reporte que publica el provider (decision D1)."""
    import yfinance as yf

    try:
        frame = yf.Ticker(symbol).get_earnings_dates(limit=limit)
    except Exception:
        return None
    return frame if isinstance(frame, pd.DataFrame) else None


def _yfinance_quote_snapshot(symbol: str) -> dict[str, object] | None:
    """Foto de cotizacion de Yahoo (incluye horario extendido), best effort.

    Devuelve solo los campos que interesan al informe, con sus claves originales
    (``preMarketPrice``, ...); los que falten quedan ``None``. ``None`` si la
    fuente no responde: es "sin dato", no una excepcion abierta.
    """
    import yfinance as yf

    try:
        info = yf.Ticker(symbol).get_info()
    except Exception:
        return None
    if not isinstance(info, Mapping):
        return None
    return {key: info.get(key) for key in _EXT_QUOTE_FIELDS}


def _ohlc_incoherent(open_: float, high: float, low: float, close: float) -> bool:
    """Vela con el OHLC contraditorio entre si (lo que ``Bar`` rechaza con ``ValueError``)."""
    return high < max(open_, close) or low > min(open_, close)


def frame_to_series(symbol: str, frame: pd.DataFrame, *, skip_incoherent: bool = False) -> PriceSeries:
    """Frame crudo -> ``PriceSeries``.

    Se ignoran las filas con NaN en cualquier precio (los provider devuelven filas de
    weekend o de dias sin cotizacion), las que no son fecha valida, y las de precio no
    positivo (el provider a veces devuelve una fila de relleno con ``Open=0``); una fila
    asi es "sin dato", no una vela.

    Una vela imposible (alto menor que el cierre, o bajo mayor que la apertura) hace
    fallar la conversion con ``ValueError``: es un dato contraditorio y promediarlo
    inventaria un rango. Con ``skip_incoherent=True`` en cambio se **descarta esa vela**
    (no se repara: no se toca el precio) y se sigue con el resto; lo usa la vista best
    effort del informe, que declara el descarte en vez de esconderlo.
    """
    if frame.empty:
        return PriceSeries.empty(symbol)
    required = {"Open", "High", "Low", "Close"}
    missing = sorted(required - set(frame.columns))
    if missing:
        raise DataUnavailable(f"{symbol}: el provider no devolvio columnas {missing}")
    indexed = frame.reset_index()
    date_column = indexed.columns[0]
    bars: list[Bar] = []
    for values in indexed.to_dict(orient="records"):
        day = _as_date(values.get(date_column))
        if day is None:
            continue
        if any(pd.isna(values.get(column)) for column in (*required, "Volume")):
            continue
        prices = [float(values[column]) for column in ("Open", "High", "Low", "Close")]
        if any(price <= 0.0 for price in prices):
            continue
        if skip_incoherent and _ohlc_incoherent(*prices):
            continue
        bars.append(
            Bar(
                date=day,
                open=prices[0],
                high=prices[1],
                low=prices[2],
                close=prices[3],
                volume=float(values["Volume"]),
            )
        )
    return PriceSeries(symbol, tuple(bars))


MARKET_TZ = ZoneInfo("America/New_York")


def _as_date(value: object) -> dt.date | None:
    """Fecha de sesion desde lo que devuelva el provider.

    ``pandas.Timestamp`` es subclase de ``datetime`` y cae en la primera rama. Con hora
    explícita se convierte a la zona del mercado antes de tomar el dia: una vela sellada
    ``2026-11-04T01:00Z`` es la sesion del **martes 3** (20:00 en New York), y tomarle
    la fecha en UTC correria el dato un dia y volveria obsoleta la vela mas reciente.
    Sin zona, la fecha se usa tal cual: es lo que entrega Yahoo para las diarias.
    ``numpy.datetime64`` se resuelve por su dia.
    """
    if isinstance(value, dt.datetime):
        if value.tzinfo is not None:
            return value.astimezone(MARKET_TZ).date()
        return value.date()
    if isinstance(value, dt.date):
        return value
    if isinstance(value, np.datetime64):
        converted = value.astype("datetime64[D]").item()
        return converted if isinstance(converted, dt.date) else None
    if isinstance(value, str):
        try:
            return dt.date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


class YahooDataProvider:
    """``DataProvider`` sobre Yahoo, con cache incremental en disco."""

    def __init__(
        self,
        cache: BarsCache,
        *,
        bars_fetcher: FrameFetcher = _yfinance_bars,
        earnings_fetcher: EarningsFetcher = _yfinance_earnings,
        quote_fetcher: QuoteFetcher = _yfinance_quote_snapshot,
    ) -> None:
        self.cache = cache
        self._bars_fetcher = bars_fetcher
        self._earnings_fetcher = earnings_fetcher
        self._quote_fetcher = quote_fetcher

    def daily_bars(self, ticker: str, *, start: dt.date, end: dt.date) -> PriceSeries:
        """Velas de ``[start, end]``, descargando solo lo que falte."""
        symbol = to_yahoo_symbol(ticker)
        cached = self.cache.read(symbol)
        pending = self.cache.missing_range(cached, start, end)
        if pending is not None:
            fetched = self._fetch(FetchRequest(symbol, *pending))
            self.cache.write(symbol, self.cache.merge(symbol, cached, fetched))
        return self.cache.read(symbol).slice(start, end)

    def earnings(self, ticker: str) -> EarningsInfo:
        """Proximo reporte futuro conocido, o el dato faltante declarado (D1)."""
        symbol = to_yahoo_symbol(ticker)
        frame = self._earnings_fetcher(symbol, EARNINGS_LIMIT)
        if frame is None or frame.empty:
            return EarningsInfo.unknown("yfinance:get_earnings_dates")
        today = dt.date.today()
        upcoming = [day for day in (_as_date(value) for value in frame.index) if day is not None and day > today]
        if not upcoming:
            return EarningsInfo.unknown("yfinance:get_earnings_dates")
        return EarningsInfo(date=min(upcoming), source="yfinance:get_earnings_dates", confirmed=True)

    def quote_snapshot(self, ticker: str) -> Mapping[str, object] | None:
        """Foto de cotizacion de Yahoo (precio y horario extendido), best effort.

        No toca la cache de velas: es una consulta al quote del dia. Un fallo de
        red o de la fuente devuelve ``None`` (sin dato) y nunca levanta, porque
        el horario extendido es informativo y su ausencia no puede tumbar el
        informe.
        """
        try:
            data = self._quote_fetcher(to_yahoo_symbol(ticker))
        except Exception:
            return None
        return data if isinstance(data, Mapping) else None

    def fx_usdclp(self, on: dt.date) -> float | None:
        """CLP por USD del dia pedido; si no hay vela exacta, la ultima anterior."""
        symbol = FX_SYMBOL
        cached = self.cache.read(symbol)
        pending = self.cache.missing_range(cached, on - dt.timedelta(days=10), on)
        if pending is not None:
            fetched = self._fetch(FetchRequest(symbol, *pending))
            self.cache.write(symbol, self.cache.merge(symbol, cached, fetched))
        series = self.cache.read(symbol).slice(end=on)
        return series.last_close

    def daily_bars_report(self, ticker: str, *, start: dt.date, end: dt.date) -> tuple[PriceSeries, bool]:
        """Velas para el informe, best effort ante una vela contraditoria.

        Igual que ``daily_bars`` salvo que, si la descarga trae una vela con el OHLC
        contraditorio, la **descarta** (no la repara) y sigue con el resto, avisando con
        ``True`` en el segundo elemento. La serie recortada **no se cachea**: la cache
        guarda velas coherentes o nada, y asi el motor (que usa ``daily_bars``, fail-closed)
        sigue viendo el hueco en vez de un historico "arreglado" en silencio, y el informe
        vuelve a declarar el descarte en cada corrida.
        """
        symbol = to_yahoo_symbol(ticker)
        cached = self.cache.read(symbol)
        pending = self.cache.missing_range(cached, start, end)
        if pending is None:
            return cached.slice(start, end), False
        frame = self._raw_frame(FetchRequest(symbol, *pending))
        try:
            fetched = frame_to_series(symbol, frame)
        except ValueError:
            fetched = frame_to_series(symbol, frame, skip_incoherent=True)
            merged = self.cache.merge(symbol, cached, fetched)
            if merged.is_empty:
                raise
            return merged.slice(start, end), True
        self.cache.write(symbol, self.cache.merge(symbol, cached, fetched))
        return self.cache.read(symbol).slice(start, end), False

    def _raw_frame(self, request: FetchRequest) -> pd.DataFrame:
        """Frame crudo del provider, declarando el fallo en vez de propagarlo crudo.

        Cualquier error del fetcher (red, formato del proveedor) es un ``DataUnavailable``:
        el llamador ya lo trata como "ticker sin dato".
        """
        try:
            return self._bars_fetcher(request)
        except DataUnavailable:
            raise
        except Exception as exc:
            raise DataUnavailable(f"{request.symbol}: fallo al descargar velas: {exc}") from exc

    def _fetch(self, request: FetchRequest) -> PriceSeries:
        """Descarga y normaliza un rango; un ``ValueError`` por vela incoherente sube tal cual."""
        return frame_to_series(request.symbol, self._raw_frame(request))


def _offline_quotes(symbol: str) -> Mapping[str, object] | None:
    """Fetch de cotizacion que nunca toca la red (modo offline): sin dato."""
    return None


def provider_from_project(cache_dir: Path | None = None, *, offline: bool = False) -> YahooDataProvider:
    """Provider con la cache en ``cache/bars`` al lado del proyecto.

    Con ``offline=True`` no se toca la red: lo que falte en cache se declara
    ``DataUnavailable`` en vez de descargarse, y la foto de cotizacion (horario
    extendido) devuelve ``None``.
    """
    root = cache_dir if cache_dir is not None else Path(__file__).resolve().parents[3] / "cache" / "bars"
    if offline:
        return YahooDataProvider(
            BarsCache(root), bars_fetcher=_offline_bars, quote_fetcher=_offline_quotes
        )
    return YahooDataProvider(BarsCache(root), bars_fetcher=_yfinance_bars)


def symbols_from(tickers: Sequence[str]) -> tuple[str, ...]:
    """Symbols de Yahoo de un universo, sin repetir."""
    seen: dict[str, None] = {}
    for ticker in tickers:
        seen.setdefault(to_yahoo_symbol(ticker), None)
    return tuple(seen)


#: Devuelve el sector que declara el provider para un symbol, o ``None`` sin dato.
SectorFetcher = Callable[[str], str | None]


def _yfinance_sector(symbol: str) -> str | None:
    """Sector de Yahoo. ``None`` sin red o sin dato; nunca levanta."""
    import yfinance as yf

    try:
        info = yf.Ticker(symbol).get_info()
    except Exception:
        return None
    if not isinstance(info, dict):
        return None
    sector = info.get("sector")
    return str(sector).strip() if sector else None


def sectors_from(
    tickers: Sequence[str],
    fetcher: SectorFetcher = _yfinance_sector,
) -> dict[str, str]:
    """Sector del proveedor por ticker, para el fallback de tema (spec 5.8).

    :func:`sectors_from` no decide temas: devuelve lo que dice la fuente. La
    convencion (``semiconductors``, ``financials``, ...) la fija ``themes.yaml``;
    aca el sector es un respaldo que el analisis declara como ``proveedor`` (D14).
    Un ticker que no responde simplemente no aparece: el analisis lo deja en
    ``sin_tema`` y lo dice, en vez de adivinar un sector.
    """
    result: dict[str, str] = {}
    for ticker in dict.fromkeys(tickers):
        value = fetcher(to_yahoo_symbol(ticker))
        if value:
            result[ticker.strip().upper()] = value
    return result
