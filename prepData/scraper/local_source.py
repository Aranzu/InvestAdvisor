"""Datos de mercado de una bolsa que finviz no cubre (ej. la Bolsa de Santiago).

Es el equivalente local de ``FinvizSource`` + ``YahooSource``, y hace las tres
cosas que para una bolsa local no funcionan con las fuentes de siempre:

1. **No consulta finviz.** ``SQM-B`` da 404; lo unico publicado es el ADR
   ``SQM`` en dolares. Usarlo seria mezclar dos monedas en el mismo bloque.
2. **Usa el simbolo completo de Yahoo** (``SQM-B.SN``). Sin el sufijo, Yahoo no
   conoce el enlistado local y devuelve otra cosa (o nada).
3. **Agrega la serie horaria a velas diarias antes de calcular.** La serie
   diaria de los enlistados locales de Yahoo llega congelada (ver
   ``local_technicals``), y calcular sobre una serie congelada produce un ATR 0
   que el Prompt Maestro leeria como un SL sin ruido.

Lo que trae de Yahoo ademas de los tecnicos: clasificacion, market cap, P/E
forward, dividend yield y el consenso de analistas. Para el enlistado local el
consenso viene en la moneda de la bolsa, no en la del ADR: se verifico que el
target medio de ``SQM-B.SN`` esta en pesos (80.350 contra un precio de
64.201). Las estimaciones de revenue, en cambio, llegan en la moneda financiera
de la empresa (USD en el caso de SQM) y el reporte las etiqueta.

Lo que **no** trae, y se declara como ``N/D`` con advertencia en vez de
rellenarse: el calendario de earnings, las revisiones de estimaciones y las
acciones de analistas. No es que fallen: para estos enlistados Yahoo no los
trae, y el calendario que si trae es de 2009 (descartado en ``load_earnings``).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import pandas as pd
import yfinance as yf

from .local_technicals import LocalTechnicals, candle_extremes, compute_technicals, resample_daily
from .xtb_reader import Position, format_amount
from .yahoo_source import (
    YahooData,
    _to_float,
    load_analyst_actions,
    load_earnings,
    load_estimates,
    load_news,
    parse_info,
)

logger = logging.getLogger(__name__)

# Periodo e intervalo de las velas que se agregan a diario. Dos anos dan mas de
# 500 sesiones, que es lo que necesitan la SMA200 y las ventanas de rendimiento.
HOURLY_PERIOD = "2y"
HOURLY_INTERVAL = "1h"
# Serie diaria de respaldo, cuando la horaria no se puede traer: Yahoo recorta el
# historico intradiario segun el simbolo y la antiguedad pedida.
DAILY_PERIOD = "2y"
# Si la cotizacion de mercado y la hoja transcrita se separan mas que esto, se
# avisa: la hoja se pego a mano y envejece.
MAX_SHEET_DIVERGENCE_PCT = 2.0
# Tolerancia para comparar la variacion diaria de la hoja con la del mercado.
MAX_DAY_RETURN_DIVERGENCE_PCT = 1.0


@dataclass
class LocalQuote:
    """Todo lo que el reporte necesita de una bolsa local."""

    symbol: str
    yahoo: YahooData
    technicals: LocalTechnicals


class LocalSource:
    """Cliente de las bolsas locales: cache en memoria, reintentos y delay."""

    def __init__(self, delay: float = 0.4, retries: int = 2):
        self.delay = delay
        self.retries = retries
        self._memory: dict[str, LocalQuote] = {}

    def get(self, pos: Position, force_refresh: bool = False) -> LocalQuote:
        """Datos de mercado de la posicion local.

        ``pos`` se actualiza en el camino: si el mercado tiene un precio mas
        reciente que el de la hoja, ese gana (``apply_market_price``), porque el
        bloque imprime un solo ``Actual:`` y tiene que ser el de hoy.
        """
        symbol = pos.yahoo_symbol or pos.ticker
        if symbol in self._memory and not force_refresh:
            quote = self._memory[symbol]
            self._adopt_price(pos, quote)
            return quote

        data = YahooData(ticker=symbol)
        tech: LocalTechnicals | None = None
        for attempt in range(self.retries + 1):
            try:
                tech = self._enrich(symbol, pos, data)
                break
            except Exception as exc:
                logger.warning("Yahoo fallo en %s (intento %d): %s", symbol, attempt + 1, exc)
                if attempt == self.retries:
                    data.warnings.append(f"Yahoo Finance no responde ({type(exc).__name__})")
                else:
                    time.sleep(1.5 * (attempt + 1))
        if tech is None:
            tech = LocalTechnicals(ticker=pos.ticker, company=pos.name)
            tech.warnings.append("no se pudo obtener ningun dato de mercado para este simbolo")

        # Que finviz no publique el simbolo es lo normal en una bolsa local, no un
        # problema: no va a la lista de advertencias (que es donde el Prompt Maestro
        # busca que falta algo). La procedencia ya sale publicada en el bloque por
        # ``source_label`` y ``candles_label``, asi que aqui solo queda en el log.
        logger.debug(
            "finviz no publica %s: los tecnicos salen de las velas de Yahoo en %s (%s)",
            pos.ticker, pos.currency or "la moneda de la bolsa", symbol,
        )
        quote = LocalQuote(symbol=symbol, yahoo=data, technicals=tech)
        self._memory[symbol] = quote
        self._adopt_price(pos, quote)
        time.sleep(self.delay)
        return quote

    # -- consultas --------------------------------------------------------
    def _enrich(self, symbol: str, pos: Position, data: YahooData) -> LocalTechnicals:
        y = yf.Ticker(symbol)
        info = self._info(y, data)
        parse_info(info, data)
        load_analyst_actions(y, data)
        load_estimates(y, data)
        load_earnings(y, data)
        load_news(y, data)

        daily, origin = self._load_daily(y, symbol)
        tech = compute_technicals(daily, self._quote_price(info), ticker=pos.ticker, symbol=symbol)
        tech.company = _company_name(info) or pos.name
        self._load_market_info(info, tech)
        self._load_candles(daily, data, symbol, origin)
        if not data.analyst_actions:
            data.warnings.append("este enlistado local no trae acciones de analistas en Yahoo")
        return tech

    @staticmethod
    def _info(y: yf.Ticker, data: YahooData) -> dict:
        try:
            return y.info or {}
        except Exception as exc:
            data.warnings.append(f"info de Yahoo no disponible ({exc})")
            return {}

    @staticmethod
    def _quote_price(info: dict) -> float | None:
        """Precio de cotizacion del enlistado local, en su moneda.

        Se prefiere ``currentPrice`` y no el ultimo cierre de la serie: para un
        enlistado local la serie diaria puede estar congelada (ver
        ``local_technicals``) y el unico precio al dia es el de la cotizacion.
        """
        for key in ("currentPrice", "regularMarketPrice"):
            value = _to_float(info.get(key))
            if value:
                return value
        return None

    def _load_daily(self, y: yf.Ticker, symbol: str) -> tuple[pd.DataFrame, str]:
        """Velas diarias: las horarias agregadas, con la serie diaria de respaldo."""
        try:
            hourly = y.history(period=HOURLY_PERIOD, interval=HOURLY_INTERVAL, auto_adjust=True)
        except Exception as exc:
            logger.warning("No se pudieron traer las velas horarias de %s: %s", symbol, exc)
            hourly = None
        daily = resample_daily(hourly)
        if not daily.empty:
            return daily, "velas 1h agregadas a diario"

        try:
            fallback = y.history(period=DAILY_PERIOD, interval="1d", auto_adjust=True)
        except Exception as exc:
            logger.warning("No se pudo traer la serie diaria de %s: %s", symbol, exc)
            return resample_daily(None), "sin velas"
        if fallback is None or fallback.empty:
            return resample_daily(None), "sin velas"
        return fallback, "serie diaria"

    @staticmethod
    def _load_market_info(info: dict, tech: LocalTechnicals) -> None:
        """Liquidez y riesgo que finviz no daria y Yahoo si publica.

        No se copia ``targetMeanPrice`` ni ``forwardEps``: el target ya se
        imprime desde el bloque de consenso de Yahoo (en la moneda de la bolsa)
        y el EPS forward es anual, asi que mezclado con las estimaciones
        trimestrales de Yahoo lo dejaria inconsistente.
        """
        tech.beta = _to_float(info.get("beta"))
        volume = _to_float(info.get("averageVolume"))
        # ``avg_volume`` es un texto en finviz ("2.45M"), asi que aqui se
        # formatea con separador de miles: "74,551" no se confunde con una
        # magnitud como lo haria "74551".
        tech.avg_volume = f"{volume:,.0f}" if volume else None
        cap = _to_float(info.get("marketCap"))
        tech.market_cap = format_amount(cap) if cap else None

    @staticmethod
    def _load_candles(daily: pd.DataFrame, data: YahooData, symbol: str, origin: str) -> None:
        high, low, high_date, low_date = candle_extremes(daily)
        if high is None or low is None:
            data.warnings.append("sin 22 velas diarias agregadas para el maximo/minimo")
            return
        data.high_22, data.low_22 = high, low
        data.high_22_date, data.low_22_date = high_date, low_date
        data.candles_label = f"Yahoo, {symbol} ({origin})"

    # -- cruce con la hoja transcrita -------------------------------------
    def _adopt_price(self, pos: Position, quote: LocalQuote) -> None:
        """Si el mercado tiene precio, ese es el ``Actual:`` del bloque.

        La hoja se transcribe a mano y llega con un dia de rezago. Cuando el
        precio de mercado y el de la hoja se separan mas de
        ``MAX_SHEET_DIVERGENCE_PCT`` se avisa, porque el retorno que declara el
        broker ya no corresponde al precio del bloque.
        """
        price = quote.technicals.price
        if not price:
            return
        sheet_price = pos.current_price
        if sheet_price and abs(price - sheet_price) / price * 100 > MAX_SHEET_DIVERGENCE_PCT:
            message = (
                f"el precio de la hoja ({sheet_price:g}) y el de mercado ({price:g}) difieren en mas "
                f"de {MAX_SHEET_DIVERGENCE_PCT:g}%: se usa el de mercado y el 'Retorno total' de la "
                "hoja queda desactualizado respecto a el"
            )
            if message not in pos.warnings:
                pos.warnings.append(message)
        day_change = quote.technicals.change_pct
        if pos.day_return_pct is not None and day_change is not None:
            if abs(pos.day_return_pct - day_change) > MAX_DAY_RETURN_DIVERGENCE_PCT:
                aviso = (
                    f"la variacion diaria de la hoja ({pos.day_return_pct:g}%) no coincide con la del "
                    f"mercado ({day_change:g}%): alguna de las dos fuentes va con rezago"
                )
                if aviso not in pos.warnings:
                    pos.warnings.append(aviso)
        pos.apply_market_price(price, "Yahoo")


def _company_name(info: dict) -> str:
    for key in ("longName", "shortName"):
        value = info.get(key)
        if value:
            return str(value).strip()
    return ""
